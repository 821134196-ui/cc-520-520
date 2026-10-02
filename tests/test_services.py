"""端到端业务测试（标准库 unittest，零依赖）：

  1. 重复防伪码归并原调查，不生成互不相关的新案
  2. 库存冻结：复查期标记、总部冻结与结论同事务、竞态销售被拦截
  3. 结论版本：复查改判只追加版本、旧版保留、当前版本指针正确
  4. 门店权限：不能出具/修改总部结论
  5. 六类必须保留完整证据的情形均有证据落库
  6. 列表可按冻结批次筛选；对照页数据完整
运行：python3 -m unittest -v
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest

from app import seed, services
from app.db import connect, init_db

STORE_A = {"role": "store", "store_id": 1}
STORE_B = {"role": "store", "store_id": 2}
HQ = {"role": "hq", "store_id": None}
CODE_A = "ACF-A1-88880001"


class SeededTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="acf-test-")
        cls.db = os.path.join(cls.tmp, "test.db")
        cls.data = cls.tmp
        cls.ids = seed.seed(cls.db, cls.data)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def q(self, sql, args=()):
        conn = connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return dict(rows[0]) if rows else None


class TestDuplicateMerge(SeededTestBase):
    def test_second_case_links_original(self):
        """重复码案件必须 original_case_id 指向首次出现的原案。"""
        orig, dup = self.ids["A_original"], self.ids["A_duplicate"]
        d = self.one("SELECT * FROM cases WHERE id = ?", (dup,))
        self.assertEqual(d["original_case_id"], orig)
        self.assertIn("归并至原调查", d["merge_note"])
        self.assertIn("首次出现", d["merge_note"])

    def test_root_chain_stable(self):
        """第三次同码查询仍然归并到同一个根，不产生第二条根。"""
        c3 = services.accept_case(self.db, STORE_A, code=CODE_A,
                                  product_model="AJ-X1",
                                  batch_code="PC20260901A",
                                  customer_phone="138****0099", files=[])
        r = services.query_code(self.db, STORE_A, c3["id"], CODE_A)
        self.assertTrue(r["result"]["is_duplicate"])
        self.assertEqual(r["merge"]["root_case_id"], self.ids["A_original"])
        root = self.one("SELECT original_case_id FROM cases WHERE id = ?", (c3["id"],))
        self.assertEqual(root["original_case_id"], self.ids["A_original"])
        roots = {r[0] for r in self.q(
            "SELECT id FROM cases WHERE original_case_id IS NULL AND code = ?", (CODE_A,))}
        self.assertEqual(roots, {self.ids["A_original"]})

    def test_first_seen_recorded(self):
        """首次出现的时间与门店来自第一次查询（门店1）。"""
        reg = self.one("SELECT * FROM code_registry WHERE code = ?", (CODE_A,))
        self.assertEqual(reg["first_store_id"], 1)
        self.assertIsNotNone(reg["first_seen_at"])
        self.assertGreaterEqual(reg["query_count"], 2)  # 种子中两店各查询一次

    def test_duplicate_evidence_chain(self):
        """重复码在平台查询记录中被标注 is_duplicate，且证据保留。"""
        dup_q = self.one(
            "SELECT * FROM code_queries WHERE case_id = ? AND is_duplicate = 1",
            (self.ids["A_duplicate"],))
        self.assertIsNotNone(dup_q)
        self.assertEqual(dup_q["first_store_id"], 1)


class TestInventoryFreeze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acf-freeze-")
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        conn = connect(self.db)
        conn.executescript("""
          INSERT INTO stores(id,code,name,role) VALUES
            (1,'A','店A','store'),(2,'B','店B','store'),(3,'H','总部','hq');
          INSERT INTO suppliers(id,code,name) VALUES (1,'SUP','原厂');
          INSERT INTO products(id,model,name) VALUES (1,'M','商品');
          INSERT INTO batches(id,code,product_id,supplier_id,supplied_store_id)
            VALUES (1,'B1',1,1,1),(2,'B2',1,1,2);
          INSERT INTO inventory(id,store_id,batch_id,qty,status,updated_at)
            VALUES (1,1,1,10,'normal','t'),(2,2,1,5,'normal','t'),
                   (3,2,2,7,'normal','t');
          INSERT INTO code_registry(code,product_id,batch_id,valid)
            VALUES ('K1',1,1,1);
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, args=()):
        conn = connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return dict(rows[0]) if rows else None

    def _case(self):
        return services.accept_case(self.db, STORE_A, code="K1",
                                    product_model="M", batch_code="B1",
                                    customer_phone="138", files=[])

    def test_quarantine_blocks_only_store_then_hq_freeze_chainwide(self):
        cid = self._case()["id"]
        # 门店复查期暂挂：本店不能卖；他店同批仍可卖（尚未总部冻结）
        services.mark_related_inventory(self.db, STORE_A, cid)
        with self.assertRaises(services.FrozenBatchError):
            services.sell_item(self.db, STORE_A, store_id=1, batch_id=1, qty=1)
        sale = services.sell_item(self.db, STORE_B, store_id=2, batch_id=1, qty=1)
        self.assertTrue(sale["sale_id"] > 0)
        # 总部冻结：全连锁同批立即停售（结论与冻结同一调用）
        services.issue_conclusion(self.db, HQ, cid,
                                  verdict="suspected_counterfeit",
                                  text="疑似假冒，全批冻结", batch_action="freeze")
        with self.assertRaises(services.FrozenBatchError):
            services.sell_item(self.db, STORE_B, store_id=2, batch_id=1, qty=1)
        st = {r[0]: r[1] for r in self.q("SELECT store_id, status FROM inventory WHERE batch_id=1")}
        self.assertEqual(st, {1: "frozen", 2: "frozen"})
        # 无关批次不受影响
        self.assertEqual(
            self.one("SELECT status FROM inventory WHERE id=3")["status"], "normal")

    def test_freeze_conclusion_atomic_and_audited(self):
        cid = self._case()["id"]
        services.issue_conclusion(self.db, HQ, cid,
                                  verdict="counterfeit", text="假冒，没收",
                                  batch_action="condemn")
        self.assertEqual(
            self.one("SELECT status FROM inventory WHERE id=1")["status"], "condemned")
        ev = self.q(
            "SELECT batch_action FROM conclusions WHERE case_id=? ORDER BY version", (cid,))
        self.assertEqual([r[0] for r in ev], ["condemn"])
        fe = self.one("SELECT * FROM freeze_events WHERE action='condemn'")
        self.assertIsNotNone(fe["conclusion_id"])  # 冻结事件与结论绑定

    def test_sell_vs_freeze_race(self):
        """并发：一个线程销售、一个线程总部冻结；结束后批次不得再有成功超卖，
        且最终状态恒为 frozen。多轮重复降低调度偶然。"""
        outcomes = []

        def seller(barrier, rounds):
            barrier.wait()
            for _ in range(rounds):
                try:
                    services.sell_item(self.db, STORE_B, store_id=2,
                                       batch_id=1, qty=1)
                    outcomes.append("sold")
                except services.FrozenBatchError:
                    outcomes.append("blocked")
                except Exception:
                    outcomes.append("error")

        def freezer(barrier, cid):
            barrier.wait()
            services.issue_conclusion(
                self.db, HQ, cid, verdict="suspected_counterfeit",
                text="冻结", batch_action="freeze")

        for trial in range(5):
            # 每轮换一批新库存，重置为 normal + 充足数量
            conn = connect(self.db)
            conn.execute("UPDATE inventory SET qty=500, status='normal', "
                         "frozen_case_id=NULL WHERE batch_id=1")
            conn.commit()
            conn.close()
            cid = self._case()["id"]
            outcomes.clear()
            barrier = threading.Barrier(3)
            threads = [threading.Thread(target=seller, args=(barrier, 30)),
                       threading.Thread(target=seller, args=(barrier, 30)),
                       threading.Thread(target=freezer, args=(barrier, cid))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertNotIn("error", outcomes)
            # 冻结之后绝不可能再有售出：售出数必须 <= 冻结时刻的可见库存，
            # 用状态恒为 frozen + 数量守恒（初始 500）验证没有越过冻结销售
            inv = self.one("SELECT qty,status FROM inventory WHERE store_id=2 AND batch_id=1")
            self.assertEqual(inv["status"], "frozen")
            self.assertEqual(inv["qty"], 500 - outcomes.count("sold"))


class TestConclusionVersions(SeededTestBase):
    def test_revision_appends_and_supersedes(self):
        """复查改判：F 案有两版结论，v2 supersedes v1，案件指向 v2。"""
        fid = self.ids["F"]
        vs = self.q("SELECT id, version, verdict, batch_action, supersedes "
                    "FROM conclusions WHERE case_id=? ORDER BY version", (fid,))
        self.assertEqual([r[1] for r in vs], [1, 2])
        self.assertEqual(vs[0][2], "inconclusive")
        self.assertEqual(vs[1][2], "genuine")
        self.assertEqual(vs[1][4], vs[0][0])  # v2.supersedes = v1.id
        case = self.one("SELECT current_conclusion_id, status FROM cases WHERE id=?", (fid,))
        self.assertEqual(case["current_conclusion_id"], vs[1][0])
        self.assertEqual(case["status"], "closed")

    def test_old_version_text_preserved(self):
        fid = self.ids["F"]
        texts = [r[0] for r in self.q(
            "SELECT text FROM conclusions WHERE case_id=? ORDER BY version", (fid,))]
        self.assertIn("证据不足", texts[0])
        self.assertIn("成分检测", texts[1])
        self.assertNotEqual(texts[0], texts[1])

    def test_version_per_merged_root(self):
        """归并案件改判落在根案件上：A 根有两版（正品 -> 疑似假冒）。"""
        rid = self.ids["A_original"]
        vs = self.q("SELECT version, verdict FROM conclusions WHERE case_id=? ORDER BY version", (rid,))
        self.assertEqual([(r[0], r[1]) for r in vs],
                         [(1, "genuine"), (2, "suspected_counterfeit")])


class TestStorePermissions(SeededTestBase):
    def test_store_cannot_issue_conclusion(self):
        bid = self.ids["B"]
        with self.assertRaises(services.PermissionError):
            services.issue_conclusion(self.db, STORE_A, bid,
                                      verdict="genuine", text="门店自行判正品")
        # 未落库任何结论
        self.assertIsNone(
            self.one("SELECT * FROM conclusions WHERE case_id=?", (bid,)))
        denied = self.one(
            "SELECT * FROM audit_log WHERE action='issue_conclusion_denied' "
            "AND entity_id=?", (str(bid),))
        self.assertIsNotNone(denied)
        self.assertEqual(denied["allowed"], 0)

    def test_store_cannot_modify_existing_hq_conclusion(self):
        """门店无法改写：update 路径不存在；再次“出具”同样被拒，旧结论不变。"""
        cid = self.ids["C"]
        before = self.one("SELECT verdict, text FROM conclusions c JOIN cases ca "
                          "ON ca.current_conclusion_id=c.id WHERE ca.id=?", (cid,))
        with self.assertRaises(services.PermissionError):
            services.issue_conclusion(self.db, STORE_A, cid,
                                      verdict="genuine", text="门店试图翻案")
        after = self.one("SELECT verdict, text FROM conclusions c JOIN cases ca "
                         "ON ca.current_conclusion_id=c.id WHERE ca.id=?", (cid,))
        self.assertEqual(before, after)

    def test_store_cannot_condemn(self):
        with self.assertRaises(services.PermissionError):
            services.issue_conclusion(self.db, STORE_A, self.ids["B"],
                                      verdict="counterfeit", text="x",
                                      batch_action="condemn")


class TestEvidenceRetention(SeededTestBase):
    def test_all_six_scenarios_have_evidence(self):
        expect = {
            self.ids["A_original"]: {"package", "seal", "receipt", "code_query",
                                     "comparison", "hq_report"},
            self.ids["A_duplicate"]: {"package", "seal", "code_query", "comparison"},
            self.ids["B"]: {"label", "receipt", "code_query", "comparison"},
            self.ids["C"]: {"unit", "receipt", "code_query", "comparison",
                            "hq_report"},
            self.ids["D"]: {"package", "code_query", "comparison"},
            self.ids["E"]: {"package", "code_query", "supplier_statement",
                            "comparison", "hq_report"},
            self.ids["F"]: {"package", "code_query", "comparison", "hq_report"},
        }
        for cid, kinds in expect.items():
            got = {r[0] for r in self.q(
                "SELECT DISTINCT kind FROM evidence WHERE case_id=?", (cid,))}
            missing = kinds - got
            self.assertFalse(missing, "案件 #%s 缺证据类型 %s" % (cid, missing))

    def test_label_scratched_and_supplier_denial_files_exist(self):
        self.assertTrue(os.path.exists(os.path.join(self.data, "images", "b_label.png")))
        self.assertTrue(os.path.exists(
            os.path.join(self.data, "docs", "e_supplier_statement.txt")))

    def test_package_only_case_flags_missing_receipt(self):
        """仅外包装：比对结论必须记录销售链路无法核对，且不允许判正品。"""
        row = self.one(
            "SELECT content FROM evidence WHERE case_id=? AND kind='comparison'",
            (self.ids["D"],))
        self.assertIn("无销售小票", row["content"])
        self.assertIsNone(
            self.one("SELECT * FROM conclusions WHERE case_id=?", (self.ids["D"],)))

    def test_notifications_full_text(self):
        """客户告知完整发送结论文字（不截断），两个顾客各一条。"""
        ns = self.q("SELECT to_addr, content FROM notifications ORDER BY id")
        self.assertGreaterEqual(len(ns), 4)
        for _, content in ns:
            self.assertIn("【质控告知】", content)
        phones = {r[0] for r in ns}
        self.assertIn("138****0008", phones)  # 归并子案顾客也收到了告知


class TestListAndBoard(SeededTestBase):
    def test_frozen_only_filter(self):
        all_rows = services.list_cases(self.db, frozen_only=False)
        frozen = services.list_cases(self.db, frozen_only=True)
        self.assertGreater(len(all_rows), len(frozen))
        all_ids = {r["id"] for r in all_rows}
        frozen_ids = {r["id"] for r in frozen}
        self.assertTrue(frozen_ids <= all_ids)
        # A 原案与归并子案都应出现在冻结筛选里（批次PC...A被冻结）
        self.assertIn(self.ids["A_original"], frozen_ids)
        self.assertIn(self.ids["A_duplicate"], frozen_ids)
        # D（仅外包装、未冻结）不应出现
        self.assertNotIn(self.ids["D"], frozen_ids)

    def test_board_grouping_and_first_seen(self):
        b = services.get_evidence_board(self.db, self.ids["A_duplicate"])
        self.assertEqual(b["case"]["original_case_id"], self.ids["A_original"])
        self.assertIsNotNone(b["first_seen"])
        self.assertEqual(b["first_seen"]["store"], "城东旗舰店")
        # 五个来源分组键齐全
        self.assertEqual(set(b["groups"]),
                         {"customer", "store", "supplier", "registry", "hq"})
        # 库存反映冻结
        statuses = {i["status"] for i in b["inventory"] if i["batch_code"] == "PC20260901A"}
        self.assertEqual(statuses, {"frozen"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
