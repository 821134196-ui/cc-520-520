"""SQLite 数据层：商品链路（门店 / 供应商 / 商品 / 批次 / 库存 / 销售）、
防伪调查（案件 / 码查询 / 证据 / 结论版本 / 冻结事件 / 通知 / 审计）。

写事务一律使用 BEGIN IMMEDIATE，由调用方显式 COMMIT，
保证“结论 + 库存冻结”原子可见，销售与冻结在锁上串行化。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stores (
    id      INTEGER PRIMARY KEY,
    code    TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL CHECK(role IN ('store','hq'))
);

CREATE TABLE IF NOT EXISTS suppliers (
    id    INTEGER PRIMARY KEY,
    code  TEXT UNIQUE NOT NULL,
    name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id     INTEGER PRIMARY KEY,
    model  TEXT UNIQUE NOT NULL,
    name   TEXT NOT NULL
);

-- 供应批次：供应商 -> 门店的供货链路
CREATE TABLE IF NOT EXISTS batches (
    id                     INTEGER PRIMARY KEY,
    code                   TEXT UNIQUE NOT NULL,
    product_id             INTEGER NOT NULL REFERENCES products(id),
    supplier_id            INTEGER NOT NULL REFERENCES suppliers(id),
    supplied_store_id      INTEGER REFERENCES stores(id),
    supply_date            TEXT,
    supplier_acknowledged  INTEGER NOT NULL DEFAULT 1,  -- 0 = 供应商否认该批次
    note                   TEXT
);

-- 门店库存。status: normal 正常 / quarantined 复查暂挂(门店标记) /
-- frozen 冻结(随总部结论) / released 解除冻结 / condemned 没收销毁
CREATE TABLE IF NOT EXISTS inventory (
    id              INTEGER PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    batch_id        INTEGER NOT NULL REFERENCES batches(id),
    qty             INTEGER NOT NULL CHECK(qty >= 0),
    status          TEXT NOT NULL DEFAULT 'normal'
                    CHECK(status IN ('normal','quarantined','frozen','released','condemned')),
    frozen_case_id  INTEGER,
    updated_at      TEXT,
    UNIQUE(store_id, batch_id)
);

CREATE TABLE IF NOT EXISTS sales (
    id             INTEGER PRIMARY KEY,
    store_id       INTEGER NOT NULL REFERENCES stores(id),
    sale_no        TEXT NOT NULL,
    product_id     INTEGER REFERENCES products(id),
    batch_id       INTEGER REFERENCES batches(id),
    code           TEXT,
    customer_phone TEXT,
    sold_at        TEXT
);

-- 品牌防伪平台（模拟）的码登记册
CREATE TABLE IF NOT EXISTS code_registry (
    code             TEXT PRIMARY KEY,
    product_id       INTEGER REFERENCES products(id),
    batch_id         INTEGER REFERENCES batches(id),
    valid            INTEGER NOT NULL DEFAULT 1,  -- 1 登记在册 0 平台标记异常/假冒
    first_store_id   INTEGER REFERENCES stores(id),
    first_seen_at    TEXT,
    query_count      INTEGER NOT NULL DEFAULT 0,
    note             TEXT
);

-- 真伪质疑案件。original_case_id 非空 = 重复码归并，指向原调查
CREATE TABLE IF NOT EXISTS cases (
    id                     INTEGER PRIMARY KEY,
    case_no                TEXT UNIQUE NOT NULL,
    code                   TEXT,
    product_model          TEXT,
    sale_no                TEXT,
    batch_code             TEXT,
    customer_phone         TEXT,
    customer_desc          TEXT,
    status                 TEXT NOT NULL DEFAULT 'opened'
                           CHECK(status IN ('opened','store_checked','hq_review','closed')),
    created_store_id       INTEGER NOT NULL REFERENCES stores(id),
    original_case_id       INTEGER REFERENCES cases(id),
    merge_note             TEXT,
    current_conclusion_id  INTEGER,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS code_queries (
    id              INTEGER PRIMARY KEY,
    case_id         INTEGER NOT NULL REFERENCES cases(id),
    code            TEXT NOT NULL,
    store_id        INTEGER REFERENCES stores(id),
    queried_at      TEXT NOT NULL,
    result          TEXT NOT NULL,   -- registered / not_registered / flagged / duplicate
    is_duplicate    INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT,
    first_store_id  INTEGER REFERENCES stores(id),
    raw             TEXT
);

-- 证据永不删除；结论改判只追加新版本
-- kind: package / seal / receipt / label / sales_record / batch_record /
--       code_query / supplier_statement / hq_report / comparison / unit
-- source: customer / store / supplier / hq / registry
CREATE TABLE IF NOT EXISTS evidence (
    id              INTEGER PRIMARY KEY,
    case_id         INTEGER NOT NULL REFERENCES cases(id),
    kind            TEXT NOT NULL,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    file_path       TEXT,
    content         TEXT,
    created_by_role TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conclusions (
    id                 INTEGER PRIMARY KEY,
    case_id            INTEGER NOT NULL REFERENCES cases(id),
    version            INTEGER NOT NULL,
    verdict            TEXT NOT NULL,
                       -- pending_review / genuine / suspected_counterfeit /
                       -- counterfeit / inconclusive / sales_mismatch
    text               TEXT NOT NULL,
    issued_by_role     TEXT NOT NULL,   -- hq 结论门店不可改
    issued_by_store_id INTEGER REFERENCES stores(id),
    batch_action       TEXT NOT NULL DEFAULT 'none',
                       -- quarantine/freeze/release/condemn/none
    reason             TEXT,
    supersedes         INTEGER,
    created_at         TEXT NOT NULL,
    UNIQUE(case_id, version)
);

CREATE TABLE IF NOT EXISTS freeze_events (
    id            INTEGER PRIMARY KEY,
    case_id       INTEGER REFERENCES cases(id),
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    store_id      INTEGER REFERENCES stores(id),   -- NULL = 全连锁范围
    action        TEXT NOT NULL,                   -- quarantine/freeze/release/condemn
    qty           INTEGER,
    conclusion_id INTEGER REFERENCES conclusions(id),
    operator_role TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY,
    case_id         INTEGER NOT NULL REFERENCES cases(id),
    channel         TEXT NOT NULL DEFAULT 'sms',
    to_addr         TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL,
    provider_msg_id TEXT,
    sent_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY,
    at             TEXT NOT NULL,
    actor_role     TEXT NOT NULL,
    actor_store_id INTEGER,
    action         TEXT NOT NULL,
    entity_type    TEXT,
    entity_id      TEXT,
    allowed        INTEGER NOT NULL,
    detail         TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_code     ON cases(code);
CREATE INDEX IF NOT EXISTS idx_queries_case   ON code_queries(case_id);
CREATE INDEX IF NOT EXISTS idx_evidence_case  ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_inv_batch      ON inventory(batch_id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
