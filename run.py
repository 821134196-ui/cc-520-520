#!/usr/bin/env python3
"""一条本地命令启动：建库 -> 灌入含“重复码”的演示数据 -> 启动 Web。

    python3 run.py            # 默认 http://127.0.0.1:8000
    python3 run.py --reseed   # 重置演示数据后启动
    python3 run.py --seed-only
    PORT=8080 python3 run.py
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import seed as seed_mod
from app import server

DB_PATH = os.path.join("data", "acf.db")
DATA_DIR = "data"


def main():
    ap = argparse.ArgumentParser(description="零售商品真伪质控台账（模拟）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--reseed", action="store_true",
                    help="重置数据库并重新生成演示数据")
    ap.add_argument("--seed-only", action="store_true",
                    help="只生成演示数据，不启动服务")
    args = ap.parse_args()

    need_seed = args.reseed or args.seed_only or not os.path.exists(DB_PATH)
    if not need_seed and os.path.exists(DB_PATH):
        # 库里没有任何案件（例如首次播种曾中断）时自动补播
        from app.db import connect as _conn
        _c = _conn(DB_PATH)
        try:
            need_seed = _c.execute("SELECT COUNT(*) c FROM cases").fetchone()["c"] == 0
        finally:
            _c.close()
    if need_seed:
        print("生成演示数据（含重复码案例 ACF-A1-88880001）...")
        ids = seed_mod.seed(DB_PATH, DATA_DIR)
        print("  重复码原案 #%s；2号店重复案件 #%s"
              % (ids["A_original"], ids["A_duplicate"]))
        print("  已归并：%s" % ids.get("A_merge_note"))
        print("  其他场景：标签刮损 #%s / 销售不匹配 #%s / 仅外包装 #%s /"
              " 供应商否认 #%s / 复查改判 #%s"
              % (ids["B"], ids["C"], ids["D"], ids["E"], ids["F"]))

    if args.seed_only:
        print("数据与证据已生成于 %s/" % DATA_DIR)
        return

    server.run(DB_PATH, DATA_DIR, args.host, args.port)


if __name__ == "__main__":
    main()
