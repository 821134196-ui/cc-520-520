"""演示种子数据：一条命令重建一个可直接操作的连锁门店质控环境。

覆盖六类必须保留完整证据的情形：
  A 防伪码重复（核心案例，启动即自带）
  B 标签刮损
  C 销售记录不匹配
  D 顾客仅带外包装
  E 供应商否认批次
  F 复查后改判（结论多版本）
另含一个尚未受理的正常库存批次，用于演示冻结竞态下销售被拦截。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import images, services
from .db import connect

STORE_A = {"role": "store", "store_id": 1}
STORE_B = {"role": "store", "store_id": 2}
HQ = {"role": "hq", "store_id": None}


def _img_dir(base):
    d = os.path.join(base, "images")
    os.makedirs(d, exist_ok=True)
    return d


def _doc_dir(base):
    d = os.path.join(base, "docs")
    os.makedirs(d, exist_ok=True)
    return d


def seed(db_path: str, data_dir: str) -> dict:
    if os.path.exists(db_path):
        os.remove(db_path)
    for ext in ("-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)
    from .db import init_db
    init_db(db_path)

    imgd, docd = _img_dir(data_dir), _doc_dir(data_dir)
    conn = connect(db_path)
    now = datetime.now()
    ts = lambda d: (now - timedelta(days=d)).isoformat(timespec="seconds")

    conn.executescript("""
      INSERT INTO stores (id, code, name, role) VALUES
        (1,'SH001','城东旗舰店','store'),
        (2,'SH002','南站分店','store'),
        (3,'HQ','总部质控中心','hq');
      INSERT INTO suppliers (id, code, name) VALUES
        (1,'SUP-A','安洁日化原厂'),
        (2,'SUP-B','恒顺贸易（渠道商）');
      INSERT INTO products (id, model, name) VALUES
        (1,'AJ-X1','安洁洗发水 400ml'),
        (2,'AJ-X2','安洁沐浴露 500ml'),
        (3,'AJ-FX','安洁精华霜 50g');
    """)

    def batch(bid, code, pid, sup, store, days, ack=1, note=None):
        conn.execute(
            """INSERT INTO batches (id, code, product_id, supplier_id,
               supplied_store_id, supply_date, supplier_acknowledged, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (bid, code, pid, sup, store, ts(days)[:10], ack, note))

    def inv(iid, store, bid, qty, status="normal", case=None):
        conn.execute(
            """INSERT INTO inventory (id, store_id, batch_id, qty, status,
               frozen_case_id, updated_at) VALUES (?,?,?,?,?,?,?)""",
            (iid, store, bid, qty, status, case, ts(0)))

    # 批次：A 重复码（1号店首查，2号店再查）；B 刮损；C 销售不匹配；
    # D 仅外包装；E 供应商否认；F 改判；G 正常批次（演示销售拦截）
    batch(1, "PC20260901A", 1, 1, 1, 31)
    batch(2, "PC20260905B", 2, 1, 1, 26)
    batch(3, "PC20260910C", 1, 2, 2, 21)
    batch(4, "PC20260912D", 3, 1, 2, 19)
    batch(5, "PC20260915E", 1, 2, 1, 16, ack=0,
          note="供应商否认：该批号码段不在其出厂记录")
    batch(6, "PC20260918F", 2, 1, 1, 13)
    batch(7, "PC20260925G", 3, 1, 2, 6)

    inv(1, 1, 1, 40)
    inv(2, 2, 1, 12)   # 同批次也配送到 2 号店
    inv(3, 1, 2, 30)
    inv(4, 2, 3, 25)
    inv(5, 2, 4, 18)
    inv(6, 1, 5, 22)
    inv(7, 1, 6, 16)
    inv(8, 2, 7, 50)

    # 历史销售
    conn.execute(
        """INSERT INTO sales (id, store_id, sale_no, product_id, batch_id,
           code, customer_phone, sold_at) VALUES
           (1,1,'XS-9001',1,1,'ACF-A1-88880001','138****0001',?),
           (2,1,'XS-9002',2,2,'ACF-B2-77770002','138****0002',?),
           (3,2,'XS-9003',1,3,'ACF-C3-66660003','138****0003',?)""",
        (ts(20), ts(15), ts(10)))

    # 防伪码登记册
    conn.executescript("""
      INSERT INTO code_registry (code, product_id, batch_id, valid, note) VALUES
        ('ACF-A1-88880001',1,1,1,'重复码主案例'),
        ('ACF-B2-77770002',2,2,1,'标签刮损'),
        ('ACF-C3-66660003',1,3,1,'销售不匹配'),
        ('ACF-D4-55550004',3,4,1,'仅外包装'),
        ('ACF-E5-44440005',1,5,1,'供应商否认批次'),
        ('ACF-F6-33330006',2,6,1,'复查改判'),
        ('ACF-G7-22220007',3,7,1,'正常在售');
    """)
    conn.commit()
    conn.close()

    ids = {}

    # ---- A：重复码。原案在 1 号店受理、查码、比对、暂挂、总部初判正品；
    #         随后 2 号店遇到同码新案，查码触发归并，总部改判疑似假冒并全批冻结 ----
    pkg = images.package_photo(imgd, "a_pkg_1.png", "ACF-A1-88880001",
                               "PC20260901A", seal_ok=True)
    seal = images.seal_photo(imgd, "a_seal_1.png", ok=True)
    rcpt = images.receipt_photo(imgd, "a_rcpt_1.png", "XS-9001",
                                "AJ-X1", ts(20)[:10])
    c1 = services.accept_case(db_path, STORE_A,
        code="ACF-A1-88880001", product_model="AJ-X1", sale_no="XS-9001",
        batch_code="PC20260901A", customer_phone="138****0001",
        customer_desc="顾客怀疑买到假货，包装与封签完好",
        files=[{"kind": "package", "title": "外包装照片（顾客提供）", "file_path": pkg},
               {"kind": "seal", "title": "封签照片：完好", "file_path": seal},
               {"kind": "receipt", "title": "销售小票 XS-9001", "file_path": rcpt}])
    ids["A_original"] = c1["id"]
    services.query_code(db_path, STORE_A, c1["id"])
    services.compare_evidence(db_path, STORE_A, c1["id"])
    services.mark_related_inventory(db_path, STORE_A, c1["id"])
    services.issue_conclusion(db_path, HQ, c1["id"],
        verdict="genuine",
        text="首次核验：防伪码登记在册且为首查，销售单、供应批次、封签均吻合，判定为正品。",
        batch_action="release", reason="门店核对一致，解除暂挂")
    services.notify_customer(db_path, STORE_A, c1["id"])

    # 2 号店：同码再次出现（重复码案例随启动自带）
    pkg2 = images.package_photo(imgd, "a_pkg_2.png", "ACF-A1-88880001",
                                "PC20260901A", seal_ok=False,
                                tag="SEAL BROKEN")
    c2 = services.accept_case(db_path, STORE_B,
        code="ACF-A1-88880001", product_model="AJ-X1",
        batch_code="PC20260901A", customer_phone="138****0008",
        customer_desc="另一顾客持同码商品到南站分店，封签有撬动痕迹",
        files=[{"kind": "package", "title": "外包装照片（南站分店受理）",
                "file_path": pkg2},
               {"kind": "seal", "source": "customer",
                "title": "封签照片：破损撬动", "file_path": "a_seal_2.png"}])
    images.seal_photo(imgd, "a_seal_2.png", ok=False)
    dup = services.query_code(db_path, STORE_B, c2["id"])
    ids["A_duplicate"] = c2["id"]
    ids["A_merge_note"] = dup["merge"]["note"] if dup["merge"] else None
    services.compare_evidence(db_path, STORE_B, c2["id"])
    services.mark_related_inventory(db_path, STORE_B, c2["id"])
    # 归并后总部对根案件改判：冻结全连锁同批库存（结论与冻结同事务）
    services.issue_conclusion(db_path, HQ, c1["id"],
        verdict="suspected_counterfeit",
        text=("同一防伪码 ACF-A1-88880001 在两个门店重复出现，且第二次出现商品封签破损；"
              "存在套码/回收包装造假重大嫌疑，改判疑似假冒，批次 PC20260901A 全渠道冻结。"),
        batch_action="freeze", reason="重复码 + 封签破损，证据链冲突")
    services.notify_customer(db_path, STORE_B, c2["id"])

    # ---- B：标签刮损（顾客刮码过度，部分数字不可读，待总部）----
    lab = images.label_photo(imgd, "b_label.png", "ACF-B2-77770002",
                             scratched=True)
    cb = services.accept_case(db_path, STORE_A,
        code="ACF-B2-77770002", product_model="AJ-X2", sale_no="XS-9002",
        batch_code="PC20260905B", customer_phone="138****0002",
        customer_desc="防伪涂层被严重刮损，码只看清一部分",
        files=[{"kind": "label", "title": "刮损防伪标签照片", "file_path": lab},
               {"kind": "receipt", "title": "销售小票 XS-9002",
                "file_path": images.receipt_photo(imgd, "b_rcpt.png", "XS-9002",
                                                  "AJ-X2", ts(15)[:10])}])
    services.query_code(db_path, STORE_A, cb["id"])
    services.compare_evidence(db_path, STORE_A, cb["id"])
    services.mark_related_inventory(db_path, STORE_A, cb["id"])
    ids["B"] = cb["id"]

    # ---- C：销售记录不匹配（型号/批次对不上小票）----
    cc = services.accept_case(db_path, STORE_B,
        code="ACF-C3-66660003", product_model="AJ-X1", sale_no="XS-9003",
        batch_code="PC20260910C", customer_phone="138****0003",
        customer_desc="商品型号与小票不符，怀疑调包",
        files=[{"kind": "unit", "title": "商品本体照片",
                "file_path": images.unit_photo(imgd, "c_unit.png",
                                               "ACF-C3-66660003", ok=False)},
               {"kind": "receipt", "title": "销售小票 XS-9003",
                "file_path": images.receipt_photo(imgd, "c_rcpt.png", "XS-9003",
                                                  "AJ-X1", ts(10)[:10])}])
    services.query_code(db_path, STORE_B, cc["id"])
    services.compare_evidence(db_path, STORE_B, cc["id"])
    services.mark_related_inventory(db_path, STORE_B, cc["id"])
    services.issue_conclusion(db_path, HQ, cc["id"],
        verdict="sales_mismatch",
        text="小票型号、批次与实物登记不一致，防伪码虽在册但指向其他批次；判定销售记录不匹配，冻结相关批次并核查出库环节。",
        batch_action="freeze", reason="型号/批次/码三者链路冲突")
    services.notify_customer(db_path, STORE_B, cc["id"])
    ids["C"] = cc["id"]

    # ---- D：顾客仅带外包装，无销售小票 ----
    cd = services.accept_case(db_path, STORE_B,
        code="ACF-D4-55550004", product_model="AJ-FX",
        batch_code="PC20260912D", customer_phone="138****0004",
        customer_desc="只拿来一个空盒子，商品和小票都没带",
        files=[{"kind": "package", "title": "仅外包装（无小票无实物）",
                "file_path": images.package_photo(imgd, "d_pkg.png",
                                                  "ACF-D4-55550004",
                                                  "PC20260912D")}])
    services.query_code(db_path, STORE_B, cd["id"])
    services.compare_evidence(db_path, STORE_B, cd["id"])
    ids["D"] = cd["id"]

    # ---- E：供应商否认批次 ----
    ce = services.accept_case(db_path, STORE_A,
        code="ACF-E5-44440005", product_model="AJ-X1",
        batch_code="PC20260915E", customer_phone="138****0005",
        customer_desc="包装粗糙，质疑假货",
        files=[{"kind": "package", "title": "外包装照片",
                "file_path": images.package_photo(imgd, "e_pkg.png",
                                                  "ACF-E5-44440005",
                                                  "PC20260915E", seal_ok=False)}])
    services.query_code(db_path, STORE_A, ce["id"])
    stmt = images.text_voucher(docd, "e_supplier_statement.txt",
        "供应商批次否认声明（模拟凭证）",
        ["致：总部质控中心",
         "经核查，批次号 PC20260915E 不在我司出厂批次号码段内，"
         "该批防伪码标签亦非我司申领。我司否认生产或供应该批次商品。",
         "供应商：恒顺贸易（渠道商）        日期：%s" % ts(12)[:10]])
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO evidence (case_id, kind, source, title, file_path,
           created_by_role, created_at) VALUES (?, 'supplier_statement','supplier',
           '供应商书面否认批次 PC20260915E', ?, 'store', ?)""",
        (ce["id"], stmt, ts(12)))
    conn.commit()
    conn.close()
    services.compare_evidence(db_path, STORE_A, ce["id"])
    services.mark_related_inventory(db_path, STORE_A, ce["id"])
    services.issue_conclusion(db_path, HQ, ce["id"],
        verdict="counterfeit",
        text="供应商书面否认该批次，平台防伪码与批次归属矛盾，包装工艺与正品存在差异；判定假冒商品，全批没收并追溯渠道。",
        batch_action="condemn", reason="供应商否认 + 工艺差异")
    services.notify_customer(db_path, STORE_A, ce["id"])
    ids["E"] = ce["id"]

    # ---- F：复查后改判（先存疑、复检正品，保留两版结论）----
    cf = services.accept_case(db_path, STORE_A,
        code="ACF-F6-33330006", product_model="AJ-X2",
        batch_code="PC20260918F", customer_phone="138****0006",
        customer_desc="气味与以往不同，要求复查",
        files=[{"kind": "package", "title": "外包装照片",
                "file_path": images.package_photo(imgd, "f_pkg.png",
                                                  "ACF-F6-33330006",
                                                  "PC20260918F")}])
    services.query_code(db_path, STORE_A, cf["id"])
    services.compare_evidence(db_path, STORE_A, cf["id"])
    services.mark_related_inventory(db_path, STORE_A, cf["id"])
    services.issue_conclusion(db_path, HQ, cf["id"],
        verdict="inconclusive",
        text="初次复检气味主观差异无法仪器确认，证据不足，维持冻结待实验室成分检测。",
        batch_action="freeze", reason="证据不足，等待检测")
    report = images.text_voucher(docd, "f_lab_report.txt",
        "总部实验室成分检测报告（模拟凭证）",
        ["案件对应批次：PC20260918F",
         "检测项目：香型成分、pH、活性物含量",
         "结果：各项指标与正品标准样品一致，未检出异常添加物。",
         "结论：成分符合正品标准。        日期：%s" % ts(2)[:10]])
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO evidence (case_id, kind, source, title, file_path,
           created_by_role, created_at) VALUES (?, 'hq_report','hq',
           '实验室成分检测报告（复检）', ?, 'hq', ?)""",
        (cf["id"], report, ts(2)))
    conn.commit()
    conn.close()
    services.issue_conclusion(db_path, HQ, cf["id"],
        verdict="genuine",
        text="实验室成分检测与正品标准样品完全一致，主观气味差异系批次香型正常波动；改判正品，解除冻结。",
        batch_action="release", reason="实验室复检合格")
    services.notify_customer(db_path, STORE_A, cf["id"])
    ids["F"] = cf["id"]

    return ids


def _case_id_by_code(db_path, code):
    conn = connect(db_path)
    try:
        return conn.execute("SELECT id FROM cases WHERE code = ? ORDER BY id LIMIT 1",
                            (code,)).fetchone()["id"]
    finally:
        conn.close()
