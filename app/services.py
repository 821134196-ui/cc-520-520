"""领域服务：受理 / 码查询 / 证据比对 / 批次冻结 / 总部复核 / 客户告知。

关键约束：
- 所有写操作在 BEGIN IMMEDIATE 事务中完成；冻结与结论同事务提交，
  销售与冻结在 SQLite 写锁上串行化，疑似批次不可能在竞态下售出。
- 防伪码重复查询一律归并到原调查（沿 original_case_id 链找到根案件）。
- 结论只追加版本（supersedes 指向上一版），门店角色无权出具或修改结论。
- 证据只增不删，复查改判的全过程通过结论版本与证据链保留。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from .adapters import AntiCounterfeitAdapter, NotificationAdapter
from .db import connect


class PermissionError(Exception):
    """门店尝试执行总部专属操作。"""


class FrozenBatchError(Exception):
    """批次处于 quarantine/frozen/condemned 状态，禁止销售。"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _rollback_safe(conn):
    """事务可能已提交（例如拦截销售时先落审计再提交），回滚需容错。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- 内部工具

def _audit(conn, actor, action, entity_type=None, entity_id=None,
           allowed=True, detail=""):
    conn.execute(
        """INSERT INTO audit_log
           (at, actor_role, actor_store_id, action, entity_type, entity_id, allowed, detail)
           VALUES (?,?,?,?,?,?,?,?)""",
        (now(), actor["role"], actor.get("store_id"), action, entity_type,
         str(entity_id) if entity_id is not None else None, 1 if allowed else 0, detail))


def _add_evidence(conn, case_id, kind, source, title, actor,
                  file_path=None, content=None):
    cur = conn.execute(
        """INSERT INTO evidence
           (case_id, kind, source, title, file_path, content, created_by_role, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (case_id, kind, source, title, file_path, content, actor["role"], now()))
    return cur.lastrowid


def _root_case_id(conn, case_id: int) -> int:
    """沿归并链找到根案件。"""
    seen = set()
    cur = case_id
    while cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT id, original_case_id FROM cases WHERE id = ?", (cur,)).fetchone()
        if row is None or row["original_case_id"] is None:
            return cur
        cur = row["original_case_id"]
    return cur


def _case_batches(conn, case_id: int):
    """案件关联批次：包装批次码 + 销售记录批次 + 码登记批次。"""
    case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    ids = set()
    if case["batch_code"]:
        b = conn.execute("SELECT id FROM batches WHERE code = ?",
                         (case["batch_code"],)).fetchone()
        if b:
            ids.add(b["id"])
    sale = conn.execute("SELECT * FROM sales WHERE sale_no = ?",
                        (case["sale_no"],)).fetchone() if case["sale_no"] else None
    if sale and sale["batch_id"]:
        ids.add(sale["batch_id"])
    if case["code"]:
        reg = conn.execute(
            "SELECT batch_id FROM code_registry WHERE code = ?",
            (case["code"],)).fetchone()
        if reg and reg["batch_id"]:
            ids.add(reg["batch_id"])
    return ids


# ---------------------------------------------------------------- 1. 受理

def accept_case(db_path, actor, *, code=None, product_model=None, sale_no=None,
                batch_code=None, customer_phone=None, customer_desc="",
                files=None):
    """门店受理顾客真伪质疑并登记顾客提供的证据（包装/封签/标签/小票照片）。"""
    if actor["role"] != "store":
        raise PermissionError("仅门店可受理顾客质疑")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = now()
        case_no = "AJ" + ts[:10].replace("-", "") + "-" + uuid.uuid4().hex[:6].upper()
        cur = conn.execute(
            """INSERT INTO cases
               (case_no, code, product_model, sale_no, batch_code,
                customer_phone, customer_desc, status, created_store_id,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'opened', ?, ?, ?)""",
            (case_no, code, product_model, sale_no, batch_code,
             customer_phone, customer_desc, actor["store_id"], ts, ts))
        case_id = cur.lastrowid
        for f in files or []:
            _add_evidence(conn, case_id, f["kind"], f.get("source", "customer"),
                          f["title"], actor, file_path=f.get("file_path"),
                          content=f.get("content"))
        _audit(conn, actor, "accept_case", "case", case_id, True, case_no)
        conn.execute("COMMIT")
        return get_case(db_path, case_id)
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 2. 码查询 + 归并

