# -*- coding: utf-8 -*-
"""HTTP 层：JSON API + 静态前端。零第三方依赖（http.server）。

路由：
  GET  /api/meta                     门店、商品型号（供表单）
  GET  /api/investigations?frozen=1  调查列表（可按冻结批次筛选）
  GET  /api/investigations/{id}      调查详情（证据对照/版本/查询/冻结/通知）
  POST /api/intake                   门店受理
  POST /api/code-search              防伪码查询
  POST /api/hq-review                总部复核（结论版本）
  POST /api/investigations/{id}/notify  门店依据总部结论告知顾客
  POST /api/demo/sell                销售出库（竞态演示）
  GET  /api/inventory                库存（?status=FROZEN/ACTIVE&store=S01）
  GET  /api/audit                    审计日志
  GET  /evidence/<...>               本地证据图片
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import db
from .db import query_all
from .services import LotFrozenError, PermissionError_, Service, ServiceError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CONCLUSION_LABELS = {"GENUINE": "真品", "FAKE": "假冒商品", "INCONCLUSIVE": "无法认定"}


class AppState:
    def __init__(self, conn, evidence_dir: str):
        self.conn = conn
        self.evidence_dir = evidence_dir
        # 串行化所有数据库访问（共用连接），写路径再配合 BEGIN IMMEDIATE
        self.lock = threading.RLock()

    def service(self) -> Service:
        return Service(self.conn, self.evidence_dir)


def _json_default(obj: Any):
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "AntiCounterfeit/1.0"

    # ---- 基础收发 ----
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    # ---- 静态文件 ----
    def _serve_static(self, rel: str) -> None:
        rel = rel or "index.html"
        # 防目录穿越
        safe = os.path.normpath(os.path.join("/", rel)).lstrip(os.sep)
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            self._send_json({"error": "not found", "path": rel}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_evidence(self, rel: str) -> None:
        safe = os.path.normpath(os.path.join("/", rel)).lstrip(os.sep)
        path = os.path.join(self.state.evidence_dir, safe)
        if not os.path.isfile(path):
            self._send_json({"error": "证据文件不存在", "path": safe}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    # ---- 路由 ----
    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._handle_api_get(path, qs)
            elif path.startswith("/evidence/"):
                self._serve_evidence(path[len("/evidence/"):])
            else:
                self._serve_static(path.lstrip("/"))
        except (ServiceError, LotFrozenError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except PermissionError_ as exc:
            self._send_json({"error": str(exc), "permission_denied": True}, 403)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, 404)

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            self._handle_api_post(parsed.path)
        except ServiceError as exc:
            self._send_json({"error": str(exc)}, 400)
        except LotFrozenError as exc:
            self._send_json({"error": str(exc), "lot_frozen": True}, 409)
        except PermissionError_ as exc:
            self._send_json({"error": str(exc), "permission_denied": True}, 403)

    # ---- GET API ----
    def _handle_api_get(self, path: str, qs: dict) -> None:
        with self.state.lock:
            self._handle_api_get_locked(path, qs)

    def _handle_api_get_locked(self, path: str, qs: dict) -> None:
        svc = self.state.service()

        if path == "/api/meta":
            self._send_json({
                "stores": svc.stores(),
                "products": db.rows_to_dicts(query_all(
                    self.state.conn,
                    "SELECT p.*, s.name AS supplier_name FROM products p"
                    " JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.model")),
                "lots": db.rows_to_dicts(query_all(
                    self.state.conn,
                    "SELECT sl.*, s.name AS supplier_name FROM supply_lots sl"
                    " JOIN suppliers s ON s.id=sl.supplier_id ORDER BY sl.lot_no")),
                "conclusions": [{"value": k, "label": v}
                                for k, v in CONCLUSION_LABELS.items()],
            })
            return

        if path == "/api/investigations":
            frozen_only = qs.get("frozen", [""])[0] in ("1", "true", "yes")
            self._send_json({"items": svc.list_investigations(frozen_only=frozen_only)})
            return

        inv_id = self._match(path, r"^/api/investigations/(\d+)$")
        if inv_id is not None:
            self._send_json(svc.detail(inv_id))
            return

        if path == "/api/inventory":
            status = qs.get("status", [None])[0]
            store = qs.get("store", [None])[0]
            self._send_json({"items": svc.list_inventory(status=status, store_id=store)})
            return

        if path == "/api/audit":
            rows = query_all(
                self.state.conn,
                "SELECT a.*, s.name AS actor_name FROM audit_log a"
                " LEFT JOIN stores s ON s.id=a.actor_store ORDER BY a.id DESC LIMIT 200")
            self._send_json({"items": db.rows_to_dicts(rows)})
            return

        self._send_json({"error": "未知接口", "path": path}, 404)

    # ---- POST API ----
    def _handle_api_post(self, path: str) -> None:
        data = self._read_json()
        with self.state.lock:
            svc = self.state.service()

            if path == "/api/intake":
                actor = self._need(data, "actor_store")
                result = svc.intake(
                    actor_store=actor,
                    product_model=self._need(data, "product_model"),
                    receipt_no=(data.get("receipt_no") or "").strip() or None,
                    security_code=(data.get("security_code") or "").strip() or None,
                    lot_no=(data.get("lot_no") or "").strip() or None,
                    package_only=bool(data.get("package_only")),
                    label_damaged=bool(data.get("label_damaged")),
                    seal_intact=not bool(data.get("seal_broken")),
                    customer_name=data.get("customer_name") or None,
                    customer_contact=data.get("customer_contact") or None,
                    notify_channel=data.get("notify_channel") or "SMS",
                )
                self._send_json(result, 201)
                return

            if path == "/api/code-search":
                result = svc.code_search(
                    actor_store=self._need(data, "actor_store"),
                    code=(data.get("code") or "").strip() or None,
                    damaged_hint=bool(data.get("label_damaged")),
                    customer_name=data.get("customer_name") or None,
                    customer_contact=data.get("customer_contact") or None,
                )
                self._send_json(result, 201)
                return

            if path == "/api/hq-review":
                result = svc.hq_review(
                    actor_store=self._need(data, "actor_store"),
                    investigation_id=int(self._need(data, "investigation_id")),
                    conclusion=self._need(data, "conclusion"),
                    reason=self._need(data, "reason"),
                    decided_by=data.get("decided_by") or "当值质控",
                )
                self._send_json(result)
                return

            inv_id = self._match(path, r"^/api/investigations/(\d+)/notify$")
            if inv_id is not None:
                result = svc.notify_customer(
                    actor_store=self._need(data, "actor_store"),
                    investigation_id=inv_id,
                    channel=data.get("channel") or None)
                self._send_json(result, 201)
                return

            if path == "/api/demo/sell":
                result = svc.sell(
                    store_id=self._need(data, "store_id"),
                    receipt_no=self._need(data, "receipt_no"),
                    lot_id=int(self._need(data, "lot_id")),
                    product_model=self._need(data, "product_model"),
                    security_code=data.get("security_code"),
                    customer_name=data.get("customer_name"),
                )
                self._send_json(result, 201)
                return

            self._send_json({"error": "未知接口", "path": path}, 404)

    @staticmethod
    def _need(data: dict, key: str) -> Any:
        val = data.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            raise ServiceError(f"缺少必填字段：{key}")
        return val

    @staticmethod
    def _match(path: str, pattern: str):
        import re
        m = re.match(pattern, path)
        return int(m.group(1)) if m else None


def build_server(db_path: str, evidence_dir: str, host: str, port: int) -> ThreadingHTTPServer:
    conn = db.connect(db_path)
    db.init_db(conn)
    from . import seed as seed_mod
    # seed 内部每个业务动作各自开/提交事务，此处不再外层包事务
    seed_mod.seed(conn, evidence_dir)
    server = ThreadingHTTPServer((host, port), Handler)
    server.state = AppState(conn, evidence_dir)  # type: ignore[attr-defined]
    return server
