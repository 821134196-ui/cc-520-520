"""模拟适配器：品牌防伪查询平台 + 短信通知。

真实部署应替换为厂商 HTTP API；本实现读写本地 code_registry，
并在每次查询时累计 query_count、记录首次出现的时间与门店。
同一码在不同门店/不同案件再次查询即判定 duplicate 并返回原案件。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import connect


class AntiCounterfeitAdapter:
    """防伪查询适配器（模拟）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def query(self, code: str, store_id: int) -> dict:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            reg = conn.execute(
                "SELECT * FROM code_registry WHERE code = ?", (code,)).fetchone()
            now = datetime.now().isoformat(timespec="seconds")
            if reg is None:
                conn.execute("COMMIT")
                return {"code": code, "result": "not_registered",
                        "message": "防伪平台无此码登记记录", "query_count": 0}

            is_dup = reg["query_count"] > 0
            first_store = reg["first_store_id"]
            first_seen = reg["first_seen_at"]
            if first_seen is None:
                # 首次查询：登记首次出现时间与门店
                first_store = store_id
                first_seen = now
            # 找出首次出现时关联的原案件（同码、最早查询所在的根案件）
            root_case = conn.execute(
                """SELECT c.* FROM cases c
                   JOIN code_queries q ON q.case_id = c.id
                   WHERE c.code = ?
                   ORDER BY q.id ASC LIMIT 1""", (code,)).fetchone()
            conn.execute(
                """UPDATE code_registry
                   SET query_count = query_count + 1,
                       first_seen_at = COALESCE(first_seen_at, ?),
                       first_store_id = COALESCE(first_store_id, ?)
                   WHERE code = ?""",
                (now, store_id, code))
            conn.execute("COMMIT")

            if not reg["valid"]:
                result = "flagged"
                msg = "平台已将该码标记为异常/假冒"
            elif is_dup:
                result = "duplicate"
                msg = "重复查询：该码此前已被查询过"
            else:
                result = "registered"
                msg = "首次查询，登记在册"

            return {
                "code": code,
                "result": result,
                "valid": bool(reg["valid"]),
                "message": msg,
                "query_count": (reg["query_count"] + 1),
                "is_duplicate": is_dup,
                "first_seen_at": first_seen,
                "first_store_id": first_store,
                "original_case_id": root_case["id"] if root_case else None,
                "original_case_no": root_case["case_no"] if root_case else None,
                "registered_batch_id": reg["batch_id"],
                "raw": json.dumps({"platform": "brand-acf-sim", "code": code,
                                   "ts": now}, ensure_ascii=False),
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            conn.close()


class NotificationAdapter:
    """短信通知适配器（模拟）：写入 notifications 表并返回虚构运营商回执。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def send(self, case_id: int, phone: str, content: str) -> dict:
        conn = connect(self.db_path)
        try:
            now = datetime.now().isoformat(timespec="seconds")
            msg_id = "SIM" + str(abs(hash((case_id, phone, now))) % 10**10).zfill(10)
            cur = conn.execute(
                """INSERT INTO notifications
                   (case_id, channel, to_addr, content, status, provider_msg_id, sent_at)
                   VALUES (?, 'sms', ?, ?, 'delivered', ?, ?)""",
                (case_id, phone, content, msg_id, now))
            conn.commit()
            return {"ok": True, "provider_msg_id": msg_id, "to": phone,
                    "notification_id": cur.lastrowid, "content": content}
        finally:
            conn.close()