def query_code(db_path, actor, case_id: int, code: str = None):
    """查询防伪平台；若为重复码则把本案归并到原调查（不另立互不相关的新案）。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        code = code or case["code"]
        if not code:
            raise ValueError("案件未登记防伪码")

        # 适配器内部也会加锁；为避免同连接嵌套事务，先在本事务外调用。
        conn.execute("COMMIT")
        result = AntiCounterfeitAdapter(db_path).query(code, actor["store_id"])
        conn.execute("BEGIN IMMEDIATE")

        cur = conn.execute(
            """INSERT INTO code_queries
               (case_id, code, store_id, queried_at, result, is_duplicate,
                first_seen_at, first_store_id, raw)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (case_id, code, actor.get("store_id"), now(), result["result"],
             1 if result.get("is_duplicate") else 0,
             result.get("first_seen_at"), result.get("first_store_id"),
             result.get("raw", "")))
        query_id = cur.lastrowid
        _add_evidence(conn, case_id, "code_query", "registry",
                      "防伪平台查询：" + result["result"], actor,
                      content=json.dumps(result, ensure_ascii=False, indent=2))

        merge = None
        if result.get("is_duplicate") and result.get("original_case_id"):
            root_id = _root_case_id(conn, result["original_case_id"])
            if root_id != case_id and root_id != _root_case_id(conn, case_id):
                root = conn.execute(
                    "SELECT case_no, created_store_id FROM cases WHERE id = ?",
                    (root_id,)).fetchone()
                note = ("重复防伪码 %s：归并至原调查 %s（首次出现 %s，门店 #%s），第 %d 次查询"
                        % (code, root["case_no"], result.get("first_seen_at"),
                           result.get("first_store_id"), result.get("query_count")))
                conn.execute(
                    "UPDATE cases SET original_case_id = ?, merge_note = ?, updated_at = ? WHERE id = ?",
                    (root_id, note, now(), case_id))
                _audit(conn, actor, "merge_duplicate_code", "case", case_id,
                       True, "-> %s" % root_id)
                merge = {"root_case_id": root_id, "root_case_no": root["case_no"],
                         "note": note}
        conn.execute("COMMIT")
        return {"query_id": query_id, "result": result, "merge": merge}
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 3. 证据比对

