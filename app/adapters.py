# -*- coding: utf-8 -*-
"""模拟外部适配器：防伪查询、供应商批次确认、客户通知。

真实系统中这些会调用防伪平台/供应商门户/短信网关；本实现全部在本地模拟，
返回结构保持稳定，业务层不依赖其实现细节。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from . import db
from .db import CODE_DAMAGED, CODE_DUPLICATE, CODE_OK, CODE_UNKNOWN


@dataclass
class CodeQueryResult:
    """防伪码查询适配器返回。"""
    status: str                    # OK / DUPLICATE / DAMAGED / UNKNOWN
    product_model: Optional[str]
    first_seen_at: Optional[str]   # 防伪平台记录的首次查询/出现时间
    first_seen_store: Optional[str]
    message: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "product_model": self.product_model,
            "first_seen_at": self.first_seen_at,
            "first_seen_store": self.first_seen_store,
            "message": self.message,
        }


def code_fingerprint(code: str) -> str:
    """刮损码可能不完整，统一生成指纹用于归并比对。"""
    normalized = (code or "").strip().upper().replace(" ", "")
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


class AntiCounterfeitAdapter:
    """防伪平台（模拟）。

    判定规则：
    - 码在防伪库注册且未标记刮损 -> OK / DUPLICATE（由调查历史决定重复）
    - 注册档标记 damaged -> DAMAGED
    - 未注册 -> UNKNOWN
    重复的“首次出现时间与门店”来自本系统调查留痕（传入 history_finder），
    保证同一码多次查询始终指向同一原案。
    """

    def __init__(self, conn, history_finder):
        self._conn = conn
        self._find_history = history_finder

    def query(self, code: Optional[str], *, damaged_hint: bool = False) -> CodeQueryResult:
        code_norm = (code or "").strip().upper() or None
        reg = None
        if code_norm:
            reg = db.query_one(
                self._conn,
                "SELECT * FROM security_code_registry WHERE code = ?", (code_norm,),
            )
        if reg is None:
            # 未注册码也可能已有历史（如伪造码被多次举报）
            hist = self._find_history(code_norm) if code_norm else None
            if hist:
                return CodeQueryResult(
                    CODE_DUPLICATE, None,
                    hist["first_seen_at"], hist["first_seen_store_id"],
                    "防伪库无此码，且该码在既往调查中已出现，按重复线索并入原调查。",
                )
            return CodeQueryResult(CODE_UNKNOWN, None, None, None, "防伪库查无此码。")

        # 重复判定优先于刮损：已在既往调查中出现的码（即便刮损）必须归并原案
        hist = self._find_history(code_norm)
        if hist is not None:
            msg = "防伪码已在其他调查中出现：一码多用，高度疑似假冒/套码。"
            if reg["damaged"] or damaged_hint:
                msg += "（该码标签同时存在刮损记录）"
            return CodeQueryResult(
                CODE_DUPLICATE, reg["product_model"],
                hist["first_seen_at"], hist["first_seen_store_id"], msg,
            )
        if reg["damaged"] or damaged_hint:
            return CodeQueryResult(
                CODE_DAMAGED, reg["product_model"], None, None,
                "防伪标签刮损，涂层/字符缺失，无法完整识读，需人工复核。",
            )
        return CodeQueryResult(
            CODE_OK, reg["product_model"], None, None,
            "防伪码首次查询，库内信息一致。",
        )


class SupplierAdapter:
    """供应商门户（模拟）：确认批次是否为其出厂。"""

    def confirm_batch(self, conn, lot_no: str, product_model: str, supplier_id: str) -> dict:
        row = db.query_one(
            conn,
            "SELECT sl.*, s.name AS supplier_name FROM supply_lots sl"
            " JOIN suppliers s ON s.id = sl.supplier_id"
            " WHERE sl.lot_no = ? AND sl.product_model = ?",
            (lot_no, product_model),
        )
        if row is None:
            return {
                "acknowledged": False,
                "message": f"供应商 {supplier_id} 批次台账中无 {lot_no}，否认生产该批次。",
            }
        if not row["supplier_confirmed"]:
            return {
                "acknowledged": False,
                "message": f"{row['supplier_name']} 书面否认批次 {lot_no} 为其出厂（批次号疑似伪造）。",
            }
        return {
            "acknowledged": True,
            "message": f"{row['supplier_name']} 确认批次 {lot_no} 为其出厂，发运日期 {row['shipped_at']}。",
        }


class NotificationAdapter:
    """短信/邮件网关（模拟）：发送即成功并给出虚拟回执。"""

    def send(self, channel: str, destination: str, subject: str, body: str) -> dict:
        ref = "MOCK-" + hashlib.sha1(
            f"{channel}|{destination}|{subject}".encode("utf-8")
        ).hexdigest()[:10].upper()
        return {
            "ok": True,
            "provider_ref": ref,
            "channel": channel,
            "destination": destination,
        }


ADAPTERS = {
    "antifake": AntiCounterfeitAdapter,
    "supplier": SupplierAdapter,
    "notification": NotificationAdapter,
}
