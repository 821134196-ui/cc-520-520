# -*- coding: utf-8 -*-
"""本地证据图片生成：全部为虚构 SVG 凭证（包装、封签、销售单、供应商函件、总部复查单）。

不依赖任何外部资源；用有限的警示黄标识异常（刮损/封签破损/否认批次）。
"""
from __future__ import annotations

import os
from typing import Optional

INK = "#1f2933"      # 深灰主文字
SUB = "#66707a"      # 次要文字
LINE = "#c7cdd4"     # 分隔线
BG = "#ffffff"
WARN = "#e0a800"     # 有限警示黄
WARN_BG = "#fdf6d8"  # 浅黄底

W, H = 720, 460


def _esc(text: object) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _doc_frame(title: str, badge: Optional[str] = None, warn: bool = False) -> str:
    badge_svg = ""
    if badge:
        fill = WARN_BG if warn else "#eef1f4"
        stroke = WARN if warn else LINE
        badge_svg = (
            f'<rect x="500" y="28" width="180" height="34" rx="4" fill="{fill}" stroke="{stroke}"/>'
            f'<text x="590" y="51" text-anchor="middle" font-size="15" '
            f'fill="{WARN if warn else INK}" font-weight="700">{_esc(badge)}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="Noto Sans CJK SC, WenQuanYi Zen Hei, sans-serif">'
        f'<rect width="{W}" height="{H}" fill="{BG}"/>'
        f'<rect x="20" y="20" width="{W-40}" height="{H-40}" fill="none" stroke="{LINE}" stroke-width="1.5"/>'
        f'<text x="40" y="56" font-size="22" font-weight="700" fill="{INK}">{_esc(title)}</text>'
        f'<line x1="40" y1="72" x2="{W-40}" y2="72" stroke="{LINE}"/>'
        f'{badge_svg}'
    )


def _lines(lines: list[tuple[str, str]], y0: int = 112, gap: int = 36, warn_keys=()) -> str:
    out = []
    y = y0
    for k, v in lines:
        if k in warn_keys:
            v_attrs = f' fill="{WARN}" font-weight="700"'
        else:
            v_attrs = f' fill="{INK}"'
        out.append(
            f'<text x="40" y="{y}" font-size="16" fill="{SUB}">{_esc(k)}</text>'
            f'<text x="180" y="{y}" font-size="16"{v_attrs}>{_esc(v)}</text>'
        )
        y += gap
    return "".join(out)


def _save(path: str, svg: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg + "</svg>")
    return path


def packaging(path: str, model: str, name: str, lot_no: str, code: str,
              damaged: bool = False, package_only: bool = False) -> str:
    badge = "标签刮损" if damaged else ("仅外包装" if package_only else "包装完整")
    svg = _doc_frame("证物照片 · 商品外包装", badge, warn=(damaged or package_only))
    # 包装盒示意
    svg += (f'<rect x="40" y="100" width="260" height="300" rx="6" fill="#f4f6f8" stroke="{LINE}"/>'
            f'<rect x="60" y="120" width="220" height="70" rx="4" fill="{BG}" stroke="{LINE}"/>'
            f'<text x="170" y="152" text-anchor="middle" font-size="17" font-weight="700" fill="{INK}">{_esc(name)}</text>'
            f'<text x="170" y="178" text-anchor="middle" font-size="14" fill="{SUB}">型号 {_esc(model)}</text>')
    shown_code = code if not damaged else (code[:6] + " ██刮损██ " if code else "██刮损无法识读██")
    code_fill = WARN if damaged else INK
    svg += (f'<rect x="60" y="210" width="220" height="86" rx="4" fill="{BG}" stroke="{LINE}"/>'
            f'<text x="170" y="238" text-anchor="middle" font-size="13" fill="{SUB}">防伪码</text>'
            f'<text x="170" y="268" text-anchor="middle" font-size="15" font-weight="700" fill="{code_fill}">{_esc(shown_code)}</text>'
            f'<text x="170" y="288" text-anchor="middle" font-size="12" fill="{SUB}">（刮刮银涂层）</text>'
            f'<text x="170" y="330" text-anchor="middle" font-size="13" fill="{SUB}">包装批次 {_esc(lot_no)}</text>'
            f'<text x="170" y="372" text-anchor="middle" font-size="12" fill="{SUB}">拍摄：受理店员 · 顾客提交</text>')
    warn_lines = ()
    if damaged:
        warn_lines = ("标签状态",)
    svg += _lines([
        ("商品型号", model),
        ("包装批次", lot_no),
        ("防伪码", shown_code),
        ("标签状态", "涂层刮损/字符缺失" if damaged else "完好可识读"),
        ("商品本体", "顾客仅携带外包装，未见本体" if package_only else "包装+本体均在场"),
    ], warn_keys=warn_lines)
    return _save(path, svg)