def compare_evidence(db_path, actor, case_id: int):
    """店员核对本店销售记录与供应批次，产出按证据来源分组的比对结论。

    只做核对与记录，不改判真伪；无法确认时升级总部（见 escalate）。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        findings = []

        def add(level, item, detail, source):
            findings.append({"level": level, "item": item,
                             "detail": detail, "source": source})

        # —— 来源：顾客（包装/小票自述）——
        if case["sale_no"]:
            sale = conn.execute(
                """SELECT s.*, p.model AS sale_model, b.code AS sale_batch,
                          st.name AS store_name
                   FROM sales s
                   LEFT JOIN products p ON p.id = s.product_id
                   LEFT JOIN batches b ON b.id = s.batch_id
                   LEFT JOIN stores st ON st.id = s.store_id
                   WHERE s.sale_no = ?""", (case["sale_no"],)).fetchone()
            if sale is None:
                add("mismatch", "销售单",
                    "销售单号 %s 在本店（及全连锁）销售记录中不存在" % case["sale_no"],
                    "store")
            else:
                add("match", "销售单",
                    "销售单 %s 存在，售出门店：%s，时间：%s"
                    % (sale["sale_no"], sale["store_name"], sale["sold_at"]),
                    "store")
                same_store = sale["store_id"] == case["created_store_id"]
                add("match" if same_store else "mismatch", "售出门店",
                    "小票门店 %s；受理门店 #%s"
                    % (sale["store_id"], case["created_store_id"]), "store")
                if case["product_model"] and sale["sale_model"]:
                    ok = sale["sale_model"] == case["product_model"]
                    add("match" if ok else "mismatch", "商品型号",
                        "小票型号 %s；包装/顾客型号 %s"
                        % (sale["sale_model"], case["product_model"]), "store")
                if case["batch_code"] and sale["sale_batch"]:
                    ok = sale["sale_batch"] == case["batch_code"]
                    add("match" if ok else "mismatch", "批次",
                        "销售记录批次 %s；包装批次 %s"
                        % (sale["sale_batch"], case["batch_code"]), "store")
                if case["code"]:
                    ok = (sale["code"] or "") == case["code"]
                    add("match" if ok else "mismatch", "防伪码",
                        "销售记录登记码 %s；质疑商品码 %s"
                        % (sale["code"] or "（未登记）", case["code"]), "store")
        else:
            add("warn", "销售单", "顾客仅提供外包装，无销售小票，销售链路无法核对",
                "customer")

        # —— 来源：供应商（供应批次）——
        if case["batch_code"]:
            batch = conn.execute(
                """SELECT b.*, s.name AS supplier_name, p.model AS product_model,
                          st.name AS store_name
                   FROM batches b
                   JOIN suppliers s ON s.id = b.supplier_id
                   JOIN products p ON p.id = b.product_id
                   LEFT JOIN stores st ON st.id = b.supplied_store_id
                   WHERE b.code = ?""", (case["batch_code"],)).fetchone()
            if batch is None:
                add("mismatch", "供应批次",
                    "包装批次 %s 在供应记录中不存在" % case["batch_code"], "supplier")
            elif not batch["supplier_acknowledged"]:
                add("mismatch", "供应批次",
                    "供应商 %s 书面否认批次 %s 为其生产/供货"
                    % (batch["supplier_name"], batch["code"]), "supplier")
            else:
                add("match", "供应批次",
                    "批次 %s 由 %s 供应，收货门店：%s"
                    % (batch["code"], batch["supplier_name"],
                       batch["store_name"] or "—"), "supplier")

        # —— 来源：防伪平台 ——（汇总历次查询，标注首次出现）
        qrows = conn.execute(
            """SELECT * FROM code_queries WHERE case_id = ? ORDER BY id""",
            (case_id,)).fetchall()
        if not qrows:
            add("info", "防伪平台", "尚未查询防伪码", "registry")
        for q in qrows:
            if q["is_duplicate"]:
                fs = conn.execute(
                    "SELECT name FROM stores WHERE id = ?",
                    (q["first_store_id"],)).fetchone()
                add("warn", "防伪码重复",
                    "%s 首次出现于 %s、门店 %s"
                    % (q["code"], q["first_seen_at"],
                       fs["name"] if fs else q["first_store_id"]), "registry")
            elif q["result"] == "flagged":
                add("mismatch", "防伪平台", "码 %s 被平台标记异常/假冒" % q["code"],
                    "registry")
            elif q["result"] == "not_registered":
                add("mismatch", "防伪平台", "码 %s 无平台登记" % q["code"],
                    "registry")
            else:
                add("match", "防伪平台", "码 %s 登记在册且为首次查询" % q["code"],
                    "registry")

        store_ok = all(f["level"] == "match" for f in findings)
        has_mismatch = any(f["level"] == "mismatch" for f in findings)
        summary = ("本店核对通过" if store_ok and findings else
                   "发现不一致，需总部质控复查" if has_mismatch else
                   "信息不足，无法在门店层面确认")
        content = json.dumps({"summary": summary, "findings": findings},
                             ensure_ascii=False, indent=2)
        _add_evidence(conn, case_id, "comparison", "store",
                      "证据比对记录：" + summary, actor, content=content)
        conn.execute(
            "UPDATE cases SET status = 'store_checked', updated_at = ? WHERE id = ? AND status = 'opened'",
            (now(), case_id))
        _audit(conn, actor, "compare_evidence", "case", case_id, True, summary)
        conn.execute("COMMIT")
        return {"summary": summary, "findings": findings,
                "store_confirmed": store_ok and bool(findings)}
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


def get_evidence_board(db_path, case_id: int):
    """对照页数据：按证据来源排列 包装 / 销售单 / 供应批次 / 平台 / 总部。

    被归并的重复码子案件同时聚合根案件（原调查）的证据与结论版本，
    保证多次查询始终关联到同一调查而非孤立新案。
    """
    conn = connect(db_path)
    try:
        case = conn.execute(
            """SELECT c.*, s.name AS store_name,
                      oc.case_no AS original_case_no
               FROM cases c
               JOIN stores s ON s.id = c.created_store_id
               LEFT JOIN cases oc ON oc.id = c.original_case_id
               WHERE c.id = ?""", (case_id,)).fetchone()
        root_id = _root_case_id(conn, case_id)
        case_ids = {case_id, root_id}
        groups = {"customer": [], "store": [], "supplier": [],
                  "registry": [], "hq": []}
        id_list = ",".join(str(i) for i in case_ids)
        for e in conn.execute(
                """SELECT e.*, c.case_no AS owner_case_no FROM evidence e
                   JOIN cases c ON c.id = e.case_id
                   WHERE e.case_id IN (%s) ORDER BY e.id""" % id_list):
            d = dict(e)
            d["from_root"] = e["case_id"] != case_id and root_id != case_id
            groups.setdefault(e["source"], []).append(d)

        first_seen = None
        if case["code"]:
            q = conn.execute(
                """SELECT q.*, s.name AS first_store_name FROM code_queries q
                   LEFT JOIN stores s ON s.id = q.first_store_id
                   WHERE q.code = ? AND q.first_seen_at IS NOT NULL
                   ORDER BY q.first_seen_at LIMIT 1""", (case["code"],)).fetchone()
            if q:
                first_seen = {"at": q["first_seen_at"],
                              "store": q["first_store_name"]
                              or ("门店 #%s" % q["first_store_id"]),
                              "query_count": conn.execute(
                                  "SELECT COALESCE(query_count,0) AS c FROM code_registry WHERE code = ?",
                                  (case["code"],)).fetchone()["c"]}

        inv = []
        for cid_ in case_ids:
            for bid in _case_batches(conn, cid_):
                b = conn.execute("SELECT code FROM batches WHERE id = ?",
                                 (bid,)).fetchone()
                for r in conn.execute(
                        """SELECT i.*, s.name AS store_name FROM inventory i
                           JOIN stores s ON s.id = i.store_id WHERE batch_id = ?""",
                        (bid,)).fetchall():
                    d = dict(r)
                    d["batch_code"] = b["code"]
                    if d not in inv:
                        inv.append(d)

        # 结论版本与当前有效指针一律以根案件为准
        root_row = conn.execute(
            "SELECT current_conclusion_id FROM cases WHERE id = ?",
            (root_id,)).fetchone()
        versions = [dict(r) for r in conn.execute(
            "SELECT * FROM conclusions WHERE case_id = ? ORDER BY version",
            (root_id,)).fetchall()]
        case_d = dict(case)
        case_d["root_case_id"] = root_id
        case_d["current_conclusion_id"] = root_row["current_conclusion_id"]
        return {"case": case_d, "groups": groups,
                "first_seen": first_seen, "inventory": inv, "versions": versions}
    finally:
        conn.close()


# ---------------------------------------------------------------- 4. 批次冻结 / 库存标记

def _apply_inventory_action(conn, case_id, batch_ids, action, actor,
                            conclusion_id=None):
    """在当前事务内更新库存状态并写冻结事件。调用方持有 BEGIN IMMEDIATE。

    作用域：
    - freeze/condemn：疑似/假冒批次必须全连锁停止销售，作用于批次全部库存；
    - release：只解除由本案件挂接（frozen_case_id）的库存，不影响无关正常库存；
    - quarantine：只作用于当前正常库存。
    """
    status_map = {"quarantine": "quarantined", "freeze": "frozen",
                  "release": "released", "condemn": "condemned"}
    new_status = status_map[action]
    where = {
        "freeze": "status != 'condemned'",
        "condemn": "1=1",
        "release": "frozen_case_id = ?",
        "quarantine": "status = 'normal'",
    }[action]
    params = (case_id,) if action == "release" else ()
    for bid in batch_ids:
        rows = conn.execute(
            "SELECT id, store_id, qty, status FROM inventory WHERE batch_id = ? AND " + where,
            (bid, *params)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE inventory SET status = ?, frozen_case_id = ?, updated_at = ? WHERE id = ?",
                (new_status, case_id if action != "release" else None, now(), r["id"]))
            conn.execute(
                """INSERT INTO freeze_events
                   (case_id, batch_id, store_id, action, qty, conclusion_id,
                    operator_role, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (case_id, bid, r["store_id"], action, r["qty"], conclusion_id,
                 actor["role"], now()))


