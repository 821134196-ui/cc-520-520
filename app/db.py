# -*- coding: utf-8 -*-
"""SQLite 数据层：建表、连接管理与基础仓储。

所有写操作默认在调用方提供的事务中执行；涉及库存与质控结论联动的
关键路径使用 BEGIN IMMEDIATE，避免“疑似批次在复查期间继续售出”。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

# ---- 枚举（以文本落库，便于直接阅读）------------------------------------

STORE_HQ = "HQ"  # 总部门店编号，总部复核操作以此身份执行

# 调查状态
ST_OPEN = "OPEN"            # 受理中
ST_ESCALATED = "ESCALATED"  # 已上交总部质控
ST_CLOSED = "CLOSED"        # 已闭环（有生效结论）

# 证据来源（对照页按此分组排列）
SRC_PACKAGING = "PACKAGING"  # 顾客带来的包装 / 防伪码 / 封签照片
SRC_SALES = "SALES"          # 本店销售记录
SRC_SUPPLY = "SUPPLY"        # 供应批次 / 供应商确认
SRC_HQ = "HQ"                # 总部质控复查材料
SRC_SYSTEM = "SYSTEM"        # 系统比对记录

EVIDENCE_SOURCES = [SRC_PACKAGING, SRC_SALES, SRC_SUPPLY, SRC_HQ, SRC_SYSTEM]

# 防伪码查询适配器返回的码状态
CODE_OK = "OK"                 # 首次、合法
CODE_DUPLICATE = "DUPLICATE"   # 重复（被多次查询/多处出现）
CODE_DAMAGED = "DAMAGED"       # 标签刮损、无法完整识读
CODE_UNKNOWN = "UNKNOWN"       # 防伪库无记录

# 批次库存状态
LOT_ACTIVE = "ACTIVE"
LOT_FROZEN = "FROZEN"

# 比对发现
FIND_DUPLICATE = "DUPLICATE_CODE"
FIND_SALES_MISMATCH = "SALES_RECORD_MISMATCH"
FIND_DAMAGED = "LABEL_DAMAGED"
FIND_PACKAGE_ONLY = "PACKAGE_ONLY"
FIND_SUPPLIER_DENY = "SUPPLIER_DENIED_BATCH"
FIND_MATCH = "ALL_MATCHED"

# 总部质控结论
CONCL_GENUINE = "GENUINE"          # 真品
CONCL_FAKE = "FAKE"                # 假冒
CONCL_INCONCLUSIVE = "INCONCLUSIVE"  # 无法认定

# 冻结动作
FRZ_FREEZE = "FREEZE"
FRZ_UNFREEZE = "UNFREEZE"

# 通知渠道
NTF_SMS = "SMS"
NTF_EMAIL = "EMAIL"
NTF_IN_APP = "IN_APP"

# 通知状态
NTF_QUEUED = "QUEUED"
NTF_SENT = "SENT"
NTF_FAILED = "FAILED"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stores (
    id          TEXT PRIMARY KEY,          -- 门店编号，如 S01 / HQ
    name        TEXT NOT NULL,
    is_hq       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id          TEXT PRIMARY KEY,          -- 供应商标识
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 商品型号
CREATE TABLE IF NOT EXISTS products (
    model       TEXT PRIMARY KEY,          -- 商品型号
    name        TEXT NOT NULL,
    supplier_id TEXT NOT NULL REFERENCES suppliers(id),
    created_at  TEXT NOT NULL
);

-- 供应批次（商品链路：供应商 -> 批次 -> 门店库存）
CREATE TABLE IF NOT EXISTS supply_lots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_no          TEXT NOT NULL,          -- 包装批次号
    product_model   TEXT NOT NULL REFERENCES products(model),
    supplier_id     TEXT NOT NULL REFERENCES suppliers(id),
    shipped_at      TEXT NOT NULL,
    supplier_confirmed INTEGER NOT NULL DEFAULT 1, -- 供应商是否承认该批次
    created_at      TEXT NOT NULL,
    UNIQUE (lot_no, product_model)
);

-- 门店库存批次：同一次供货在某门店的库存数量与冻结状态
CREATE TABLE IF NOT EXISTS inventory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id        TEXT NOT NULL REFERENCES stores(id),
    lot_id          INTEGER NOT NULL REFERENCES supply_lots(id),
    qty_total       INTEGER NOT NULL CHECK (qty_total >= 0),
    qty_sold        INTEGER NOT NULL DEFAULT 0 CHECK (qty_sold >= 0),
    status          TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE / FROZEN
    frozen_at       TEXT,
    frozen_reason   TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (store_id, lot_id),
    CHECK (qty_sold <= qty_total)
);

-- 销售单
CREATE TABLE IF NOT EXISTS sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no      TEXT NOT NULL UNIQUE,    -- 销售单号
    store_id        TEXT NOT NULL REFERENCES stores(id),
    lot_id          INTEGER NOT NULL REFERENCES supply_lots(id),
    product_model   TEXT NOT NULL REFERENCES products(model),
    security_code   TEXT,                    -- 售出时登记的防伪码（可空）
    customer_name   TEXT,
    sold_at         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 防伪码注册档：防伪库（模拟）中的权威记录
CREATE TABLE IF NOT EXISTS security_code_registry (
    code            TEXT PRIMARY KEY,
    product_model   TEXT NOT NULL REFERENCES products(model),
    lot_id          INTEGER REFERENCES supply_lots(id),
    first_store_id  TEXT REFERENCES stores(id),  -- 防伪库记录的首发门店
    damaged         INTEGER NOT NULL DEFAULT 0,  -- 标签是否已被标记刮损
    note            TEXT
);

-- 调查主单：同一防伪码的多次质疑归并到同一条调查
CREATE TABLE IF NOT EXISTS investigations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no             TEXT NOT NULL UNIQUE,     -- 调查编号
    store_id            TEXT NOT NULL REFERENCES stores(id), -- 受理门店
    product_model       TEXT NOT NULL,
    receipt_no          TEXT,                     -- 顾客声称的销售单
    security_code       TEXT,                     -- 顾客提供的防伪码（刮损时可能不完整）
    origin              TEXT NOT NULL DEFAULT 'INTAKE', -- INTAKE 受理 / QUERY 码查询建档
    customer_name       TEXT,
    customer_contact    TEXT,                     -- 手机号/邮箱（用于客户告知）
    notify_channel      TEXT NOT NULL DEFAULT 'SMS',
    package_only        INTEGER NOT NULL DEFAULT 0, -- 顾客仅带外包装
    status              TEXT NOT NULL DEFAULT 'OPEN',
    merged_into_id      INTEGER REFERENCES investigations(id), -- 归并到原调查
    duplicate_of_case_no TEXT,                    -- 重复码：首次出现的调查编号
    first_seen_at       TEXT,                     -- 该防伪码首次出现时间
    first_seen_store_id TEXT REFERENCES stores(id), -- 该防伪码首次出现门店
    current_conclusion  TEXT,                     -- 当前生效结论（冗余，随版本更新）
    current_version     INTEGER NOT NULL DEFAULT 0,-- 结论版本号
    closed_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inv_code ON investigations(security_code);
CREATE INDEX IF NOT EXISTS idx_inv_status ON investigations(status);

-- 调查与涉疑批次的关联（一次调查可牵出多个批次/门店库存）
CREATE TABLE IF NOT EXISTS investigation_lots (
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    lot_id              INTEGER NOT NULL REFERENCES supply_lots(id),
    marked_at           TEXT NOT NULL,
    PRIMARY KEY (investigation_id, lot_id)
);

-- 每次防伪查询都留痕，并关联到当时归属的调查（重复码不另立孤岛新案）
CREATE TABLE IF NOT EXISTS code_queries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT,
    code_fingerprint    TEXT NOT NULL,            -- 刮损码用指纹（前缀+特征）
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    store_id            TEXT NOT NULL REFERENCES stores(id),
    adapter_status      TEXT NOT NULL,            -- OK/DUPLICATE/DAMAGED/UNKNOWN
    reported_first_at   TEXT,                     -- 适配器返回的首次出现时间
    reported_first_store TEXT,
    queried_at          TEXT NOT NULL
);

-- 比对发现：受理时系统自动比对的结论性线索
CREATE TABLE IF NOT EXISTS findings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    kind                TEXT NOT NULL,
    detail              TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

-- 证据：照片/凭证/记录，按来源分组展示
CREATE TABLE IF NOT EXISTS evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    source              TEXT NOT NULL,            -- PACKAGING/SALES/SUPPLY/HQ/SYSTEM
    title               TEXT NOT NULL,
    detail              TEXT,
    file_path           TEXT,                     -- web/static/evidence 下的相对文件名
    related_receipt     TEXT,
    related_lot_no      TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_inv ON evidence(investigation_id);

-- 总部质控结论版本：复查改判产生新版本，历史版本全部保留
CREATE TABLE IF NOT EXISTS conclusion_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    version             INTEGER NOT NULL,
    conclusion          TEXT NOT NULL,            -- GENUINE/FAKE/INCONCLUSIVE
    reason              TEXT NOT NULL,
    decided_by          TEXT NOT NULL,            -- 质控判定人（总部）
    revised             INTEGER NOT NULL DEFAULT 0, -- 是否为改判版本
    supersedes_version  INTEGER,                  -- 取代的版本号
    created_at          TEXT NOT NULL,
    UNIQUE (investigation_id, version)
);

-- 批次冻结/解冻流水：与结论在同一事务更新
CREATE TABLE IF NOT EXISTS inventory_freezes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    inventory_id        INTEGER NOT NULL REFERENCES inventory(id),
    action              TEXT NOT NULL,            -- FREEZE/UNFREEZE
    reason              TEXT NOT NULL,
    operator_store      TEXT NOT NULL,            -- 执行方（受理门店或 HQ）
    conclusion_version  INTEGER,                  -- 联动的结论版本
    created_at          TEXT NOT NULL
);

-- 客户告知记录（模拟适配器）
CREATE TABLE IF NOT EXISTS notifications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id    INTEGER NOT NULL REFERENCES investigations(id),
    channel             TEXT NOT NULL,
    destination         TEXT NOT NULL,
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'QUEUED',
    provider_ref        TEXT,                     -- 模拟服务商回执
    conclusion_version  INTEGER,
    created_at          TEXT NOT NULL,
    sent_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ntf_inv ON notifications(investigation_id);

-- 审计日志：关键动作（结论改判、权限拒绝、冻结联动等）
CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_store         TEXT NOT NULL,
    action              TEXT NOT NULL,
    entity              TEXT NOT NULL,
    entity_id           TEXT,
    detail              TEXT,
    created_at          TEXT NOT NULL
);
"""


def now_iso() -> str:
    """统一时间戳（本地时区，ISO，秒级），便于页面直接展示。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    # Web 服务器为多线程，连接由 AppState 的锁串行化保护
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """事务上下文。

    immediate=True 时使用 BEGIN IMMEDIATE 直接获取写锁——库存扣减与
    批次冻结共用该事务，杜绝“复查标记与销售并发、疑似批次继续售出”。
    """
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---- 轻量查询助手 ---------------------------------------------------------

def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchone()


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def insert(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> int:
    cur = conn.execute(sql, tuple(params))
    return int(cur.lastrowid)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def write_audit(conn: sqlite3.Connection, actor: str, action: str, entity: str,
                entity_id: Any = None, detail: Any = None) -> None:
    insert(
        conn,
        "INSERT INTO audit_log(actor_store, action, entity, entity_id, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (actor, action, entity, str(entity_id) if entity_id is not None else None,
         json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail,
         now_iso()),
    )
