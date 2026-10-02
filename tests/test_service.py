# -*- coding: utf-8 -*-
"""核心规则测试：调查归并、库存冻结竞态、结论版本、门店权限、证据留存。

不依赖演示种子数据，每个用例自建最小数据集。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from app import db
from app.db import (
    CONCL_FAKE, CONCL_GENUINE, CONCL_INCONCLUSIVE, FIND_DAMAGED,
    FIND_DUPLICATE, FIND_PACKAGE_ONLY, FIND_SALES_MISMATCH, FIND_SUPPLIER_DENY,
    LOT_ACTIVE, LOT_FROZEN, ST_CLOSED, ST_ESCALATED, insert, query_one,
)
from app.services import LotFrozenError, PermissionError_, Service, ServiceError


class Fixture:
    """每个用例一个独立临时库。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="auth-test-")
        self.db_path = os.path.join(self.dir, "t.db")
        self.evidence_dir = os.path.join(self.dir, "evidence")
        os.makedirs(self.evidence_dir, exist_ok=True)
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        self._master_data()

    def service(self, conn=None) -> Service:
        return Service(conn or self.conn, self.evidence_dir)

    def new_conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def _master_data(self):
        with db.transaction(self.conn) as c:
            for sid, name, hq in [("S01", "一店", 0), ("S02", "二店", 0),
                                  ("S03", "三店", 0), ("HQ", "总部", 1)]:
                insert(c, "INSERT INTO stores(id,name,is_hq,created_at) VALUES(?,?,?,?)",
                       (sid, name, hq, "2026-10-01T09:00:00+08:00"))
            insert(c, "INSERT INTO suppliers(id,name,created_at) VALUES(?,?,?)",
                   ("SUP", "供应商甲", "2026-10-01T09:00:00+08:00"))
            insert(c, "INSERT INTO products(model,name,supplier_id,created_at) VALUES(?,?,?,?)",
                   ("P1", "商品一", "SUP", "2026-10-01T09:00:00+08:00"))

    def add_lot(self, lot_no: str, *, confirmed: bool = True) -> int:
        with db.transaction(self.conn) as c:
            return insert(
                c,
                "INSERT INTO supply_lots(lot_no,product_model,supplier_id,shipped_at,"
                " supplier_confirmed,created_at) VALUES(?,?,?,?,?,?)",
                (lot_no, "P1", "SUP", "2026-09-20T09:00:00+08:00",
                 1 if confirmed else 0, "2026-10-01T09:00:00+08:00"))

    def add_inventory(self, store: str, lot_id: int, total: int, sold: int = 0,
                      status: str = LOT_ACTIVE) -> None:
        with db.transaction(self.conn) as c:
            insert(c, "INSERT INTO inventory(store_id,lot_id,qty_total,qty_sold,status,created_at)"
                      " VALUES(?,?,?,?,?,?)",
                   (store, lot_id, total, sold, status, "2026-10-01T09:00:00+08:00"))

    def add_sale(self, receipt: str, store: str, lot_id: int, code: str | None) -> None:
        with db.transaction(self.conn) as c:
            insert(c, "INSERT INTO sales(receipt_no,store_id,lot_id,product_model,security_code,"
                      " customer_name,sold_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (receipt, store, lot_id, "P1", code, "顾客",
                    "2026-09-25T10:00:00+08:00", "2026-09-25T10:00:00+08:00"))

    def add_code(self, code: str, lot_id: int, first_store: str, damaged: bool = False) -> None:
        with db.transaction(self.conn) as c:
            insert(c, "INSERT INTO security_code_registry(code,product_model,lot_id,"
                      " first_store_id,damaged,note) VALUES(?,?,?,?,?,?)",
                   (code, "P1", lot_id, first_store, 1 if damaged else 0, None))

    def close(self):
        self.conn.close()


