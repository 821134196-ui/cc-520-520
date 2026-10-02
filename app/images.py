"""本地“证据照片”生成器：用标准库 zlib/struct 直接编码 PNG，零第三方依赖。

真实门店会上传拍摄照片；本模拟环境用程序化生成的占位图片代替：
外包装、封签（完好/破损）、防伪标签（正常/刮损）、销售小票、商品本体。
另生成文本版虚构凭证（供应商声明、总部复查报告）。
"""
from __future__ import annotations

import os
import struct
import zlib

# ---- 极简 5x7 点阵字体（ASCII 32~95 的子集），行数据位 4..0 为自左向右 ----
_F = {
    " ": [0, 0, 0, 0, 0, 0, 0], "!": [4, 4, 4, 4, 4, 0, 4], '"': [5, 5, 2, 0, 0, 0, 0],
    "#": [10, 10, 31, 10, 31, 10, 10], "$": [4, 15, 20, 14, 5, 30, 4], "%": [25, 25, 2, 4, 8, 19, 19],
    "&": [6, 9, 10, 4, 10, 9, 13], "'": [4, 4, 8, 0, 0, 0, 0], "(": [2, 4, 8, 8, 8, 4, 2],
    ")": [8, 4, 2, 2, 2, 4, 8], "*": [0, 4, 21, 14, 21, 4, 0], "+": [0, 4, 4, 31, 4, 4, 0],
    ",": [0, 0, 0, 0, 12, 4, 8], "-": [0, 0, 0, 31, 0, 0, 0], ".": [0, 0, 0, 0, 0, 12, 12],
    "/": [1, 2, 2, 4, 8, 8, 16],
    "0": [14, 17, 19, 21, 25, 17, 14], "1": [4, 12, 4, 4, 4, 4, 14],
    "2": [14, 17, 1, 2, 4, 8, 31], "3": [31, 2, 4, 2, 1, 17, 14],
    "4": [2, 6, 10, 18, 31, 2, 2], "5": [31, 16, 30, 1, 1, 17, 14],
    "6": [6, 8, 16, 30, 17, 17, 14], "7": [31, 1, 2, 4, 8, 8, 8],
    "8": [14, 17, 17, 14, 17, 17, 14], "9": [14, 17, 17, 15, 1, 2, 12],
    ":": [0, 12, 12, 0, 12, 12, 0], ";": [0, 12, 12, 0, 12, 4, 8],
    "<": [2, 4, 8, 16, 8, 4, 2], "=": [0, 0, 31, 0, 31, 0, 0], ">": [8, 4, 2, 1, 2, 4, 8],
    "?": [14, 17, 1, 2, 4, 0, 4], "@": [14, 17, 1, 13, 21, 21, 14],
    "A": [14, 17, 17, 31, 17, 17, 17], "B": [30, 17, 17, 30, 17, 17, 30],
    "C": [14, 17, 16, 16, 16, 17, 14], "D": [28, 18, 17, 17, 17, 18, 28],
    "E": [31, 16, 16, 30, 16, 16, 31], "F": [31, 16, 16, 30, 16, 16, 16],
    "G": [14, 17, 16, 23, 17, 17, 15], "H": [17, 17, 17, 31, 17, 17, 17],
    "I": [14, 4, 4, 4, 4, 4, 14], "J": [7, 2, 2, 2, 2, 18, 12],
    "K": [17, 18, 20, 24, 20, 18, 17], "L": [16, 16, 16, 16, 16, 16, 31],
    "M": [17, 27, 21, 21, 17, 17, 17], "N": [17, 25, 21, 19, 17, 17, 17],
    "O": [14, 17, 17, 17, 17, 17, 14], "P": [30, 17, 17, 30, 16, 16, 16],
    "Q": [14, 17, 17, 17, 21, 18, 13], "R": [30, 17, 17, 30, 20, 18, 17],
    "S": [15, 16, 16, 14, 1, 1, 30], "T": [31, 4, 4, 4, 4, 4, 4],
    "U": [17, 17, 17, 17, 17, 17, 14], "V": [17, 17, 17, 17, 17, 10, 4],
    "W": [17, 17, 17, 21, 21, 27, 17], "X": [17, 17, 10, 4, 10, 17, 17],
    "Y": [17, 17, 10, 4, 4, 4, 4], "Z": [31, 1, 2, 4, 8, 16, 31],
    "[": [14, 8, 8, 8, 8, 8, 14], "\\": [16, 8, 8, 4, 2, 2, 1], "]": [14, 2, 2, 2, 2, 2, 14],
    "^": [4, 10, 17, 0, 0, 0, 0], "_": [0, 0, 0, 0, 0, 0, 31],
    "`": [8, 4, 2, 0, 0, 0, 0],
}