def seal(path: str, intact: bool = True) -> str:
    badge = "封签破损" if not intact else "封签完好"
    svg = _doc_frame("证物照片 · 防伪封签", badge, warn=not intact)
    cx, cy = 200, 250
    color = "#2e7d32" if intact else WARN
    svg += (f'<circle cx="{cx}" cy="{cy}" r="90" fill="none" stroke="{color}" stroke-width="3"/>'
            f'<circle cx="{cx}" cy="{cy}" r="70" fill="none" stroke="{color}" stroke-width="1.5"/>')
    if intact:
        svg += (f'<text x="{cx}" y="{cy-6}" text-anchor="middle" font-size="18" font-weight="700" fill="{color}">正品封签</text>'
                f'<text x="{cx}" y="{cy+22}" text-anchor="middle" font-size="13" fill="{SUB}">封口完整 · 无二次粘贴</text>')
    else:
        # 撕裂纹
        pts = "150,180 176,214 158,244 192,278 176,320"
        svg += (f'<polyline points="{pts}" fill="none" stroke="{WARN}" stroke-width="4"/>'
                f'<text x="{cx}" y="{cy+128}" text-anchor="middle" font-size="14" fill="{WARN}" font-weight="700">封签断裂/揭贴痕迹</text>')
    svg += _lines([
        ("封签状态", "完好" if intact else "破损"),
        ("封口情况", "无拆动" if intact else "有拆动痕迹"),
        ("判定", "包装链路连续" if intact else "包装可能被调换"),
    ], warn_keys=() if intact else ("封签状态", "封口情况", "判定"))
    return _save(path, svg)


def receipt(path: str, receipt_no: str, store_name: str, model: str,
            code: Optional[str], sold_at: str, mismatch: Optional[str] = None) -> str:
    badge = "记录不匹配" if mismatch else "销售凭证"
    svg = _doc_frame("销售单（本店存根）", badge, warn=bool(mismatch))
    rows = [
        ("销售单号", receipt_no),
        ("售出门店", store_name),
        ("商品型号", model),
        ("售出时间", sold_at),
        ("登记防伪码", code or "（未登记）"),
    ]
    svg += _lines(rows, warn_keys=("登记防伪码",) if mismatch else ())
    if mismatch:
        svg += (f'<rect x="40" y="320" width="640" height="80" rx="6" fill="{WARN_BG}" stroke="{WARN}"/>'
                f'<text x="58" y="352" font-size="15" font-weight="700" fill="{WARN}">比对异常</text>'
                f'<text x="58" y="382" font-size="14" fill="{INK}">{_esc(mismatch)}</text>')
    return _save(path, svg)


def supplier_letter(path: str, supplier: str, lot_no: str, acknowledged: bool) -> str:
    badge = "供应商否认" if not acknowledged else "批次确认"
    svg = _doc_frame("供应商批次确认函（模拟凭证）", badge, warn=not acknowledged)
    svg += _lines([
        ("供应商", supplier),
        ("涉疑批次", lot_no),
        ("回函结论", "确认该批次为我司出厂" if acknowledged else "否认：该批次非我司生产"),
        ("回函方式", "供应商门户电子回函"),
    ], warn_keys=() if acknowledged else ("回函结论",))
    if not acknowledged:
        svg += (f'<rect x="40" y="300" width="640" height="100" rx="6" fill="{WARN_BG}" stroke="{WARN}"/>'
                f'<text x="58" y="332" font-size="15" font-weight="700" fill="{WARN}">风险提示</text>'
                f'<text x="58" y="362" font-size="14" fill="{INK}">批次号在供应商出厂台账中不存在，外包装批次信息涉嫌伪造，</text>'
                f'<text x="58" y="388" font-size="14" fill="{INK}">建议同线索库存立即冻结并等待总部质控复查。</text>')
    return _save(path, svg)


def hq_report(path: str, case_no: str, conclusion: str, version: int, reason: str) -> str:
    warn = conclusion == "FAKE"
    badge = {"FAKE": "复查：假冒", "GENUINE": "复查：真品",
             "INCONCLUSIVE": "复查：无法认定"}.get(conclusion, "总部复查单")
    svg = _doc_frame("总部质控复查意见书", badge, warn=warn)
    svg += _lines([
        ("调查编号", case_no),
        ("结论版本", f"V{version}"),
        ("质控结论", {"FAKE": "假冒商品", "GENUINE": "真品",
                   "INCONCLUSIVE": "无法认定"}.get(conclusion, conclusion)),
        ("出具部门", "总部质量管理中心"),
    ], warn_keys=("质控结论",) if warn else ())
    # reason 完整呈现，自动折行
    svg += f'<text x="40" y="300" font-size="14" fill="{SUB}">复查说明</text>'
    chars_per_line = 30
    lines = [reason[i:i + chars_per_line] for i in range(0, len(reason), chars_per_line)] or [""]
    y = 330
    for ln in lines[:4]:
        svg += f'<text x="40" y="{y}" font-size="14" fill="{INK}">{_esc(ln)}</text>'
        y += 26
    return _save(path, svg)
