# -*- coding: utf-8 -*-
"""核心业务服务：受理、码查询、证据比对、批次冻结、总部复核、客户告知。

关键约束：
- 同一防伪码多次查询/举报一律归并到原调查（merged_into_id），不产生孤岛新案；
- 批次冻结与质控结论在同一个 BEGIN IMMEDIATE 事务中更新；
- 只有总部（HQ）能出具/改判结论，门店仅能受理、上报与告知；
- 销售出库与批次冻结互斥，冻结后的疑似批次一单都卖不出去。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Optional

from . import db, evidence_assets
from .adapters import (
    AntiCounterfeitAdapter, CodeQueryResult, NotificationAdapter, SupplierAdapter,
    code_fingerprint,
)
from .db import (
    CODE_DAMAGED, CODE_DUPLICATE, CODE_OK, CODE_UNKNOWN, CONCL_FAKE, CONCL_GENUINE,
    CONCL_INCONCLUSIVE, FIND_DAMAGED, FIND_DUPLICATE, FIND_MATCH, FIND_PACKAGE_ONLY,
    FIND_SALES_MISMATCH, FIND_SUPPLIER_DENY, FRZ_FREEZE, FRZ_UNFREEZE, LOT_ACTIVE,
    LOT_FROZEN, NTF_IN_APP, NTF_SENT, NTF_SMS, SRC_HQ, SRC_PACKAGING, SRC_SALES,
    SRC_SUPPLY, SRC_SYSTEM, ST_CLOSED, ST_ESCALATED, ST_OPEN, STORE_HQ,
    insert, now_iso, query_all, query_one, transaction,
)

CONCLUSION_TEXT = {
    CONCL_GENUINE: "真品",
    CONCL_FAKE: "假冒商品",
    CONCL_INCONCLUSIVE: "无法认定",
}


class ServiceError(Exception):
    """业务规则错误（400）。"""


class PermissionError_(Exception):
    """门店越权（403）：门店不能执行总部动作。"""


class LotFrozenError(Exception):
    """库存已冻结，禁止出库。"""


def norm_code(code: Optional[str]) -> Optional[str]:
    return (code or "").strip().upper() or None


class Service:
    def __init__(self, conn: sqlite3.Connection, evidence_dir: str):
        self.conn = conn
        self.evidence_dir = evidence_dir
        self.supplier = SupplierAdapter()
        self.notifier = NotificationAdapter()
        self.antifake = AntiCounterfeitAdapter(conn, self._find_code_history_row)

    # ------------------------------------------------------------------ 工具

    def _store(self, conn, store_id: str) -> sqlite3.Row:
        row = query_one(conn, "SELECT * FROM stores WHERE id=?", (store_id,))
        if row is None:
            raise ServiceError(f"未知门店：{store_id}")
        return row

    def _require_hq(self, actor: str) -> None:
        if actor != STORE_HQ:
            raise PermissionError_(
                f"门店 {actor} 无权出具或修改总部质控结论（仅总部质控可操作）。"
            )

    def _require_case_owner(self, conn, inv: sqlite3.Row, actor: str) -> None:
        if actor != STORE_HQ and inv["store_id"] != actor:
            raise PermissionError_(
                f"门店 {actor} 不能操作其他门店 {inv['store_id']} 的调查。"
            )

    def _effective(self, conn, inv_id: int) -> sqlite3.Row:
        """沿 merged_into_id 找到当前生效调查。"""
        for _ in range(50):
            row = query_one(conn, "SELECT * FROM investigations WHERE id=?", (inv_id,))
            if row is None:
                raise ServiceError("调查不存在")
            if row["merged_into_id"]:
                inv_id = row["merged_into_id"]
            else:
                return row
        raise ServiceError("调查归并链异常")

    def _find_code_history_row(self, code: Optional[str]) -> Optional[sqlite3.Row]:
        """供防伪适配器调用：找该码的既往生效调查（须在事务连接内）。"""
        if not code:
            return None
        n = norm_code(code)
        rows = query_all(
            self.conn,
            "SELECT * FROM investigations WHERE merged_into_id IS NULL"
            " AND security_code IS NOT NULL ORDER BY id ASC",
        )
        for r in rows:
            if norm_code(r["security_code"]) == n:
                return r
        # 刮损码：用查询指纹兜底归并
        q = query_one(
            self.conn,
            "SELECT investigation_id FROM code_queries WHERE code_fingerprint=?"
            " ORDER BY id ASC LIMIT 1",
            (code_fingerprint(n),),
        )
        if q:
            return self._effective(self.conn, q["investigation_id"])
        return None

    def _new_case_no(self, conn) -> str:
        day = datetime.now().strftime("%Y%m%d")
        n = query_one(
            conn,
            "SELECT COUNT(*) AS c FROM investigations WHERE case_no LIKE ?",
            (f"AS-{day}-%",),
        )["c"]
        return f"AS-{day}-{n + 1:03d}"

    def _evidence_path(self, case_no: str, filename: str) -> str:
        return os.path.join(self.evidence_dir, case_no, filename)

    def _add_evidence(self, conn, inv_id: int, source: str, title: str,
                      detail: str = "", file_rel: Optional[str] = None,
                      related_receipt: Optional[str] = None,
                      related_lot_no: Optional[str] = None) -> int:
        return insert(
            conn,
            "INSERT INTO evidence(investigation_id, source, title, detail, file_path,"
            " related_receipt, related_lot_no, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (inv_id, source, title, detail, file_rel, related_receipt,
             related_lot_no, now_iso()),
        )

    def _add_finding(self, conn, inv_id: int, kind: str, detail: str) -> None:
        insert(conn,
               "INSERT INTO findings(investigation_id, kind, detail, created_at)"
               " VALUES (?,?,?,?)", (inv_id, kind, detail, now_iso()))

    def _log_query(self, conn, *, code: Optional[str], inv_id: int, store_id: str,
                   result: CodeQueryResult) -> None:
        insert(
            conn,
            "INSERT INTO code_queries(code, code_fingerprint, investigation_id,"
            " store_id, adapter_status, reported_first_at, reported_first_store, queried_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (norm_code(code), code_fingerprint(norm_code(code) or "∅"), inv_id,
             store_id, result.status, result.first_seen_at, result.first_seen_store,
             now_iso()),
        )

    # ---------------------------------------------------------- 库存/冻结

    def _link_lots(self, conn, inv_id: int, lot_ids: list[int]) -> None:
        for lot_id in lot_ids:
            insert(
                conn,
                "INSERT OR IGNORE INTO investigation_lots(investigation_id, lot_id, marked_at)"
                " VALUES (?,?,?)", (inv_id, lot_id, now_iso()),
            )

    def _freeze_inventory(self, conn, *, inv_id: int, lot_ids: list[int],
                          reason: str, actor: str, store_scope: Optional[str] = None,
                          version: Optional[int] = None) -> int:
        """冻结指定批次的库存。

        store_scope 为门店号时只冻结该门店库存（门店受理）；为 None 时
        冻结连锁全部相关库存（总部动作）。返回新冻结条数。
        """
        self._link_lots(conn, inv_id, lot_ids)
        if not lot_ids:
            db.write_audit(conn, actor, "FREEZE", "investigation", inv_id,
                           {"lot_ids": [], "store_scope": store_scope,
                            "frozen": 0, "reason": reason})
            return 0
        placeholders = ",".join("?" * len(lot_ids))
        sql = f"SELECT * FROM inventory WHERE status=? AND lot_id IN ({placeholders})"
        params: list[Any] = [LOT_ACTIVE, *lot_ids]
        if store_scope is not None:
            sql += " AND store_id=?"
            params.append(store_scope)
        rows = query_all(conn, sql, params)
        for r in rows:
            conn.execute(
                "UPDATE inventory SET status=?, frozen_at=?, frozen_reason=? WHERE id=?",
                (LOT_FROZEN, now_iso(), reason, r["id"]),
            )
            insert(
                conn,
                "INSERT INTO inventory_freezes(investigation_id, inventory_id, action,"
                " reason, operator_store, conclusion_version, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (inv_id, r["id"], FRZ_FREEZE, reason, actor, version, now_iso()),
            )
        db.write_audit(conn, actor, "FREEZE", "investigation", inv_id,
                       {"lot_ids": lot_ids, "store_scope": store_scope,
                        "frozen": len(rows), "reason": reason})
        return len(rows)

    def _unfreeze_inventory(self, conn, *, inv_id: int, lot_ids: list[int],
                            reason: str, actor: str, version: int) -> int:
        if not lot_ids:
            return 0
        placeholders = ",".join("?" * len(lot_ids))
        rows = query_all(
            conn,
            f"SELECT * FROM inventory WHERE status=? AND lot_id IN ({placeholders})",
            [LOT_FROZEN, *lot_ids],
        )
        for r in rows:
            conn.execute(
                "UPDATE inventory SET status=?, frozen_at=NULL, frozen_reason=NULL WHERE id=?",
                (LOT_ACTIVE, r["id"]),
            )
            insert(
                conn,
                "INSERT INTO inventory_freezes(investigation_id, inventory_id, action,"
                " reason, operator_store, conclusion_version, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (inv_id, r["id"], FRZ_UNFREEZE, reason, actor, version, now_iso()),
            )
        db.write_audit(conn, actor, "UNFREEZE", "investigation", inv_id,
                       {"lot_ids": lot_ids, "unfrozen": len(rows),
                        "conclusion_version": version})
        return len(rows)

    def linked_lot_ids(self, conn, inv_id: int) -> list[int]:
        return [r["lot_id"] for r in query_all(
            conn,
            "SELECT lot_id FROM investigation_lots WHERE investigation_id=? ORDER BY lot_id",
            (inv_id,))]

    def sell(self, *, store_id: str, receipt_no: str, lot_id: int,
             product_model: str, security_code: Optional[str] = None,
             customer_name: Optional[str] = None) -> dict:
        """销售出库（演示/竞态验证用）。

        BEGIN IMMEDIATE 与冻结路径互斥：拿到写锁后重新读取库存状态，
        因此冻结一旦提交，任何并发/后续销售都会被拒绝。
        """
        with transaction(self.conn, immediate=True) as conn:
            self._store(conn, store_id)
            inv = query_one(
                conn,
                "SELECT * FROM inventory WHERE store_id=? AND lot_id=?",
                (store_id, lot_id),
            )
            if inv is None:
                raise ServiceError("该门店无此批次库存")
            if inv["status"] == LOT_FROZEN:
                raise LotFrozenError(
                    f"批次库存已冻结（{inv['frozen_reason'] or '质控复查中'}），禁止销售。"
                )
            if inv["qty_sold"] >= inv["qty_total"]:
                raise ServiceError("库存不足")
            sid = insert(
                conn,
                "INSERT INTO sales(receipt_no, store_id, lot_id, product_model,"
                " security_code, customer_name, sold_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (receipt_no, store_id, lot_id, product_model, norm_code(security_code),
                 customer_name, now_iso(), now_iso()),
            )
            conn.execute("UPDATE inventory SET qty_sold=qty_sold+1 WHERE id=?", (inv["id"],))
            db.write_audit(conn, store_id, "SELL", "sales", sid,
                           {"receipt_no": receipt_no, "lot_id": lot_id})
            return {"sales_id": sid, "receipt_no": receipt_no, "allowed": True}

    # -------------------------------------------------------------- 受理

    def intake(self, *, actor_store: str, product_model: str, receipt_no: Optional[str],
               security_code: Optional[str], lot_no: Optional[str],
               package_only: bool = False, label_damaged: bool = False,
               seal_intact: bool = True, customer_name: Optional[str] = None,
               customer_contact: Optional[str] = None,
               notify_channel: str = NTF_SMS) -> dict:
        """门店受理顾客真伪质疑：登记 + 自动证据比对 + 可疑即上报并冻结。"""
        with transaction(self.conn, immediate=True) as conn:
            store = self._store(conn, actor_store)
            product = query_one(conn, "SELECT * FROM products WHERE model=?", (product_model,))
            if product is None:
                raise ServiceError(f"未知商品型号：{product_model}")

            code = norm_code(security_code)
            history = self.antifake.query(code, damaged_hint=label_damaged)

            # ===== 情形一：重复码 —— 归并到原调查，不另立新案 =====
            if history.status == CODE_DUPLICATE:
                return self._intake_duplicate(
                    conn, actor=actor_store, store_name=store["name"],
                    product_model=product_model, receipt_no=receipt_no, code=code,
                    lot_no=lot_no, package_only=package_only,
                    label_damaged=label_damaged, history_result=history,
                    customer_name=customer_name, customer_contact=customer_contact,
                    notify_channel=notify_channel,
                )

            # ===== 情形二：全新线索，建立调查 =====
            case_no = self._new_case_no(conn)
            ts = now_iso()
            inv_id = insert(
                conn,
                "INSERT INTO investigations(case_no, store_id, product_model, receipt_no,"
                " security_code, origin, customer_name, customer_contact, notify_channel,"
                " package_only, status, first_seen_at, first_seen_store_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_no, actor_store, product_model, receipt_no, code, "INTAKE",
                 customer_name, customer_contact, notify_channel,
                 1 if package_only else 0, ST_OPEN, ts, actor_store, ts, ts),
            )
            self._log_query(conn, code=code, inv_id=inv_id, store_id=actor_store,
                            result=history)

            suspicious: list[str] = []
            implicated_lots: list[int] = []

            # --- 证据 1：包装/防伪码/封签（顾客侧，来源 PACKAGING）---
            pkg_file = f"packaging-{inv_id}.svg"
            seal_file = f"seal-{inv_id}.svg"
            evidence_assets.packaging(
                self._evidence_path(case_no, pkg_file),
                product_model, product["name"], lot_no or "（批次不清）",
                code or "（未提供）", damaged=(label_damaged or history.status == CODE_DAMAGED),
                package_only=package_only,
            )
            evidence_assets.seal(self._evidence_path(case_no, seal_file), intact=seal_intact)
            self._add_evidence(
                conn, inv_id, SRC_PACKAGING, "顾客商品外包装照片",
                f"包装批次 {lot_no or '不清'}；防伪码 {code or '未提供'}；"
                + ("仅携带外包装，无商品本体；" if package_only else "")
                + ("防伪标签刮损；" if label_damaged else "")
                + ("封签完好。" if seal_intact else "封签破损，有调换嫌疑。"),
                file_rel=os.path.join(case_no, pkg_file), related_lot_no=lot_no)
            self._add_evidence(
                conn, inv_id, SRC_PACKAGING, "防伪封签照片",
                "封签完好，封口连续。" if seal_intact else "封签破损/揭贴痕迹，包装可能被调换。",
                file_rel=os.path.join(case_no, seal_file), related_lot_no=lot_no)
            if label_damaged or history.status == CODE_DAMAGED:
                suspicious.append(FIND_DAMAGED)
                self._add_finding(conn, inv_id, FIND_DAMAGED,
                                  "防伪标签涂层刮损、字符缺失，无法完整识读。")
            if package_only:
                suspicious.append(FIND_PACKAGE_ONLY)
                self._add_finding(conn, inv_id, FIND_PACKAGE_ONLY,
                                  "顾客仅携带外包装，无商品本体可核，无法完成实物比对。")
            if not seal_intact:
                self._add_finding(conn, inv_id, "SEAL_BROKEN",
                                  "防伪封签破损，包装链路不连续。")
                suspicious.append("SEAL_BROKEN")
            if history.status == CODE_UNKNOWN:
                self._add_finding(conn, inv_id, "CODE_UNKNOWN",
                                  f"防伪平台查无此码（{history.message}）")
                suspicious.append("CODE_UNKNOWN")

            # --- 证据 2：本店销售记录（来源 SALES）---
            sale = None
            if receipt_no:
                sale = query_one(conn, "SELECT * FROM sales WHERE receipt_no=?", (receipt_no,))
            if receipt_no and sale is not None:
                mismatches: list[str] = []
                if sale["store_id"] != actor_store:
                    other = query_one(conn, "SELECT name FROM stores WHERE id=?",
                                      (sale["store_id"],))
                    mismatches.append(
                        f"销售单属于{other['name'] if other else sale['store_id']}，非本店售出")
                if sale["product_model"] != product_model:
                    mismatches.append(
                        f"销售单型号为 {sale['product_model']}，与顾客所称 {product_model} 不一致")
                if sale["security_code"] and code and norm_code(sale["security_code"]) != code:
                    mismatches.append(
                        f"销售单登记防伪码 {sale['security_code']} 与实物防伪码 {code} 不一致")
                sale_store = query_one(conn, "SELECT name FROM stores WHERE id=?",
                                       (sale["store_id"],))
                detail = ("销售记录核对一致：门店、型号、防伪码均匹配。"
                          if not mismatches else "；".join(mismatches))
                rfile = f"receipt-{inv_id}.svg"
                evidence_assets.receipt(
                    self._evidence_path(case_no, rfile), receipt_no,
                    sale_store["name"], sale["product_model"], sale["security_code"],
                    sale["sold_at"], mismatch="；".join(mismatches) or None)
                self._add_evidence(
                    conn, inv_id, SRC_SALES, f"销售单 {receipt_no}（本店存根）", detail,
                    file_rel=os.path.join(case_no, rfile), related_receipt=receipt_no,
                    related_lot_no=query_one(conn, "SELECT lot_no FROM supply_lots WHERE id=?",
                                             (sale["lot_id"],))["lot_no"])
                implicated_lots.append(sale["lot_id"])
                if mismatches:
                    suspicious.append(FIND_SALES_MISMATCH)
                    self._add_finding(conn, inv_id, FIND_SALES_MISMATCH, "；".join(mismatches))
            else:
                self._add_evidence(
                    conn, inv_id, SRC_SALES,
                    f"销售记录核查：{receipt_no or '未提供销售单'}",
                    "连锁销售台账中查无此销售单，无法证明商品购自本店。"
                    if receipt_no else "顾客未提供销售单号。")
                if receipt_no:
                    suspicious.append(FIND_SALES_MISMATCH)
                    self._add_finding(conn, inv_id, FIND_SALES_MISMATCH,
                                      f"销售单 {receipt_no} 在销售台账中不存在。")

            # --- 证据 3：供应批次/供应商确认（来源 SUPPLY）---
            claimed_lot = None
            if lot_no:
                claimed_lot = query_one(
                    conn, "SELECT * FROM supply_lots WHERE lot_no=? AND product_model=?",
                    (lot_no, product_model))
            if claimed_lot is not None:
                implicated_lots.append(claimed_lot["id"])
                conf = self.supplier.confirm_batch(conn, lot_no, product_model,
                                                   product["supplier_id"])
                srow = query_one(conn, "SELECT name FROM suppliers WHERE id=?",
                                 (claimed_lot["supplier_id"],))
                lfile = f"supplier-{inv_id}.svg"
                evidence_assets.supplier_letter(
                    self._evidence_path(case_no, lfile),
                    srow["name"], lot_no, conf["acknowledged"])
                self._add_evidence(
                    conn, inv_id, SRC_SUPPLY, f"供应商批次确认函 · {lot_no}",
                    conf["message"], file_rel=os.path.join(case_no, lfile),
                    related_lot_no=lot_no)
                if not conf["acknowledged"]:
                    suspicious.append(FIND_SUPPLIER_DENY)
                    self._add_finding(
                        conn, inv_id, FIND_SUPPLIER_DENY,
                        f"供应商否认批次 {lot_no}：{conf['message']}")
            elif lot_no:
                lfile = f"supplier-{inv_id}.svg"
                srow = query_one(conn, "SELECT name FROM suppliers WHERE id=?",
                                 (product["supplier_id"],))
                evidence_assets.supplier_letter(
                    self._evidence_path(case_no, lfile), srow["name"], lot_no, False)
                self._add_evidence(
                    conn, inv_id, SRC_SUPPLY, f"供应商批次确认函 · {lot_no}",
                    f"供应商 {srow['name']} 出厂台账无批次 {lot_no}，否认生产。",
                    file_rel=os.path.join(case_no, lfile), related_lot_no=lot_no)
                suspicious.append(FIND_SUPPLIER_DENY)
                self._add_finding(conn, inv_id, FIND_SUPPLIER_DENY,
                                  f"供应商台账与包装批次 {lot_no} 均无匹配，批次涉嫌伪造。")

            # --- 系统比对小结（来源 SYSTEM）---
            implicated_lots = sorted(set(implicated_lots))
            # 无论是否可疑，涉疑批次都先关联到调查（关联不等于冻结），
            # 保证总部后续复查/改判时能定位到完整库存范围
            self._link_lots(conn, inv_id, implicated_lots)
            if not suspicious:
                self._add_finding(conn, inv_id, FIND_MATCH,
                                  "防伪码首查正常、销售记录与供应批次均一致，门店核对无异常。")
                result = {"merged": False, "investigation_id": inv_id, "case_no": case_no,
                          "escalated": False,
                          "message": "本店记录核对一致，暂不上报；可在页面提交总部复核或告知顾客。"}
                db.write_audit(conn, actor_store, "INTAKE", "investigation", inv_id,
                               {"case_no": case_no, "result": "matched"})
                return result

            # 可疑：上报总部 + 冻结本店相关库存（同一事务）
            n_frozen = self._freeze_inventory(
                conn, inv_id=inv_id, lot_ids=implicated_lots,
                reason=f"调查 {case_no} 复查期间预防性冻结（{','.join(suspicious)}）",
                actor=actor_store, store_scope=actor_store,
            )
            conn.execute("UPDATE investigations SET status=?, updated_at=? WHERE id=?",
                         (ST_ESCALATED, now_iso(), inv_id))
            self._add_evidence(
                conn, inv_id, SRC_SYSTEM, "自动比对结论 · 上报总部质控",
                f"发现线索：{'、'.join(suspicious)}；已冻结本店相关库存 {n_frozen} 条，"
                "调查上交总部质控复查，门店冻结期间不得销售该批次。")
            db.write_audit(conn, actor_store, "ESCALATE", "investigation", inv_id,
                           {"case_no": case_no, "findings": suspicious,
                            "frozen_rows": n_frozen})
            return {"merged": False, "investigation_id": inv_id, "case_no": case_no,
                    "escalated": True, "frozen_rows": n_frozen,
                    "findings": suspicious,
                    "message": f"已受理并上报总部质控，相关库存冻结 {n_frozen} 条。"}

    def _intake_duplicate(self, conn, *, actor: str, store_name: str, product_model: str,
                          receipt_no, code, lot_no, package_only, label_damaged,
                          history_result: CodeQueryResult, customer_name,
                          customer_contact, notify_channel) -> dict:
        """重复码受理：全部挂到原调查下，并按需重新升级/冻结新牵出的批次。"""
        parent = self._find_code_history_row(code)
        if parent is None:
            raise ServiceError("归并目标调查丢失")
        ts = now_iso()
        child_no = self._new_case_no(conn)
        child_id = insert(
            conn,
            "INSERT INTO investigations(case_no, store_id, product_model, receipt_no,"
            " security_code, origin, customer_name, customer_contact, notify_channel,"
            " package_only, status, merged_into_id, duplicate_of_case_no,"
            " first_seen_at, first_seen_store_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_no, actor, product_model, receipt_no, code, "INTAKE",
             customer_name, customer_contact, notify_channel,
             1 if package_only else 0, parent["status"], parent["id"],
             parent["case_no"], parent["first_seen_at"], parent["first_seen_store_id"],
             ts, ts),
        )
        # 查询留痕记在原调查上——所有码查询始终关联同一原案
        self._log_query(conn, code=code, inv_id=parent["id"], store_id=actor,
                        result=history_result)
        # 顾客侧证物同样完整留存（挂原调查，来源 PACKAGING）
        product = query_one(conn, "SELECT * FROM products WHERE model=?", (product_model,))
        if product is not None:
            pkg_file = os.path.join(parent["case_no"], f"packaging-dup-{child_id}.svg")
            seal_file = os.path.join(parent["case_no"], f"seal-dup-{child_id}.svg")
            evidence_assets.packaging(
                os.path.join(self.evidence_dir, pkg_file),
                product_model, product["name"], lot_no or "（批次不清）",
                code or "（未提供）", damaged=label_damaged, package_only=package_only)
            evidence_assets.seal(os.path.join(self.evidence_dir, seal_file), intact=True)
            self._add_evidence(
                conn, parent["id"], SRC_PACKAGING,
                f"顾客外包装照片 · {store_name}（{child_no}）",
                f"重复码再次受理时的包装/防伪码留影；包装批次 {lot_no or '不清'}；"
                + ("仅外包装；" if package_only else "")
                + ("标签刮损；" if label_damaged else "标签可识读。"),
                file_rel=pkg_file, related_receipt=receipt_no, related_lot_no=lot_no)
            self._add_evidence(
                conn, parent["id"], SRC_PACKAGING,
                f"防伪封签照片 · {store_name}（{child_no}）",
                "封签完好。", file_rel=seal_file, related_lot_no=lot_no)
        self._add_evidence(
            conn, parent["id"], SRC_SYSTEM,
            f"重复防伪码再次出现 · 受理门店 {store_name}（{child_no}）",
            f"同一防伪码 {code} 由门店 {store_name} 再次受理；"
            f"首次出现于 {parent['first_seen_at']} / "
            f"{self._store_name(conn, parent['first_seen_store_id'])}。"
            "按一码多用并入本调查，不另立案件。",
            related_receipt=receipt_no, related_lot_no=lot_no)
        self._add_finding(
            conn, parent["id"], FIND_DUPLICATE,
            f"防伪码 {code} 重复出现：{store_name}（{child_no}）再次受理；"
            f"首次出现 {parent['first_seen_at']} 于"
            f" {self._store_name(conn, parent['first_seen_store_id'])}。")

        # 重复受理同样核对本店销售单：登记码不一致是“一码多用”的强信号
        if receipt_no:
            sr = query_one(conn, "SELECT * FROM sales WHERE receipt_no=?", (receipt_no,))
            if sr is not None:
                mismatch_parts: list[str] = []
                if sr["store_id"] != actor:
                    mismatch_parts.append("销售单非本店售出")
                if sr["security_code"] and norm_code(sr["security_code"]) != code:
                    mismatch_parts.append(
                        f"销售单登记防伪码 {sr['security_code']}，与顾客实物码 {code} 不一致")
                if mismatch_parts:
                    self._add_evidence(
                        conn, parent["id"], SRC_SALES,
                        f"重复受理销售单核对 · {receipt_no}（{child_no}）",
                        "；".join(mismatch_parts), related_receipt=receipt_no)
                    self._add_finding(
                        conn, parent["id"], FIND_SALES_MISMATCH,
                        f"{store_name} 受理 {child_no}：" + "；".join(mismatch_parts))

        # 新受理牵出的批次仍需在本门店冻结（批次可能早已关联原案，
        # 但本门店库存还是 ACTIVE）；_freeze_inventory 只冻结 ACTIVE 行，重复安全
        implicated_lot_ids: list[int] = []
        if lot_no:
            lr = query_one(conn, "SELECT id FROM supply_lots WHERE lot_no=? AND product_model=?",
                           (lot_no, product_model))
            if lr:
                implicated_lot_ids.append(lr["id"])
        if receipt_no:
            sr = query_one(conn, "SELECT lot_id FROM sales WHERE receipt_no=?", (receipt_no,))
            if sr:
                implicated_lot_ids.append(sr["lot_id"])
        implicated_lot_ids = sorted(set(implicated_lot_ids))
        n_frozen = 0
        if implicated_lot_ids:
            n_frozen = self._freeze_inventory(
                conn, inv_id=parent["id"], lot_ids=implicated_lot_ids,
                reason=f"重复码 {code} 在 {store_name} 再次受理，追加预防性冻结",
                actor=actor, store_scope=actor)
        if parent["status"] == ST_CLOSED:
            conn.execute(
                "UPDATE investigations SET status=?, closed_at=NULL, updated_at=? WHERE id=?",
                (ST_ESCALATED, ts, parent["id"]))
            conn.execute(
                "UPDATE investigations SET status=?, updated_at=? WHERE merged_into_id=?",
                (ST_ESCALATED, ts, parent["id"]))
            self._add_evidence(
                conn, parent["id"], SRC_HQ, "重复码触发重新复查",
                f"原结论已闭环，但防伪码在 {store_name} 再次出现，案件重新升级总部质控。")
        db.write_audit(conn, actor, "MERGE_DUPLICATE", "investigation", parent["id"],
                       {"child_case": child_no, "code": code,
                        "implicated_lots": implicated_lot_ids,
                        "frozen_rows": n_frozen,
                        "reopened": parent["status"] == ST_CLOSED})
        return {"merged": True, "investigation_id": parent["id"],
                "case_no": parent["case_no"], "child_case_no": child_no,
                "first_seen_at": parent["first_seen_at"],
                "first_seen_store_id": parent["first_seen_store_id"],
                "frozen_rows": n_frozen,
                "message": f"重复防伪码已归并到原调查 {parent['case_no']}"
                           f"（首次出现 {parent['first_seen_at']}）。"}

    def _store_name(self, conn, store_id: Optional[str]) -> str:
        if not store_id:
            return "未知门店"
        r = query_one(conn, "SELECT name FROM stores WHERE id=?", (store_id,))
        return r["name"] if r else store_id

    # ----------------------------------------------------------- 码查询

    def code_search(self, *, actor_store: str, code: Optional[str],
                    damaged_hint: bool = False, customer_name: Optional[str] = None,
                    customer_contact: Optional[str] = None) -> dict:
        """店员主动做防伪查询：留痕、归并旧案；陌生码建 QUERY 调查。"""
        with transaction(self.conn, immediate=True) as conn:
            self._store(conn, actor_store)
            c = norm_code(code)
            result = self.antifake.query(c, damaged_hint=damaged_hint)

            if result.status == CODE_DUPLICATE:
                parent = self._find_code_history_row(c)
                if parent is None:
                    raise ServiceError("归并目标调查丢失")
                # 仅查询留痕并挂原案，绝不生成互不相干的新案
                self._log_query(conn, code=c, inv_id=parent["id"],
                                store_id=actor_store, result=result)
                self._add_evidence(
                    conn, parent["id"], SRC_SYSTEM,
                    f"防伪码查询 · {self._store_name(conn, actor_store)}",
                    f"查询码 {c} 返回【重复】：首次出现于 "
                    f"{result.first_seen_at} / "
                    f"{self._store_name(conn, result.first_seen_store)}。查询已关联原调查。")
                db.write_audit(conn, actor_store, "CODE_QUERY_MERGE",
                               "investigation", parent["id"], {"code": c})
                return {"merged": True, "case_no": parent["case_no"],
                        "investigation_id": parent["id"],
                        "adapter": result.to_dict(),
                        "first_seen_at": result.first_seen_at,
                        "first_seen_store": result.first_seen_store,
                        "message": result.message}

            model = result.product_model or "UNKNOWN-MODEL"
            ts = now_iso()
            case_no = self._new_case_no(conn)
            inv_id = insert(
                conn,
                "INSERT INTO investigations(case_no, store_id, product_model, security_code,"
                " origin, customer_name, customer_contact, status, first_seen_at,"
                " first_seen_store_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_no, actor_store, model, c, "QUERY", customer_name,
                 customer_contact, ST_OPEN, ts, actor_store, ts, ts),
            )
            self._log_query(conn, code=c, inv_id=inv_id, store_id=actor_store, result=result)
            note = {
                CODE_OK: "防伪码首次查询且库内一致，暂无异常，保留监测。",
                CODE_DAMAGED: "防伪标签刮损无法完整识读，建议请顾客携实物到店受理。",
                CODE_UNKNOWN: "防伪平台查无此码，高度存疑，如顾客持物到店即转为正式受理。",
            }[result.status]
            self._add_evidence(conn, inv_id, SRC_SYSTEM, "防伪平台查询记录",
                               f"{result.message} {note}")
            if result.status == CODE_DAMAGED:
                self._add_finding(conn, inv_id, FIND_DAMAGED, note)
                conn.execute("UPDATE investigations SET status=?, updated_at=? WHERE id=?",
                             (ST_ESCALATED, now_iso(), inv_id))
            elif result.status == CODE_UNKNOWN:
                self._add_finding(conn, inv_id, "CODE_UNKNOWN", note)
            db.write_audit(conn, actor_store, "CODE_QUERY_NEW", "investigation", inv_id,
                           {"case_no": case_no, "status": result.status})
            return {"merged": False, "case_no": case_no, "investigation_id": inv_id,
                    "adapter": result.to_dict(), "message": result.message}

    # ----------------------------------------------------------- 总部复核

    def hq_review(self, *, actor_store: str, investigation_id: int, conclusion: str,
                  reason: str, decided_by: str) -> dict:
        """总部质控出具/改判结论。结论版本与库存冻结/解冻在同一事务提交。"""
        self._require_hq(actor_store)
        if conclusion not in CONCLUSION_TEXT:
            raise ServiceError("结论必须为 GENUINE / FAKE / INCONCLUSIVE")
        if not reason or not reason.strip():
            raise ServiceError("复查说明不能为空")
        with transaction(self.conn, immediate=True) as conn:
            inv = self._effective(conn, investigation_id)
            inv_id = inv["id"]
            new_version = inv["current_version"] + 1
            revised = new_version > 1
            ts = now_iso()
            insert(
                conn,
                "INSERT INTO conclusion_versions(investigation_id, version, conclusion,"
                " reason, decided_by, revised, supersedes_version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (inv_id, new_version, conclusion, reason.strip(), decided_by,
                 1 if revised else 0, inv["current_version"] or None, ts),
            )
            conn.execute(
                "UPDATE investigations SET current_conclusion=?, current_version=?,"
                " status=?, closed_at=?, updated_at=? WHERE id=?",
                (conclusion, new_version, ST_CLOSED, ts, ts, inv_id),
            )

            lot_ids = self.linked_lot_ids(conn, inv_id)
            n_unfrozen = n_frozen = 0
            stock_note = ""
            if conclusion == CONCL_GENUINE:
                n_unfrozen = self._unfreeze_inventory(
                    conn, inv_id=inv_id, lot_ids=lot_ids,
                    reason=f"总部 V{new_version} 复查确认为真品，解除冻结恢复销售",
                    actor=STORE_HQ, version=new_version)
                stock_note = f"解除冻结库存 {n_unfrozen} 条，恢复销售。"
            elif conclusion == CONCL_FAKE:
                # 维持冻结并确保同批连锁库存全部冻结
                n_frozen = self._freeze_inventory(
                    conn, inv_id=inv_id, lot_ids=lot_ids,
                    reason=f"总部 V{new_version} 复查认定假冒，批次继续冻结待处置",
                    actor=STORE_HQ, store_scope=None, version=new_version)
                stock_note = f"维持冻结并扩冻连锁同批库存 {n_frozen} 条，等待处置。"
            else:
                n_frozen = self._freeze_inventory(
                    conn, inv_id=inv_id, lot_ids=lot_ids,
                    reason=f"总部 V{new_version} 复查无法认定，库存继续冻结待补证",
                    actor=STORE_HQ, store_scope=None, version=new_version)
                stock_note = f"证据不足无法认定，继续冻结库存（新增冻结 {n_frozen} 条）。"

            hq_file = f"hq-v{new_version}-{inv_id}.svg"
            evidence_assets.hq_report(
                self._evidence_path(inv["case_no"], hq_file),
                inv["case_no"], conclusion, new_version, reason.strip())
            title = f"总部质控复查意见书 V{new_version}" + ("（改判）" if revised else "")
            self._add_evidence(
                conn, inv_id, SRC_HQ, title,
                f"结论：{CONCLUSION_TEXT[conclusion]}。{reason.strip()} {stock_note}"
                + (f" 本版本取代 V{new_version - 1}，历史版本保留可查。" if revised else ""),
                file_rel=os.path.join(inv["case_no"], hq_file))
            if revised:
                prev = query_one(
                    conn,
                    "SELECT conclusion FROM conclusion_versions"
                    " WHERE investigation_id=? AND version=?",
                    (inv_id, new_version - 1))
                self._add_finding(
                    conn, inv_id, "CONCLUSION_REVISED",
                    f"总部改判：V{new_version - 1}（{CONCLUSION_TEXT.get(prev['conclusion'], '?')}）"
                    f" → V{new_version}（{CONCLUSION_TEXT[conclusion]}）。完整证据与旧版结论均保留。")

            db.write_audit(conn, STORE_HQ, "HQ_REVIEW", "investigation", inv_id,
                           {"version": new_version, "conclusion": conclusion,
                            "revised": revised, "unfrozen": n_unfrozen,
                            "frozen": n_frozen})

            notified = None
            if inv["customer_contact"]:
                notified = self._send_conclusion_notification(
                    conn, inv, new_version, revised, conclusion)
            return {"investigation_id": inv_id, "case_no": inv["case_no"],
                    "version": new_version, "revised": revised,
                    "conclusion": conclusion, "unfrozen": n_unfrozen,
                    "frozen": n_frozen, "notification": notified}

    def _send_conclusion_notification(self, conn, inv: sqlite3.Row, version: int,
                                      revised: bool, conclusion: str) -> dict:
        channel = inv["notify_channel"] or "SMS"
        subject = f"商品真伪复查结论（{inv['case_no']} V{version}）"
        prefix = "【复查改判】" if revised else "【复查结论】"
        body = (f"{prefix}您反映的商品（型号 {inv['product_model']}，调查编号 "
                f"{inv['case_no']}）经总部质量管理中心复查，当前 V{version} 结论为："
                f"{CONCLUSION_TEXT[conclusion]}。"
                "调查证据与历版结论均已留存，如有疑问可凭编号到店查询。")
        res = self.notifier.send(channel, inv["customer_contact"], subject, body)
        nid = insert(
            conn,
            "INSERT INTO notifications(investigation_id, channel, destination, subject,"
            " body, status, provider_ref, conclusion_version, created_at, sent_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (inv["id"], channel, inv["customer_contact"], subject, body, NTF_SENT,
             res["provider_ref"], version, now_iso(), now_iso()),
        )
        return {"id": nid, "channel": channel, "provider_ref": res["provider_ref"]}

    def notify_customer(self, *, actor_store: str, investigation_id: int,
                        channel: Optional[str] = None) -> dict:
        """门店手动告知：只能基于总部已生效结论，不得自行解读。"""
        with transaction(self.conn, immediate=True) as conn:
            inv = self._effective(conn, investigation_id)
            self._require_case_owner(conn, inv, actor_store)
            if not inv["current_conclusion"]:
                raise ServiceError("总部尚未出具结论，门店不能自行告知判定结果。")
            if not inv["customer_contact"]:
                raise ServiceError("未登记顾客联系方式。")
            ch = channel or inv["notify_channel"] or "SMS"
            subject = f"商品真伪复查结论（{inv['case_no']} V{inv['current_version']}）"
            body = (f"您反映的商品（型号 {inv['product_model']}，调查编号 {inv['case_no']}）"
                    f"总部质控 V{inv['current_version']} 结论为："
                    f"{CONCLUSION_TEXT[inv['current_conclusion']]}。"
                    "本告知由门店根据总部结论代为转达，证据全程留档。")
            res = self.notifier.send(ch, inv["customer_contact"], subject, body)
            nid = insert(
                conn,
                "INSERT INTO notifications(investigation_id, channel, destination, subject,"
                " body, status, provider_ref, conclusion_version, created_at, sent_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (inv["id"], ch, inv["customer_contact"], subject, body, NTF_SENT,
                 res["provider_ref"], inv["current_version"], now_iso(), now_iso()),
            )
            db.write_audit(conn, actor_store, "NOTIFY", "investigation", inv["id"],
                           {"notification_id": nid, "channel": ch})
            return {"id": nid, "provider_ref": res["provider_ref"], "channel": ch}

    def store_attempt_overwrite(self, *, actor_store: str, investigation_id: int,
                                conclusion: str, reason: str) -> None:
        """显式表达门店越权路径：任何门店身份调用都被拒绝（用于权限演示/测试）。"""
        self._require_hq(actor_store)  # 抛出 PermissionError_
        # 即便 actor 是 HQ 也必须走正式复核通道
        raise ServiceError("请通过总部复核流程出具结论。")

    # ----------------------------------------------------------- 查询聚合

    def list_investigations(self, *, frozen_only: bool = False) -> list[dict]:
        sql = (
            "SELECT i.*, s.name AS store_name,"
            " (SELECT COUNT(*) FROM investigation_lots il"
            "  JOIN inventory inv ON inv.lot_id=il.lot_id"
            "  WHERE il.investigation_id=i.id AND inv.status='FROZEN') AS frozen_lot_count,"
            " (SELECT COUNT(*) FROM findings f WHERE f.investigation_id=i.id) AS finding_count"
            " FROM investigations i JOIN stores s ON s.id=i.store_id"
            " WHERE i.merged_into_id IS NULL")
        if frozen_only:
            sql += " AND frozen_lot_count > 0"
        sql += " ORDER BY i.id DESC"
        rows = query_all(self.conn, sql)
        out = db.rows_to_dicts(rows)
        for r in out:
            r["conclusion_text"] = CONCLUSION_TEXT.get(r["current_conclusion"])
        return out

    def list_merged_children(self, conn, inv_id: int) -> list[dict]:
        return db.rows_to_dicts(query_all(
            conn,
            "SELECT c.*, s.name AS store_name FROM investigations c"
            " JOIN stores s ON s.id=c.store_id WHERE c.merged_into_id=? ORDER BY c.id",
            (inv_id,)))

    def detail(self, investigation_id: int) -> dict:
        with transaction(self.conn) as conn:
            row = query_one(conn, "SELECT * FROM investigations WHERE id=?",
                            (investigation_id,))
            if row is None:
                raise ServiceError("调查不存在")
            if row["merged_into_id"]:
                parent = self._effective(conn, row["id"])
                data = self._detail_effective(conn, parent["id"])
                data["redirected_from"] = {"case_no": row["case_no"],
                                           "store_id": row["store_id"]}
                return data
            return self._detail_effective(conn, row["id"])

    def _detail_effective(self, conn, inv_id: int) -> dict:
        inv = query_one(
            conn,
            "SELECT i.*, s.name AS store_name, fs.name AS first_seen_store_name"
            " FROM investigations i JOIN stores s ON s.id=i.store_id"
            " LEFT JOIN stores fs ON fs.id=i.first_seen_store_id WHERE i.id=?",
            (inv_id,))
        data = dict(inv)
        data["conclusion_text"] = CONCLUSION_TEXT.get(inv["current_conclusion"])
        data["findings"] = db.rows_to_dicts(query_all(
            conn, "SELECT * FROM findings WHERE investigation_id=? ORDER BY id", (inv_id,)))
        data["versions"] = db.rows_to_dicts(query_all(
            conn, "SELECT * FROM conclusion_versions WHERE investigation_id=?"
                  " ORDER BY version", (inv_id,)))
        for v in data["versions"]:
            v["conclusion_text"] = CONCLUSION_TEXT.get(v["conclusion"])
        data["queries"] = db.rows_to_dicts(query_all(
            conn,
            "SELECT q.*, s.name AS store_name FROM code_queries q"
            " JOIN stores s ON s.id=q.store_id WHERE q.investigation_id=?"
            " ORDER BY q.id", (inv_id,)))
        data["children"] = self.list_merged_children(conn, inv_id)
        ev = query_all(
            conn,
            "SELECT e.*, CASE WHEN e.file_path IS NOT NULL THEN 1 ELSE 0 END AS has_file"
            " FROM evidence e WHERE e.investigation_id=? ORDER BY e.id", (inv_id,))
        data["evidence"] = db.rows_to_dicts(ev)
        data["evidence_by_source"] = {
            src: [dict(e) for e in ev if e["source"] == src]
            for src in db.EVIDENCE_SOURCES}
        data["notifications"] = db.rows_to_dicts(query_all(
            conn, "SELECT * FROM notifications WHERE investigation_id=? ORDER BY id",
            (inv_id,)))
        lots = query_all(
            conn,
            "SELECT sl.*, inv.id AS inventory_id, inv.store_id AS inv_store_id,"
            " st.name AS inv_store_name, inv.status AS inv_status, inv.qty_total,"
            " inv.qty_sold, inv.frozen_at, inv.frozen_reason"
            " FROM investigation_lots il JOIN supply_lots sl ON sl.id=il.lot_id"
            " LEFT JOIN inventory inv ON inv.lot_id=sl.id"
            " LEFT JOIN stores st ON st.id=inv.store_id"
            " WHERE il.investigation_id=? ORDER BY sl.id, inv.store_id", (inv_id,))
        data["lots"] = db.rows_to_dicts(lots)
        freezes = query_all(
            conn,
            "SELECT f.*, i.store_id AS inventory_store_id, sl.lot_no"
            " FROM inventory_freezes f JOIN inventory i ON i.id=f.inventory_id"
            " JOIN supply_lots sl ON sl.id=i.lot_id"
            " WHERE f.investigation_id=? ORDER BY f.id", (inv_id,))
        data["freezes"] = db.rows_to_dicts(freezes)
        return data

    def list_inventory(self, *, status: Optional[str] = None,
                       store_id: Optional[str] = None) -> list[dict]:
        sql = ("SELECT inv.id AS inventory_id, inv.store_id, inv.lot_id, inv.qty_total,"
               " inv.qty_sold, inv.status, inv.frozen_at, inv.frozen_reason,"
               " sl.lot_no, sl.product_model, p.name AS product_name,"
               " s.name AS store_name, sup.name AS supplier_name"
               " FROM inventory inv JOIN supply_lots sl ON sl.id=inv.lot_id"
               " JOIN products p ON p.model=sl.product_model"
               " JOIN stores s ON s.id=inv.store_id"
               " JOIN suppliers sup ON sup.id=sl.supplier_id WHERE 1=1")
        params: list[Any] = []
        if status:
            sql += " AND inv.status=?"; params.append(status)
        if store_id:
            sql += " AND inv.store_id=?"; params.append(store_id)
        sql += " ORDER BY inv.status DESC, sl.lot_no, s.id"
        return db.rows_to_dicts(query_all(self.conn, sql, params))

    def stores(self) -> list[dict]:
        return db.rows_to_dicts(query_all(
            self.conn, "SELECT * FROM stores ORDER BY is_hq DESC, id"))
