"""HTTP 界面与 API（标准库 http.server，零依赖）。

页面：
  /            案件列表，可按“冻结/暂挂批次”筛选
  /case/<id>   证据对照页：按证据来源排列 包装 / 销售单 / 供应批次 /
               防伪平台 / 总部；重复码显示首次出现时间与门店；
               结论多版本完整文字始终可见
  /images/<f>  本地证据照片
  /docs/<f>    文本凭证
操作（POST JSON，x-actor-role 指定身份）：
  /api/cases 受理；/api/cases/<id>/query 查码；/api/cases/<id>/compare 比对；
  /api/cases/<id>/quarantine 标记库存；/api/cases/<id>/conclusion 总部结论；
  /api/cases/<id>/notify 客户告知；/api/sell 销售（演示冻结拦截）
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import services
from .db import connect

VERDICT_CN = {
    "pending_review": "待复查", "genuine": "正品",
    "suspected_counterfeit": "疑似假冒", "counterfeit": "假冒",
    "inconclusive": "证据不足", "sales_mismatch": "销售记录不匹配",
}
STATUS_CN = {"opened": "已受理", "store_checked": "门店已核对",
             "hq_review": "总部复查中", "closed": "已结案"}
INV_CN = {"normal": "正常", "quarantined": "复查暂挂", "frozen": "已冻结",
          "released": "已解除", "condemned": "已没收"}
INV_CLASS = {"normal": "ok", "quarantined": "warn", "frozen": "danger",
             "released": "ok", "condemned": "danger"}
SOURCE_CN = {"customer": "顾客提供 · 包装/小票",
             "store": "门店 · 销售记录与核对",
             "supplier": "供应商 · 供应批次",
             "registry": "品牌防伪平台",
             "hq": "总部质控"}
SOURCE_ORDER = ["customer", "store", "supplier", "registry", "hq"]
KIND_CN = {"package": "外包装", "seal": "封签", "label": "防伪标签",
           "receipt": "销售小票", "sales_record": "销售记录",
           "batch_record": "批次记录", "code_query": "防伪查询",
           "supplier_statement": "供应商声明", "hq_report": "总部报告",
           "comparison": "证据比对", "unit": "商品本体"}


def esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


CSS = """
:root{--bg:#fff;--ink:#333;--line:#e2e2e2;--muted:#777;--warn:#f5c518;
      --warn-bg:#fff8d6;--danger:#b02a2a;--ok:#2e7d46;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{background:#333;color:#fff;padding:14px 28px;display:flex;
 align-items:baseline;gap:16px}