class MergeTests(unittest.TestCase):
    """同一防伪码多次查询必须归并到原调查。"""

    def setUp(self):
        self.fx = Fixture()
        lot = self.fx.add_lot("L1")
        self.fx.add_inventory("S01", lot, 10)
        self.fx.add_inventory("S02", lot, 10)
        self.fx.add_sale("R1", "S01", lot, "CODE-X")
        self.fx.add_sale("R2", "S02", lot, "OTHER-CODE")
        self.fx.add_code("CODE-X", lot, "S01")
        self.lot_id = lot

    def tearDown(self):
        self.fx.close()

    def test_intake_then_intake_same_code_merges(self):
        svc = self.fx.service()
        first = svc.intake(actor_store="S01", product_model="P1", receipt_no="R1",
                           security_code="CODE-X", lot_no="L1",
                           customer_name="甲", customer_contact="13800000001")
        second = svc.intake(actor_store="S02", product_model="P1", receipt_no="R2",
                            security_code="code-x", lot_no="L1",
                            customer_name="乙", customer_contact="13800000002")
        # 第二次受理明确归并，不产生第二个有效案件
        self.assertTrue(second["merged"])
        self.assertEqual(second["investigation_id"], first["investigation_id"])
        self.assertNotEqual(second["child_case_no"], first["case_no"])

        effective = self.fx.conn.execute(
            "SELECT COUNT(*) c FROM investigations WHERE merged_into_id IS NULL").fetchone()["c"]
        self.assertEqual(effective, 1)

        parent = svc.detail(first["investigation_id"])
        self.assertEqual(len(parent["children"]), 1)
        self.assertEqual(parent["children"][0]["store_id"], "S02")
        # 首次出现的时间与门店保留在原案
        self.assertEqual(parent["first_seen_store_id"], "S01")
        # 重复码发现 + 销售单登记码不一致发现都在原案
        kinds = {f["kind"] for f in parent["findings"]}
        self.assertIn(FIND_DUPLICATE, kinds)
        self.assertIn(FIND_SALES_MISMATCH, kinds)

    def test_code_search_never_creates_orphan_cases(self):
        svc = self.fx.service()
        svc.intake(actor_store="S01", product_model="P1", receipt_no="R1",
                   security_code="CODE-X", lot_no="L1")
        # 两个不同门店各查一次同一码
        q2 = svc.code_search(actor_store="S02", code="CODE-X")
        q3 = svc.code_search(actor_store="S03", code="CODE-X")
        self.assertTrue(q2["merged"] and q3["merged"])
        effective = self.fx.conn.execute(
            "SELECT COUNT(*) c FROM investigations WHERE merged_into_id IS NULL").fetchone()["c"]
        self.assertEqual(effective, 1)
        # 三次查询全部挂在同一个调查下
        nq = self.fx.conn.execute(
            "SELECT COUNT(*) c FROM code_queries q JOIN investigations i ON i.id=q.investigation_id"
            " WHERE i.merged_into_id IS NULL").fetchone()["c"]
        self.assertEqual(nq, 3)

    def test_closed_case_reopens_on_duplicate(self):
        svc = self.fx.service()
        r = svc.intake(actor_store="S01", product_model="P1", receipt_no="R1",
                       security_code="CODE-X", lot_no="L1",
                       customer_contact="13800000001")
        svc.hq_review(actor_store="HQ", investigation_id=r["investigation_id"],
                      conclusion=CONCL_GENUINE, decided_by="质控员", reason="首查一致，判真。")
        self.assertEqual(svc.detail(r["investigation_id"])["status"], ST_CLOSED)
        # 同码再现：原案重新升级等待总部改判，结论版本暂不增加
        svc.intake(actor_store="S02", product_model="P1", receipt_no="R2",
                   security_code="CODE-X", lot_no="L1")
        d = svc.detail(r["investigation_id"])
        self.assertEqual(d["status"], ST_ESCALATED)
        self.assertEqual(d["current_version"], 1)
        self.assertTrue(any("重新复查" in e["title"] for e in d["evidence"]))