def mark_related_inventory(db_path, actor, case_id: int):
    """门店在送总部复查前标记（暂挂）相关库存——门店权限内的 quarantine。"""
    if actor["role"] != "store":
        raise PermissionError("仅门店可标记库存")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch_ids = _case_batches(conn, case_id)
        if not batch_ids:
            conn.execute("COMMIT")
            return {"marked": 0}
        # 仅暂挂本门店、当前正常的库存
        n = 0
        for bid in batch_ids:
            rows = conn.execute(
                "SELECT id, qty FROM inventory WHERE batch_id = ? AND store_id = ? AND status = 'normal'",
                (bid, actor["store_id"])).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE inventory SET status = 'quarantined', frozen_case_id = ?, updated_at = ? WHERE id = ?",
                    (case_id, now(), r["id"]))
                conn.execute(
                    """INSERT INTO freeze_events
                       (case_id, batch_id, store_id, action, qty, operator_role, created_at)
                       VALUES (?,?,?,?, 'quarantine', ?, ?)""",
                    (case_id, bid, actor["store_id"], r["qty"], actor["role"], now()))
                n += 1
        conn.execute(
            "UPDATE cases SET status = 'hq_review', updated_at = ? WHERE id = ?",
            (now(), case_id))
        _add_evidence(conn, case_id, "batch_record", "store",
                      "复查期间标记相关库存 %d 条（暂挂待检）" % n, actor)
        _audit(conn, actor, "quarantine_inventory", "case", case_id, True,
               "rows=%d" % n)
        conn.execute("COMMIT")
        return {"marked": n}
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