header a{color:#fff;text-decoration:none}
h1{font-size:18px;margin:0}.sub{color:#cfcfcf;font-size:13px}
main{max-width:1080px;margin:24px auto;padding:0 20px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;
 vertical-align:top}
th{background:#f5f5f5;font-weight:600}
tr:nth-child(even) td{background:#fafafa}
a{color:#1a5fb4}
.toolbar{display:flex;gap:12px;align-items:center;margin-bottom:16px;
 flex-wrap:wrap}
.pill{display:inline-block;padding:1px 9px;border-radius:11px;font-size:12px;
 border:1px solid var(--line);white-space:nowrap}
.pill.ok{background:#e8f3ec;color:var(--ok);border-color:#bfe0ca}
.pill.warn{background:var(--warn-bg);color:#6b5500;border-color:#ecd77a}
.pill.danger{background:#fbeaea;color:var(--danger);border-color:#e7bcbc}
.pill.info{background:#eef2f7;color:#444}
.card{border:1px solid var(--line);border-radius:8px;margin:14px 0;overflow:hidden}
.card>h3{margin:0;padding:10px 14px;background:#f5f5f5;font-size:14px;
 border-bottom:1px solid var(--line)}
.card .body{padding:12px 14px}
.src-customer{border-left:4px solid #9aa7b4}
.src-store{border-left:4px solid #5a7a9a}
.src-supplier{border-left:4px solid #7a6a52}
.src-registry{border-left:4px solid var(--warn)}
.src-hq{border-left:4px solid var(--danger)}
.ev{display:flex;gap:12px;padding:10px 14px;border-top:1px solid var(--line)}
.ev:first-of-type{border-top:0}
.ev .thumb{flex:0 0 168px}
.ev img{width:168px;border:1px solid var(--line);border-radius:4px;background:#fff}
.ev pre{white-space:pre-wrap;word-break:break-word;background:#fafafa;
 border:1px solid var(--line);border-radius:4px;padding:8px 10px;margin:6px 0 0;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.meta{color:var(--muted);font-size:12px}
.banner{background:var(--warn-bg);border:1px solid #ecd77a;border-radius:8px;
 padding:12px 16px;margin:14px 0}
.banner.danger{background:#fbeaea;border-color:#e7bcbc}
.conclusion{border:1px solid var(--line);border-radius:8px;padding:14px 16px;
 margin:10px 0}
.conclusion.latest{border:2px solid #333}
.conclusion p{margin:6px 0;white-space:pre-wrap;word-break:break-word;
 overflow-wrap:anywhere}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.filterbox{border:1px solid var(--line);border-radius:6px;padding:8px 12px}
code{background:#f2f2f2;padding:1px 5px;border-radius:3px;font-size:13px}
.notice{font-size:13px;color:var(--muted)}
footer{color:var(--muted);font-size:12px;text-align:center;margin:30px 0}
</style>
"""


def page(title, body):
    return ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body>"
            "<header><h1><a href=/>零售商品真伪质控台账</a></h1>"
            "<span class=sub>受理 · 码查询 · 证据比对 · 批次冻结 · 总部复核 · 客户告知"
            "（模拟环境）</span></header><main>%s</main>"
            "<footer>数据保存在本地 SQLite；防伪查询与短信为模拟适配器</footer>"
            "</body></html>" % (esc(title), CSS, body))


class Handler(BaseHTTPRequestHandler):
    db_path = "data/acf.db"
    data_dir = "data"

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2))

    def _actor(self, payload):
        role = self.headers.get("x-actor-role", payload.pop("_actor", "store"))
        store = self.headers.get("x-actor-store")
        return {"role": role,
                "store_id": int(store) if store else
                (payload.pop("_store_id", 1) if role == "store" else None)}

    def _err(self, e):
        name = type(e).__name__
        code = 403 if name == "PermissionError" else 409 if name in (
            "FrozenBatchError",) else 400
        self._json({"error": name, "message": str(e)}, code)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/":
                return self._list(q.get("frozen", ["0"])[0] == "1")
            if p.startswith("/case/"):
                return self._board(int(p.split("/")[2]))
            if p.startswith("/images/"):
                return self._file(os.path.join(self.data_dir, "images",
                                               os.path.basename(p)), "image/png")
            if p.startswith("/docs/"):
                return self._file(os.path.join(self.data_dir, "docs",
                                               os.path.basename(p)),
                                  "text/plain; charset=utf-8")
            if p == "/api/cases":
                return self._json(services.list_cases(
                    self.db_path, q.get("frozen", ["0"])[0] == "1"))
            self._send(404, "Not Found", "text/plain")
        except Exception as e:
            self._err(e)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._send(404, b"not found", "text/plain")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _list(self, frozen_only):
        rows = services.list_cases(self.db_path, frozen_only)
        trs = []
        for r in rows:
            v = r.get("conclusion_version")
            root = ""
            if r["original_case_id"]:
                root = '<br><span class="meta">重复码归并自原案 #%s</span>' % r["original_case_id"]
            trs.append(
                "<tr><td><a href='/case/%s'>%s</a>%s</td><td>%s</td><td>%s</td>"
                "<td><code>%s</code></td><td>%s</td>"
                "<td><span class='pill info'>%s</span></td>"
                "<td>%s</td><td>%s</td></tr>" % (
                    r["id"], esc(r["case_no"]), root, esc(r["store_name"]),
                    esc(r["product_model"] or "—"), esc(r["code"] or "—"),
                    esc(r["batch_code"] or "—"),
                    esc(STATUS_CN.get(r["status"], r["status"])),
                    ("<span class='pill info'>v%d</span>" % v) if v else
                    "<span class='meta'>—</span>",
                    r["evidence_count"]))
        link = "/?frozen=1" if not frozen_only else "/?frozen=0"
        label = "只看冻结/暂挂批次案件" if not frozen_only else "显示全部案件"
        body = ("<div class=toolbar><strong>共 %d 件调查</strong>"
                "<a class='filterbox' href='%s'>%s</a>"
                "<span class=notice>重复码案件归并到原调查，不另立新案</span></div>"
                "<table><tr><th>案件</th><th>受理门店</th><th>型号</th>"
                "<th>防伪码</th><th>包装批次</th><th>状态</th>"
                "<th>总部结论</th><th>证据数</th></tr>%s</table>"
                % (len(rows), link, label, "".join(trs) or
                   "<tr><td colspan=8>暂无案件</td></tr>"))
        self._send(200, page("案件列表", body), "text/html; charset=utf-8")

    def _board(self, case_id):
        b = services.get_evidence_board(self.db_path, case_id)
        c = b["case"]
        parts = []
        title = ("<h2>案件 %s"
                 "<span class='pill info' style='margin-left:10px'>%s</span></h2>"
                 "<p class=meta>受理门店：%s ｜ 型号：%s ｜ 销售单：%s ｜ "
                 "包装批次：%s ｜ 建案：%s</p>"
                 "<p>顾客描述：%s</p>" % (
                     esc(c["case_no"]), esc(STATUS_CN.get(c["status"], c["status"])),
                     esc(c["store_name"]), esc(c["product_model"] or "—"),
                     esc(c["sale_no"] or "未提供（仅外包装）"),
                     esc(c["batch_code"] or "—"), esc(c["created_at"]),
                     esc(c["customer_desc"] or "—")))
        parts.append(title)

        if c["original_case_id"]:
            parts.append(
                "<div class=banner>⚠ %s</div>" % esc(c["merge_note"] or
                    "本案由重复防伪码归并至原调查"))
        if b["first_seen"]:
            fs = b["first_seen"]
            parts.append(
                "<div class=banner>🔁 防伪码 <code>%s</code> 为重复码："
                "<b>首次出现于 %s、%s</b>；平台累计查询 %d 次，全部关联同一原调查。</div>"
                % (esc(c["code"]), esc(fs["at"]), esc(fs["store"]),
                   fs["query_count"]))

        # 相关库存
        if b["inventory"]:
            rows = "".join(
                "<tr><td>%s</td><td>%s</td><td>%d</td>"
                "<td><span class='pill %s'>%s</span></td></tr>" % (
                    esc(i["batch_code"]), esc(i["store_name"]), i["qty"],
                    INV_CLASS[i["status"]], INV_CN[i["status"]])
                for i in b["inventory"])
            parts.append("<div class=card><h3>相关库存（冻结与结论同事务更新）</h3>"
                         "<table><tr><th>批次</th><th>门店</th><th>数量</th>"
                         "<th>状态</th></tr>%s</table></div>" % rows)

        # 证据：按证据来源排列
        for src in SOURCE_CN:
            evs = b["groups"].get(src, [])
            if not evs:
                continue
            items = []
            for e in evs:
                media = ""
                if e["file_path"]:
                    fn = e["file_path"]
                    if fn.endswith(".png"):
                        media = "<div class=thumb><img src='/images/%s'></div>" % esc(fn)
                    else:
                        media = ("<div class=thumb><a href='/docs/%s'>📎 查看文本凭证 %s"
                                 "</a></div>" % (esc(fn), esc(fn)))
                detail = ("<pre>%s</pre>" % esc(e["content"])) if e["content"] else ""
                from_root = " <span class='pill info'>来自原调查 %s</span>" % esc(
                    c["original_case_no"]) if e.get("from_root") else ""
                items.append(
                    "<div class=ev>%s<div><b>%s</b>%s "
                    "<span class=meta>[%s] · %s · %s 录入</span>%s</div></div>"
                    % (media, esc(e["title"]), from_root,
                       esc(KIND_CN.get(e["kind"], e["kind"])),
                       "顾客" if e["created_by_role"] == "store" and e["source"] == "customer"
                       else ("总部" if e["created_by_role"] == "hq" else "门店"),
                       esc(e["created_at"]), detail))
            parts.append("<div class='card src-%s'><h3>%s（%d）</h3>%s</div>"
                         % (src, esc(SOURCE_CN[src]), len(evs), "".join(items)))

        # 结论版本：完整文字始终可见
        if b["versions"]:
            blocks = []
            for v in b["versions"]:
                latest = " latest" if v["id"] == c["current_conclusion_id"] else ""
                tag = "<span class='pill info'>当前有效</span>" if latest else \
                      "<span class='meta'>已被取代</span>"
                blocks.append(
                    "<div class='conclusion%s'><b>第 %d 版结论 · %s</b> %s"
                    "<p class=meta>出具：总部质控 ｜ %s ｜ 库存动作：%s ｜ %s</p>"
                    "<p>%s</p><p class=meta>理由：%s</p></div>" % (
                        latest, v["version"],
                        esc(VERDICT_CN.get(v["verdict"], v["verdict"])), tag,
                        esc(v["created_at"]), esc(v["batch_action"]),
                        esc("改判，取代 v%d" % (v["version"] - 1))
                        if v["supersedes"] else "初判",
                        esc(v["text"]), esc(v["reason"] or "—")))
            parts.append("<h3>总部质控结论（版本留痕，门店不可修改）</h3>"
                         + "".join(blocks))
        else:
            parts.append("<p class=notice>总部结论尚未出具。</p>")

        self._send(200, page("案件 " + c["case_no"], "".join(parts)),
                   "text/html; charset=utf-8")

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        actor = self._actor(payload)
        try:
            if p == "/api/cases":
                r = services.accept_case(self.db_path, actor, **payload)
                return self._json({"ok": True, "case": r})
            if p == "/api/sell":
                r = services.sell_item(self.db_path, actor, **payload)
                return self._json({"ok": True, **r})
            parts = p.split("/")
            if len(parts) >= 4 and parts[1] == "api" and parts[2] == "cases":
                cid = int(parts[3])
                action = parts[4] if len(parts) > 4 else ""
                if action == "query":
                    return self._json({"ok": True,
                        **services.query_code(self.db_path, actor, cid)})
                if action == "compare":
                    return self._json({"ok": True,
                        **services.compare_evidence(self.db_path, actor, cid)})
                if action == "quarantine":
                    return self._json({"ok": True,
                        **services.mark_related_inventory(self.db_path, actor, cid)})
                if action == "conclusion":
                    return self._json({"ok": True,
                        **services.issue_conclusion(self.db_path, actor, cid,
                                                    **payload)})
                if action == "notify":
                    return self._json({"ok": True,
                        **services.notify_customer(self.db_path, actor, cid)})
            self._send(404, "Not Found", "text/plain")
        except Exception as e:
            self._err(e)


def run(db_path="data/acf.db", data_dir="data", host="127.0.0.1", port=8000):
    Handler.db_path = db_path
    Handler.data_dir = data_dir
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("真伪质控台账已启动：http://%s:%d" % (host, port))
    print("列表页（可筛冻结批次）：http://%s:%d/?frozen=1" % (host, port))
    httpd.serve_forever()