class FreezeRaceTests(unittest.TestCase):
    """冻结库存与销售出库互斥；竞态下疑似批次一单都卖不出去。"""

    def setUp(self):
        self.fx = Fixture()
        self.lot = self.fx.add_lot("LRACE")
        self.fx.lot = self.lot  # 供并发测试中的独立线程经 fixture 访问
        self.fx.add_inventory("S01", self.lot, 1000, 0)

    def tearDown(self):
        self.fx.close()

    def test_frozen_lot_blocks_sale_directly(self):
        svc = self.fx.service()
        # 制造一个可疑调查并冻结 S01 的该批次
        self.fx.add_code("CODE-RACE", self.lot, "S01", damaged=True)
        r = svc.intake(actor_store="S01", product_model="P1", receipt_no=None,
                       security_code="CODE-RACE", lot_no="LRACE", label_damaged=True)
        self.assertEqual(r["frozen_rows"], 1)
        with self.assertRaises(LotFrozenError):
            svc.sell(store_id="S01", receipt_no="RX", lot_id=self.lot, product_model="P1")

    def test_concurrent_freeze_vs_sells_no_sale_after_freeze(self):
        """多连接并发：冻结提交后获得锁的销售事务必须全部被拒。"""
        fx = self.fx
        start = threading.Event()
        stop = threading.Event()
        freeze_done = threading.Event()
        results: list[tuple[str, bool]] = []
        results_lock = threading.Lock()

        def seller(idx: int):
            conn = fx.new_conn()
            start.wait()
            try:
                svc = fx.service(conn)
                k = 0
                while not stop.is_set():
                    # 冻结完成后继续密集尝试，验证无一漏网
                    try:
                        svc.sell(store_id="S01", receipt_no=f"R-T{idx}-{k}",
                                 lot_id=fx.lot, product_model="P1")
                        allowed = True
                    except LotFrozenError:
                        allowed = False
                    except ServiceError:
                        allowed = False  # 售罄等
                    with results_lock:
                        results.append(("after" if freeze_done.is_set() else "before",
                                        allowed))
                    k += 1
            finally:
                conn.close()

        def freezer():
            import time
            time.sleep(0.15)  # 让销售先跑一段时间
            conn = fx.new_conn()
            try:
                svc = fx.service(conn)
                fx.add_code("CODE-RACE", fx.lot, "S01", damaged=True)
                r = svc.intake(actor_store="S01", product_model="P1", receipt_no=None,
                               security_code="CODE-RACE", lot_no="LRACE",
                               label_damaged=True)
                assert r["frozen_rows"] == 1
                freeze_done.set()
            finally:
                conn.close()

        threads = [threading.Thread(target=seller, args=(i,)) for i in range(8)]
        fz = threading.Thread(target=freezer)
        for t in threads:
            t.start()
        fz.start()
        start.set()
        fz.join()
        # 冻结提交后继续压测 0.5 秒
        import time
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join()

        after = [ok for phase, ok in results if phase == "after"]
        self.assertTrue(after, "应存在冻结完成后的销售尝试")
        self.assertFalse(any(after), "冻结提交后不得再有任何销售成功（疑似批次不能售出）")

        # 库存账实一致：已售 + 剩余 = 总量
        inv = query_one(self.fx.conn,
                        "SELECT * FROM inventory WHERE store_id='S01' AND lot_id=?",
                        (self.lot,))
        self.assertEqual(inv["status"], LOT_FROZEN)
        self.assertLessEqual(inv["qty_sold"], inv["qty_total"])

    def test_freeze_and_conclusion_commit_together(self):
        """结论 GENUINE 与解冻必须同事务：不可能出现“已判真却仍冻结”之外的中间态被读到。

        用两个连接直接观察：复核事务提交前，其他连接看到的仍是 FROZEN；
        提交后一次性变为 ACTIVE + 结论 V1。
        """
        fx = self.fx
        svc = fx.service()
        # 用一个刮损码触发冻结
        fx.add_code("CODE-DMG-G", fx.lot, "S01", damaged=True)
        r = svc.intake(actor_store="S01", product_model="P1", receipt_no=None,
                       security_code="CODE-DMG-G", lot_no="LRACE", label_damaged=True)
        inv_id = r["investigation_id"]
        self.assertEqual(query_one(fx.conn,
            "SELECT status FROM inventory WHERE lot_id=?", (fx.lot,))["status"], LOT_FROZEN)

        # 另一个连接在复核前读到冻结且无结论
        reader = fx.new_conn()
        self.assertIsNone(query_one(reader,
            "SELECT current_conclusion c FROM investigations WHERE id=?", (inv_id,))["c"])
        reader.close()

        svc.hq_review(actor_store="HQ", investigation_id=inv_id,
                      conclusion=CONCL_GENUINE, decided_by="质控",
                      reason="复查无异常，真品。")
        # 提交后：结论与解冻同时生效
        self.assertEqual(query_one(fx.conn,
            "SELECT status FROM inventory WHERE lot_id=?", (fx.lot,))["status"], LOT_ACTIVE)
        self.assertEqual(query_one(fx.conn,
            "SELECT current_conclusion c FROM investigations WHERE id=?",
            (inv_id,))["c"], CONCL_GENUINE)
        # 冻结流水同时包含 FREEZE 与 UNFREEZE，且解冻带结论版本号
        actions = fx.conn.execute(
            "SELECT action, conclusion_version FROM inventory_freezes"
            " WHERE investigation_id=? ORDER BY id", (inv_id,)).fetchall()
        self.assertEqual([a["action"] for a in actions], ["FREEZE", "UNFREEZE"])
        self.assertEqual(actions[1]["conclusion_version"], 1)