def sell_item(db_path, actor, *, store_id, batch_id, qty, code=None,
              customer_phone=None):
    """销售出库：库存非正常状态一律拒绝。与冻结操作竞争同一写锁。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")  # 与冻结/复核串行化
        inv = conn.execute(
            "SELECT * FROM inventory WHERE store_id = ? AND batch_id = ?",
            (store_id, batch_id)).fetchone()
        if inv is None:
            raise LookupError("该门店无此批次库存")
        if inv["status"] != "normal":
            _audit(conn, actor, "sell_blocked", "inventory", inv["id"], False,
                   "status=%s" % inv["status"])
            conn.execute("COMMIT")
            raise FrozenBatchError(
                "批次库存状态为 %s，禁止销售（复查暂挂/冻结/没收）" % inv["status"])
        if inv["qty"] < qty:
            raise ValueError("库存不足")
        conn.execute("UPDATE inventory SET qty = qty - ?, updated_at = ? WHERE id = ?",
                     (qty, now(), inv["id"]))
        product = conn.execute(
            "SELECT product_id FROM batches WHERE id = ?", (batch_id,)).fetchone()
        cur = conn.execute(
            """INSERT INTO sales
               (store_id, sale_no, product_id, batch_id, code, customer_phone, sold_at)
               VALUES (?,?,?,?,?,?,?)""",
            (store_id, "XS" + uuid.uuid4().hex[:10].upper(), product["product_id"],
             batch_id, code, customer_phone, now()))
        _audit(conn, actor, "sell", "sale", cur.lastrowid, True)
        conn.execute("COMMIT")
        return {"sale_id": cur.lastrowid}
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 5. 总部复核 + 结论版本

def issue_conclusion(db_path, actor, case_id: int, *, verdict, text,
                     batch_action="none", reason=""):
    """总部质控出具/改判结论。结论与库存动作在同一事务提交。门店无权调用。"""
    if actor["role"] != "hq":
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _audit(conn, actor, "issue_conclusion_denied", "case", case_id,
                   False, "门店无权出具总部结论")
            conn.execute("COMMIT")
        finally:
            conn.close()
        raise PermissionError("门店不能出具或修改总部质控结论")

    valid = {"pending_review", "genuine", "suspected_counterfeit",
             "counterfeit", "inconclusive", "sales_mismatch"}
    if verdict not in valid:
        raise ValueError("非法结论类型")
    if batch_action not in {"none", "quarantine", "freeze", "release", "condemn"}:
        raise ValueError("非法库存动作")

    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        root = _root_case_id(conn, case_id)
        prev = conn.execute(
            "SELECT * FROM conclusions WHERE case_id = ? ORDER BY version DESC LIMIT 1",
            (root,)).fetchone()
        version = (prev["version"] + 1) if prev else 1
        cur = conn.execute(
            """INSERT INTO conclusions
               (case_id, version, verdict, text, issued_by_role, issued_by_store_id,
                batch_action, reason, supersedes, created_at)
               VALUES (?,?,?,?, 'hq', ?, ?, ?, ?, ?)""",
            (root, version, verdict, text, actor.get("store_id"), batch_action,
             reason, prev["id"] if prev else None, now()))
        conclusion_id = cur.lastrowid

        if batch_action != "none":
            _apply_inventory_action(conn, root, _case_batches(conn, root),
                                    batch_action, actor, conclusion_id)

        # 复查后改判：状态重新置为 closed；归并子案件同步指向同一结论
        conn.execute(
            "UPDATE cases SET current_conclusion_id = ?, status = 'closed', updated_at = ? WHERE id = ?",
            (conclusion_id, now(), root))
        conn.execute(
            "UPDATE cases SET updated_at = ? WHERE original_case_id = ?",
            (now(), root))
        tag = "初判" if version == 1 else "改判 v%d" % version
        _add_evidence(conn, root, "hq_report", "hq",
                      "总部质控%s：%s" % (tag, verdict), actor,
                      content="%s\n\n理由：%s\n\n库存动作：%s"
                              % (text, reason or "（略）", batch_action))
        _audit(conn, actor, "issue_conclusion", "case", root, True,
               "v%d %s %s" % (version, verdict, batch_action))
        conn.execute("COMMIT")
        return {"case_id": root, "version": version,
                "conclusion_id": conclusion_id}
    except Exception:
        _rollback_safe(conn)
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- 6. 客户告知

def notify_customer(db_path, actor, case_id: int):
    """依据当前有效结论全文告知顾客（结论文字不截断）。

    传入的可能是根案件或被归并的重复码子案件：告知对象始终是该案件登记的
    顾客，内容引用根案件上的当前有效结论。
    """
    conn = connect(db_path)
    try:
        case = conn.execute("SELECT * FROM cases WHERE id = ?",
                            (case_id,)).fetchone()
        root_id = _root_case_id(conn, case_id)
        root = conn.execute("SELECT * FROM cases WHERE id = ?",
                            (root_id,)).fetchone()
        phone = case["customer_phone"]
        if not phone:
            raise ValueError("未登记顾客联系方式")
        conc = conn.execute(
            "SELECT * FROM conclusions WHERE id = ?",
            (root["current_conclusion_id"],)).fetchone()
        if conc is None:
            raise ValueError("总部结论尚未出具，暂不能向顾客告知最终结果")
        merged_note = ""
        if root_id != case_id:
            merged_note = "（您的案件因防伪码重复已归并至原调查 %s 统一处理）" % root["case_no"]
        content = ("【质控告知】案件 %s：%s您反映商品经总部质控复查（结论第 %d 版），%s。%s 如有疑问可凭本通知到店复核。"
                   % (root["case_no"], merged_note, conc["version"],
                      dict(genuine="判定为正品",
                           suspected_counterfeit="判定为疑似假冒",
                           counterfeit="判定为假冒商品",
                           inconclusive="现有证据不足，无法得出确定性结论",
                           sales_mismatch="判定为销售记录不匹配，商品来源待查").get(
                           conc["verdict"], conc["verdict"]),
                       conc["text"]))
        result = NotificationAdapter(db_path).send(case_id, phone, content)
        conn.execute("BEGIN IMMEDIATE")
        _audit(conn, actor, "notify_customer", "case", case_id, True,
               "root=%s msg=%s" % (root_id, result["provider_msg_id"]))
        conn.execute("COMMIT")
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------- 查询

def get_case(db_path, case_id: int):
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT c.*, s.name AS store_name, oc.case_no AS original_case_no
               FROM cases c JOIN stores s ON s.id = c.created_store_id
               LEFT JOIN cases oc ON oc.id = c.original_case_id
               WHERE c.id = ?""", (case_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_cases(db_path, frozen_only=False):
    conn = connect(db_path)
    try:
        sql = """SELECT DISTINCT c.*, s.name AS store_name,
                        (SELECT COUNT(*) FROM evidence e WHERE e.case_id = c.id) AS evidence_count,
                        (SELECT MAX(version) FROM conclusions cc WHERE cc.case_id = c.id) AS conclusion_version
                 FROM cases c JOIN stores s ON s.id = c.created_store_id"""
        if frozen_only:
            sql += """ WHERE c.id IN (
                    SELECT frozen_case_id FROM inventory
                    WHERE status IN ('frozen','quarantined','condemned')
                      AND frozen_case_id IS NOT NULL)
                 OR c.original_case_id IN (
                    SELECT frozen_case_id FROM inventory
                    WHERE status IN ('frozen','quarantined','condemned')
                      AND frozen_case_id IS NOT NULL)"""
        sql += " ORDER BY c.id"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()
