# -*- coding: utf-8 -*-
"""演示种子数据：一条本地命令启动后即自带完整案例链。

重点案例 —— 同一防伪码 AC-DUP-7777：
  09-28 S01 首次受理（封签破损）→ 冻结 → 09-29 总部 V1 判真品并解冻
  10-01 S02 再次受理同一码     → 归并原调查、重新升级、冻结 S02 同批库存
  10-02 S03 店员码查询同一码   → 仅留痕并关联原调查（不产生新案）
另含：供应商否认批次 + 假冒结论、标签刮损 + 仅外包装 + 无法认定、全部匹配未上报。
"""
from __future__ import annotations

from . import db
from .db import CONCL_FAKE, CONCL_GENUINE, CONCL_INCONCLUSIVE, insert, now_iso, query_one
from .services import Service


def seeded(conn) -> bool:
    return query_one(conn, "SELECT COUNT(*) AS c FROM stores")["c"] > 0


def seed(conn, evidence_dir: str) -> None:
    if seeded(conn):
        return
    ts = now_iso()

    # ---- 组织主数据 ----
    for sid, name, is_hq in [
        ("S01", "南京中山路店", 0),
        ("S02", "苏州观前街店", 0),
        ("S03", "杭州武林广场店", 0),
        ("HQ", "总部质量管理中心", 1),
    ]:
        insert(conn, "INSERT INTO stores(id,name,is_hq,created_at) VALUES (?,?,?,?)",
               (sid, name, is_hq, ts))
    for sid, name in [
        ("SUP-A", "声海电子（深圳）"),
        ("SUP-B", "环宇智能穿戴（东莞）"),
        ("SUP-C", "咖享生活电器（宁波）"),
    ]:
        insert(conn, "INSERT INTO suppliers(id,name,created_at) VALUES (?,?,?)",
               (sid, name, ts))
    for model, name, sup in [
        ("EP-200", "真无线降噪耳机", "SUP-A"),
        ("EP-500", "智能运动手表", "SUP-B"),
        ("EP-100", "声波电动牙刷", "SUP-A"),
        ("EP-300", "便携胶囊咖啡机", "SUP-C"),
    ]:
        insert(conn, "INSERT INTO products(model,name,supplier_id,created_at) VALUES (?,?,?,?)",
               (model, name, sup, ts))

    def lot(lot_no, model, sup, shipped, confirmed=1):
        return insert(
            conn,
            "INSERT INTO supply_lots(lot_no,product_model,supplier_id,shipped_at,"
            " supplier_confirmed,created_at) VALUES (?,?,?,?,?,?)",
            (lot_no, model, sup, shipped, confirmed, ts))

    lot_a = lot("L202609A", "EP-200", "SUP-A", "2026-09-05T09:00+08:00")
    lot_x = lot("L202609X", "EP-500", "SUP-B", "2026-09-20T09:00+08:00", confirmed=0)
    lot_b = lot("L202609B", "EP-500", "SUP-B", "2026-09-06T09:00+08:00")
    lot_brush = lot("L202608B", "EP-100", "SUP-A", "2026-08-18T09:00+08:00")
    lot_coffee = lot("L202609C", "EP-300", "SUP-C", "2026-09-08T09:00+08:00")
    lot_race = lot("L202609R", "EP-200", "SUP-A", "2026-09-09T09:00+08:00")

    def inv(store, lot_id, total, sold=0):
        insert(
            conn,
            "INSERT INTO inventory(store_id,lot_id,qty_total,qty_sold,status,created_at)"
            " VALUES (?,?,?,?, 'ACTIVE', ?)", (store, lot_id, total, sold, ts))

    inv("S01", lot_a, 20, 6)
    inv("S02", lot_a, 12, 4)
    inv("S03", lot_a, 10, 1)
    inv("S02", lot_x, 8, 0)          # 案例 B 受理后由流程冻结
    inv("S01", lot_b, 10, 2)
    inv("S02", lot_b, 10, 0)
    inv("S01", lot_brush, 15, 3)     # 案例 C 受理后冻结（无法认定，维持）
    inv("S03", lot_coffee, 6, 1)
    inv("S03", lot_race, 50, 0)      # 竞态演示专用库存

    def sale(receipt, store, lot_id, model, code, customer, sold_at):
        insert(
            conn,
            "INSERT INTO sales(receipt_no,store_id,lot_id,product_model,security_code,"
            " customer_name,sold_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (receipt, store, lot_id, model, code, customer, sold_at, sold_at))

    sale("R-S01-1002", "S01", lot_a, "EP-200", "AC-DUP-7777", "王磊",
         "2026-09-12T19:40+08:00")
    sale("R-S02-3007", "S02", lot_a, "EP-200", "AC-OK-2222", "陈静",
         "2026-09-18T20:10+08:00")
    sale("R-S01-2001", "S01", lot_brush, "EP-100", "AC-DMG-1234", "赵芳",
         "2026-08-30T15:05+08:00")
    sale("R-S03-5010", "S03", lot_coffee, "EP-300", "AC-OK-5555", "孙立",
         "2026-09-22T11:30+08:00")

    # 防伪库注册档（防伪平台模拟数据；AC-FAKE-0001 故意不注册）
    for code, model, lid, first_store, damaged, note in [
        ("AC-DUP-7777", "EP-200", lot_a, "S01", 0, "首发登记：南京中山路店"),
        ("AC-OK-2222", "EP-200", lot_a, "S02", 0, None),
        ("AC-DMG-1234", "EP-100", lot_brush, "S01", 1, "消费者曾反馈涂层难刮"),
        ("AC-OK-5555", "EP-300", lot_coffee, "S03", 0, None),
        ("AC-OK-9001", "EP-500", lot_b, "S01", 0, None),
    ]:
        insert(
            conn,
            "INSERT INTO security_code_registry(code,product_model,lot_id,first_store_id,"
            " damaged,note) VALUES (?,?,?,?,?,?)",
            (code, model, lid, first_store, damaged, note))

    # ---- 通过正式业务流程生成案例（证据图片/流水均走真实代码路径）----
    svc = Service(conn, evidence_dir)

    # 案例 C（时间最早）：刮损 + 仅外包装 → 无法认定，维持冻结
    c = svc.intake(actor_store="S01", product_model="EP-100",
                   receipt_no="R-S01-2001", security_code="AC-DMG-1234",
                   lot_no="L202608B", package_only=True, label_damaged=True,
                   customer_name="赵芳", customer_contact="13701400003")
    svc.hq_review(actor_store="HQ", investigation_id=c["investigation_id"],
                  conclusion=CONCL_INCONCLUSIVE, decided_by="周质控",
                  reason="顾客仅携带外包装且防伪标签刮损，无法对商品本体进行鉴定，"
                         "现有材料不足以认定真伪；请门店联系顾客补充实物后再行复查。")

    # 案例主线 A：同一防伪码 AC-DUP-7777
    a = svc.intake(actor_store="S01", product_model="EP-200",
                   receipt_no="R-S01-1002", security_code="AC-DUP-7777",
                   lot_no="L202609A", seal_intact=False,
                   customer_name="王磊", customer_contact="13801400001")
    svc.hq_review(actor_store="HQ", investigation_id=a["investigation_id"],
                  conclusion=CONCL_GENUINE, decided_by="周质控",
                  reason="防伪码首次查询且与销售单、供应批次三方一致，供应商确认批次无误；"
                         "封签破损系顾客自行拆封所致，未见假冒特征，解除库存冻结。")
    # 同码在 S02 再次出现：归并原案、重新升级
    svc.intake(actor_store="S02", product_model="EP-200",
               receipt_no="R-S02-3007", security_code="AC-DUP-7777",
               lot_no="L202609A", seal_intact=True,
               customer_name="陈静", customer_contact="13601400004")
    # S03 店员只是查码：必须关联原调查
    svc.code_search(actor_store="S03", code="AC-DUP-7777")

    # 案例 B：查无此码 + 销售单不存在 + 供应商否认批次 → 假冒
    b = svc.intake(actor_store="S02", product_model="EP-500",
                   receipt_no="R-FAKE-9999", security_code="AC-FAKE-0001",
                   lot_no="L202609X", customer_name="钱伟",
                   customer_contact="13901400002")
    svc.hq_review(actor_store="HQ", investigation_id=b["investigation_id"],
                  conclusion=CONCL_FAKE, decided_by="林质控",
                  reason="三项证据互证：防伪平台查无此码、连锁销售台账无此销售单、"
                         "供应商书面否认 L202609X 批次为其出厂，认定为假冒商品；"
                         "同线索库存继续冻结，转法务与仓管处置。")

    # 案例 D：全部匹配，未上报
    svc.intake(actor_store="S03", product_model="EP-300",
               receipt_no="R-S03-5010", security_code="AC-OK-5555",
               lot_no="L202609C", customer_name="孙立")

    _backdate(conn)


def _backdate(conn) -> None:
    """把演示案例的时间线回写到 9 月底，凸显重复码“首次出现”的先后。"""
    def q1(sql, params=()):
        return query_one(conn, sql, params)

    def by_code(code):
        return q1("SELECT id FROM investigations WHERE security_code=? AND merged_into_id IS NULL",
                  (code,))["id"]

    aid = by_code("AC-DUP-7777")
    bid = by_code("AC-FAKE-0001")
    cid = by_code("AC-DMG-1234")
    did = by_code("AC-OK-5555")
    child_id = q1("SELECT id FROM investigations WHERE merged_into_id=? ORDER BY id LIMIT 1",
                  (aid,))["id"]

    t_first = "2026-09-28T10:14:00+08:00"   # A 首次受理
    t_v1 = "2026-09-29T15:30:00+08:00"      # A 总部 V1 真品
    t_dup = "2026-10-01T11:02:00+08:00"     # A 重复码再现（归并）
    t_query3 = "2026-10-02T09:20:00+08:00"  # A S03 查询留痕
    t_b, t_b_v1 = "2026-09-30T14:00:00+08:00", "2026-09-30T17:40:00+08:00"
    t_c, t_c_v1 = "2026-09-27T16:30:00+08:00", "2026-09-28T09:10:00+08:00"
    t_d = "2026-10-01T16:20:00+08:00"

    def stamp_intake(inv_id, ts):
        for tbl in ("evidence", "findings"):
            conn.execute(f"UPDATE {tbl} SET created_at=? WHERE investigation_id=?",
                         (ts, inv_id))
        conn.execute("UPDATE code_queries SET queried_at=? WHERE investigation_id=?",
                     (ts, inv_id))
        conn.execute("UPDATE inventory_freezes SET created_at=? WHERE investigation_id=?",
                     (ts, inv_id))

    def stamp_hq(inv_id, ts):
        conn.execute("UPDATE evidence SET created_at=? WHERE investigation_id=? AND source='HQ'",
                     (ts, inv_id))
        conn.execute("UPDATE conclusion_versions SET created_at=? WHERE investigation_id=?",
                     (ts, inv_id))
        conn.execute("UPDATE notifications SET created_at=?, sent_at=? WHERE investigation_id=?",
                     (ts, ts, inv_id))

    def close_case(inv_id, t_close):
        conn.execute("UPDATE investigations SET closed_at=? WHERE id=?", (t_close, inv_id))

    # 案例 C / B / D：受理材料整体回写，总部材料覆盖到结论时间
    stamp_intake(cid, t_c); stamp_hq(cid, t_c_v1)
    conn.execute("UPDATE investigations SET created_at=?, updated_at=?, first_seen_at=? WHERE id=?",
                 (t_c, t_c_v1, t_c, cid)); close_case(cid, t_c_v1)

    stamp_intake(bid, t_b); stamp_hq(bid, t_b_v1)
    conn.execute("UPDATE investigations SET created_at=?, updated_at=?, first_seen_at=? WHERE id=?",
                 (t_b, t_b_v1, t_b, bid)); close_case(bid, t_b_v1)

    stamp_intake(did, t_d)
    conn.execute("UPDATE investigations SET created_at=?, updated_at=?, first_seen_at=? WHERE id=?",
                 (t_d, t_d, t_d, did))

    # 案例 A 主线：先整体收到首次受理，再覆盖后续事件
    stamp_intake(aid, t_first)
    stamp_hq(aid, t_v1)
    conn.execute(
        "UPDATE evidence SET created_at=? WHERE investigation_id=?"
        " AND (title LIKE '%重复%' OR title LIKE '%再次出现%')", (t_dup, aid))
    conn.execute(
        "UPDATE evidence SET created_at=? WHERE investigation_id=? AND title LIKE '%查询%'",
        (t_query3, aid))
    conn.execute("UPDATE findings SET created_at=? WHERE investigation_id=? AND kind='DUPLICATE_CODE'",
                 (t_dup, aid))
    qrows = conn.execute(
        "SELECT id FROM code_queries WHERE investigation_id=? ORDER BY id", (aid,)).fetchall()
    conn.execute("UPDATE code_queries SET queried_at=? WHERE id=?", (t_first, qrows[0]["id"]))
    conn.execute("UPDATE code_queries SET queried_at=? WHERE id=?", (t_dup, qrows[1]["id"]))
    conn.execute("UPDATE code_queries SET queried_at=? WHERE id=?", (t_query3, qrows[2]["id"]))
    frows = conn.execute(
        "SELECT id FROM inventory_freezes WHERE investigation_id=? ORDER BY id",
        (aid,)).fetchall()
    # 09-28 S01 冻结 → 09-29 V1 真品解冻 → 10-01 S02 追加冻结
    conn.execute("UPDATE inventory_freezes SET created_at=? WHERE id=?",
                 (t_first, frows[0]["id"]))
    conn.execute("UPDATE inventory_freezes SET created_at=? WHERE id=?",
                 (t_v1, frows[1]["id"]))
    conn.execute("UPDATE inventory_freezes SET created_at=? WHERE id=?",
                 (t_dup, frows[2]["id"]))
    conn.execute(
        "UPDATE investigations SET created_at=?, first_seen_at=?, updated_at=?,"
        " status='ESCALATED', closed_at=NULL WHERE id=?",
        (t_first, t_first, t_query3, aid))
    conn.execute(
        "UPDATE investigations SET created_at=?, updated_at=?, first_seen_at=?,"
        " status='ESCALATED', closed_at=NULL WHERE id=?",
        (t_dup, t_dup, t_first, child_id))

    # 库存冻结时间与原因
    conn.execute(
        "UPDATE inventory SET frozen_at=?, frozen_reason=? WHERE lot_id="
        " (SELECT id FROM supply_lots WHERE lot_no='L202609A') AND store_id='S02'",
        (t_dup, "调查重复码 AC-DUP-7777 复查期间预防性冻结"))
    conn.execute(
        "UPDATE inventory SET frozen_at=?, frozen_reason=? WHERE lot_id="
        " (SELECT id FROM supply_lots WHERE lot_no='L202609X')",
        (t_b, "总部复查认定假冒，批次冻结待处置"))
    conn.execute(
        "UPDATE inventory SET frozen_at=?, frozen_reason=? WHERE lot_id="
        " (SELECT id FROM supply_lots WHERE lot_no='L202608B')",
        (t_c, "证据不足无法认定，库存继续冻结待补证"))