class ConclusionVersionTests(unittest.TestCase):
    """复查改判生成新版本；历史版本与证据完整保留；门店不得改总部结论。"""

    def setUp(self):
        self.fx = Fixture()
        self.lot = self.fx.add_lot("LV")
        self.fx.add_inventory("S01", self.lot, 10)
        self.fx.add_inventory("S02", self.lot, 10)
        self.fx.add_code("CODE-V", self.lot, "S01")

    def tearDown(self):
        self.fx.close()

    def _open(self):
        svc = self.fx.service()
        return svc.intake(actor_store="S01", product_model="P1", receipt_no=None,
                          security_code="CODE-V", lot_no="LV",
                          customer_contact="13800000001")

    def test_revision_keeps_history_and_versions(self):
        svc = self.fx.service()
        inv_id = self._open()["investigation_id"]
        svc.hq_review(actor_store="HQ", investigation_id=inv_id,
                      conclusion=CONCL_GENUINE, decided_by="周质控",
                      reason="V1：初步判真，解除冻结。")
        v2 = svc.hq_review(actor_store="HQ", investigation_id=inv_id,
                           conclusion=CONCL_FAKE, decided_by="林质控",
                           reason="V2：同码在二店再现，证据互证为套码假冒，改判。")
        self.assertTrue(v2["revised"])
        self.assertEqual(v2["version"], 2)

        versions = self.fx.conn.execute(
            "SELECT version, conclusion, revised, supersedes_version, reason"
            " FROM conclusion_versions WHERE investigation_id=? ORDER BY version",
            (inv_id,)).fetchall()
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["conclusion"], CONCL_GENUINE)
        self.assertEqual(versions[1]["conclusion"], CONCL_FAKE)
        self.assertEqual(versions[1]["supersedes_version"], 1)
        # V1 原文仍完整可读
        self.assertIn("初步判真", versions[0]["reason"])

        d = svc.detail(inv_id)
        self.assertEqual(d["current_version"], 2)
        self.assertEqual(d["current_conclusion"], CONCL_FAKE)
        # 改判本身留痕
        self.assertTrue(any(f["kind"] == "CONCLUSION_REVISED" for f in d["findings"]))
        # 假冒结论把连锁同批库存全部冻结（S01 已在 V1 解冻，V2 重新冻结）
        stores_frozen = {r["store_id"] for r in self.fx.conn.execute(
            "SELECT store_id FROM inventory WHERE lot_id=? AND status='FROZEN'",
            (self.lot,))}
        self.assertEqual(stores_frozen, {"S01", "S02"})

    def test_conclusion_requires_reason(self):
        svc = self.fx.service()
        inv_id = self._open()["investigation_id"]
        with self.assertRaises(ServiceError):
            svc.hq_review(actor_store="HQ", investigation_id=inv_id,
                          conclusion=CONCL_FAKE, decided_by="x", reason="  ")

    def test_inconclusive_keeps_frozen(self):
        svc = self.fx.service()
        inv_id = self._open()["investigation_id"]
        svc.hq_review(actor_store="HQ", investigation_id=inv_id,
                      conclusion=CONCL_INCONCLUSIVE, decided_by="周质控",
                      reason="证据不足，无法认定。")
        self.assertEqual(query_one(self.fx.conn,
            "SELECT status FROM inventory WHERE lot_id=?", (self.lot,))["status"],
            LOT_FROZEN)


