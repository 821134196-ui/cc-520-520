#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令启动：

    python3 app.py                # 默认 http://127.0.0.1:8000，自动种子（含重复码案例）
    python3 app.py --reset        # 清空演示库与证据图片后重新种子
    PORT=8080 python3 app.py

零第三方依赖，仅用 Python 标准库。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import db
from .web import build_server

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "authenticity.db")
DEFAULT_EVIDENCE = os.path.join(BASE_DIR, "static", "evidence")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="零售商品真伪受理系统")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", DEFAULT_DB))
    parser.add_argument("--evidence-dir", default=DEFAULT_EVIDENCE)
    parser.add_argument("--reset", action="store_true",
                        help="删除演示数据库与证据图片后重新初始化")
    args = parser.parse_args(argv)

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    os.makedirs(args.evidence_dir, exist_ok=True)

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = args.db + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.isdir(args.evidence_dir):
            shutil.rmtree(args.evidence_dir)
        os.makedirs(args.evidence_dir, exist_ok=True)

    server = build_server(args.db, args.evidence_dir, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 64)
    print(" 零售商品真伪受理系统已启动")
    print(f"  地址：{url}")
    print("  演示线索：重复码 AC-DUP-7777 / 假冒码 AC-FAKE-0001 / 刮损码 AC-DMG-1234")
    print("  停止：Ctrl+C")
    print("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