WHITE = (255, 255, 255)
DARK = (51, 51, 51)
GRAY = (140, 140, 140)
LIGHT = (235, 235, 235)
YELLOW = (245, 197, 24)
RED = (176, 42, 42)
GREEN = (46, 125, 70)


class Canvas:
    def __init__(self, w: int = 640, h: int = 420, bg=WHITE):
        self.w, self.h = w, h
        self.px = bytearray(b"".join(bytes(bg) for _ in range(w * h)))

    def _set(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x, y, w, h, c, fill=True):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if fill or xx in (x, x + w - 1) or yy in (y, y + h - 1):
                    self._set(xx, yy, c)

    def line(self, x0, y0, x1, y1, c, t=1):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for ox in range(-(t // 2), t // 2 + 1):
                for oy in range(-(t // 2), t // 2 + 1):
                    self._set(x0 + ox, y0 + oy, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def circle(self, cx, cy, r, c, t=2):
        for a in range(0, 360, 3):
            import math
            x = cx + int(r * math.cos(a * math.pi / 180))
            y = cy + int(r * math.sin(a * math.pi / 180))
            for ox in range(-t // 2, t // 2 + 1):
                for oy in range(-t // 2, t // 2 + 1):
                    self._set(x + ox, y + oy, c)

    def text(self, s, x, y, c=DARK, scale=2):
        cx = x
        for ch in str(s).upper():
            glyph = _F.get(ch)
            if glyph is None and ch.lower() != ch:
                glyph = _F.get(ch.lower())
            if glyph:
                for ry, row in enumerate(glyph):
                    for rx in range(5):
                        if row & (16 >> rx):
                            self.rect(cx + rx * scale, y + ry * scale, scale, scale, c)
            cx += 6 * scale
            if cx > self.w - 30:
                cx, y = x, y + 9 * scale

    def header(self, title, tag=None, tag_c=GRAY):
        self.rect(0, 0, self.w, 52, DARK)
        self.text(title, 20, 16, WHITE, 2)
        if tag:
            tw = len(tag) * 12 + 16
            self.rect(self.w - tw - 16, 12, tw, 28, tag_c)
            self.text(tag, self.w - tw - 8, 19, DARK, 2)

    def png_bytes(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))
        comp = zlib.compress(raw, 9)

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def _save(canvas: Canvas, image_dir: str, name: str) -> str:
    os.makedirs(image_dir, exist_ok=True)
    path = os.path.join(image_dir, name)
    with open(path, "wb") as f:
        f.write(canvas.png_bytes())
    return name


def _barcode(cv: Canvas, x, y, w, h, seed: int):
    """由种子生成确定性的竖条，模拟商品条码。"""
    v = seed or 1
    cx = x
    while cx < x + w:
        v = (v * 1103515245 + 12345) & 0x7FFFFFFF
        bw = 1 + v % 4
        if v % 3:
            cv.rect(cx, y, bw, h, DARK)
        cx += bw + 1


def package_photo(image_dir, name, code, batch, seal_ok=True, tag=None):
    cv = Canvas()
    cv.header("PRODUCT PACKAGE", tag or ("SEAL OK" if seal_ok else "SEAL BROKEN"),
              GREEN if seal_ok else RED)
    cv.rect(30, 80, 580, 300, LIGHT)
    cv.rect(30, 80, 580, 300, DARK, fill=False)
    cv.text("AUTHENTIC LABEL AREA", 60, 110, GRAY, 1)
    cv.text(code, 60, 140, DARK, 2)
    cv.text("BATCH " + batch, 60, 180, DARK, 2)
    _barcode(cv, 60, 220, 300, 70, hash(code) & 0xFFFF)
    # 封签：跨盒盖封口的贴纸
    cv.rect(240, 70, 160, 26, YELLOW)
    cv.text("SEAL", 292, 77, DARK, 2)
    if not seal_ok:
        cv.line(250, 70, 390, 96, RED, 3)
        cv.line(390, 70, 250, 96, RED, 3)
    return _save(cv, image_dir, name)


def seal_photo(image_dir, name, ok=True):
    cv = Canvas()
    cv.header("TAMPER SEAL CLOSE-UP", "INTACT" if ok else "DAMAGED",
              GREEN if ok else RED)
    cv.rect(60, 90, 520, 220, LIGHT)
    cv.circle(320, 200, 80, YELLOW, 6)
    cv.text("SEAL", 278, 190, DARK, 3)
    if not ok:
        cv.line(250, 140, 390, 260, RED, 4)
        cv.line(390, 140, 260, 250, RED, 4)
        cv.text("EDGE LIFTED - GLUE RESIDUE", 130, 330, RED, 1)
    return _save(cv, image_dir, name)


def label_photo(image_dir, name, code, scratched=False):
    cv = Canvas()
    cv.header("ANTI-COUNTERFEIT CODE LABEL",
              "SCRATCHED" if scratched else "NORMAL", RED if scratched else GREEN)
    cv.rect(60, 100, 520, 180, LIGHT)
    cv.text(code, 90, 150, DARK if not scratched else GRAY, 3)
    cv.text("COAT WITH SILVER SCRATCH FILM", 90, 220, GRAY, 1)
    if scratched:
        # 确定性的刮痕：交叉斜线覆盖涂层区域
        for i, x in enumerate(range(90, 560, 46)):
            cv.line(x, 120, x + 30, 250, GRAY, 5)
            cv.line(x + 30, 120, x, 250, DARK, 2)
        cv.text("PARTIAL DIGITS UNREADABLE", 90, 310, RED, 2)
    return _save(cv, image_dir, name)


def receipt_photo(image_dir, name, sale_no, model, date):
    cv = Canvas()
    cv.header("SALES RECEIPT (CUSTOMER)", "PHOTO COPY", GRAY)
    cv.rect(80, 80, 480, 300, WHITE)
    cv.rect(80, 80, 480, 300, DARK, fill=False)
    cv.text("RETAIL STORE RECEIPT", 150, 105, DARK, 2)
    cv.text("NO: " + sale_no, 110, 160, DARK, 2)
    cv.text("MODEL: " + model, 110, 200, DARK, 2)
    cv.text("DATE: " + date, 110, 240, DARK, 2)
    cv.line(110, 290, 530, 290, DARK, 1)
    cv.text("STAMP", 380, 310, GRAY, 2)
    return _save(cv, image_dir, name)


def unit_photo(image_dir, name, code, ok=True):
    cv = Canvas()
    cv.header("PRODUCT UNIT", "OK" if ok else "SUSPECT", GREEN if ok else RED)
    cv.rect(120, 100, 400, 240, LIGHT)
    cv.rect(120, 100, 400, 240, DARK, fill=False)
    cv.text("UNIT SN", 220, 140, GRAY, 1)
    cv.text(code, 160, 180, DARK, 2)
    if not ok:
        cv.text("WORKMANSHIP ANOMALY", 170, 270, RED, 2)
    return _save(cv, image_dir, name)


def text_voucher(doc_dir: str, name: str, title: str, lines: list[str]) -> str:
    """文本版虚构凭证：供应商声明、总部复查报告等。"""
    os.makedirs(doc_dir, exist_ok=True)
    path = os.path.join(doc_dir, name)
    bar = "=" * 56
    body = "\n".join([bar, title.center(44), bar, ""])
    body += "\n\n".join(lines)
    body += "\n\n" + bar + "\n（本凭证为模拟环境自动生成的虚构文件）\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return name