class PermissionTests(unittest.TestCase):
    """门店不能自行修改总部结论；门店只能操作本店调查。"""

    def setUp(self):
        self.fx = Fixture()
        self.lot = self.fx.add_lot("LP")
        self.fx.add_inventory("S01", self.lot, 10)
        self.fx.add_code("CODE-P", self.lot, "S01")
        self.svc = self.fx.service()
        r = self.svc.intake(actor_store="S01", product_model="P1", receipt_no=None,
                            security_code="CODE-P", lot_no="LP",
                            customer_contact="13800000001")
        self.inv_id = r["investigation_id"]

    def tearDown(self):
        self.fx.close()

    def test_store_cannot_issue_conclusion(self):
        for actor in ("S01", "S02"):
            with self.assertRaises(PermissionError_):
                self.svc.hq_review(actor_store=actor, investigation_id=self.inv_id,
                                   conclusion=CONCL_FAKE, decided_by="店员",
                                   reason="门店自己判假")

    def test_store_cannot_use_overwrite_path(self):
        with self.assertRaises(PermissionError_):
            self.svc.store_attempt_overwrite(actor_store="S01",
                                             investigation_id=self.inv_id,
                                             conclusion=CONCL_FAKE, reason="x")

    def test_store_cannot_notify_other_stores_case(self):
        with self.assertRaises(PermissionError_):
            self.svc.notify_customer(actor_store="S02", investigation_id=self.inv_id)

    def test_store_cannot_notify_before_hq_conclusion(self):
        # 本店也不行：总部未出结论前门店不得自行告知判定
        with self.assertRaises(ServiceError):
            self.svc.notify_customer(actor_store="S01", investigation_id=self.inv_id)

    def test_owner_store_can_notify_after_hq(self):
        self.svc.hq_review(actor_store="HQ", investigation_id=self.inv_id,
                           conclusion=CONCL_GENUINE, decided_by="质控", reason="真品。")
        n = self.svc.notify_customer(actor_store="S01", investigation_id=self.inv_id)
        self.assertTrue(n["provider_ref"].startswith("MOCK-"))


class EvidenceRetentionTests(unittest.TestCase):
    """六类异常情形都要保留完整证据与比对发现。"""

    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_all_exception_paths_leave_evidence(self):
        # 1) 刮损 + 仅外包装
        lot1 = self.fx.add_lot("LD")
        self.fx.add_inventory("S01", lot1, 5)
        self.fx.add_code("CODE-D", lot1, "S01", damaged=True)
        self.fx.add_sale("RD", "S01", lot1, "CODE-D")
        svc = self.fx.service()
        r = svc.intake(actor_store="S01", product_model="P1", receipt_no="RD",
                       security_code="CODE-D", lot_no="LD",
                       package_only=True, label_damaged=True)
        d = svc.detail(r["investigation_id"])
        kinds = {f["kind"] for f in d["findings"]}
        self.assertIn(FIND_DAMAGED, kinds)
        self.assertIn(FIND_PACKAGE_ONLY, kinds)
        self.assertTrue(any(e["source"] == "PACKAGING" for e in d["evidence"]))

        # 2) 供应商否认批次（批次存在但 supplier_confirmed=0）
        lot2 = self.fx.add_lot("LDENY", confirmed=False)
        self.fx.add_inventory("S02", lot2, 5)
        r2 = svc.intake(actor_store="S02", product_model="P1",
                        receipt_no="R-NOPE", security_code="CODE-NOPE",
                        lot_no="LDENY")
        d2 = svc.detail(r2["investigation_id"])
        kinds2 = {f["kind"] for f in d2["findings"]}
        self.assertIn(FIND_SUPPLIER_DENY, kinds2)
        self.assertIn(FIND_SALES_MISMATCH, kinds2)  # 销售单不存在
        self.assertTrue(any("否认" in e["title"] or "否认" in (e["detail"] or "")
                            for e in d2["evidence"]))

        # 3) 重复码（已有 CODE-D 案件，再查同码归并且留证据）
        q = svc.code_search(actor_store="S02", code="CODE-D")
        self.assertTrue(q["merged"])
        d3 = svc.detail(q["investigation_id"])
        self.assertTrue(any(e["source"] == "SYSTEM" and "查询" in e["title"]
                            for e in d3["evidence"]))


if __name__ == "__main__":
    unittest.main()
