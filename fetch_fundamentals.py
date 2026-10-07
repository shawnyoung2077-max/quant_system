"""
fetch_fundamentals.py - 抓取全市场 A股基本面（断点续传 + 分片并行）
==================================================================
把沪深300+中证500（约800只）的季度财务指标逐只缓存到 data/fundamentals/<code>.csv。
已缓存的会自动跳过；多进程分片并行（各自独立 V8，不会崩）。

用法：
    python fetch_fundamentals.py --slices 4 --slice-index 0   # 各分片分别跑
    python fetch_fundamentals.py --assemble-check              # 看已缓存数量
"""

import os
import sys

import run as R
from fundamentals import ensure_stock_fundamentals

DATA_DIR = "data/fundamentals"


def list_cached():
    if not os.path.exists(DATA_DIR):
        return set()
    return {f[:-4] for f in os.listdir(DATA_DIR) if f.endswith(".csv")}


def fetch_all(slice_index=0, num_slices=1):
    prices = R.load_prices()
    symbols = list(prices.columns)
    cached = list_cached()
    pending = [s for s in symbols if s not in cached]
    mine = pending[slice_index::num_slices] if num_slices > 1 else pending
    print(f"[fund] cached={len(cached)} pending={len(pending)} | slice {len(mine)} ({slice_index+1}/{num_slices})")
    n_ok = 0
    for i, sym in enumerate(mine, 1):
        df = ensure_stock_fundamentals(sym)
        if not df.empty:
            n_ok += 1
        if i % 20 == 0 or i == len(mine):
            print(f"[fund] {i}/{len(mine)} done ({n_ok} ok)")
            sys.stdout.flush()
    print(f"[fund] slice finished: {n_ok}/{len(mine)} ok")


if __name__ == "__main__":
    if "--assemble-check" in sys.argv:
        print(f"已缓存基本面: {len(list_cached())}/{R.load_prices().shape[1]}")
    else:
        slice_index = 0; num_slices = 1
        if "--slices" in sys.argv:
            num_slices = int(sys.argv[sys.argv.index("--slices") + 1])
        if "--slice-index" in sys.argv:
            slice_index = int(sys.argv[sys.argv.index("--slice-index") + 1])
        fetch_all(slice_index, num_slices)
