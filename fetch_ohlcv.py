"""
fetch_ohlcv.py - 抓取全市场 OHLCV（开高低量），断点续传 + 分片并行
==================================================================
为条件反转等需要 high/low/volume 的因子提供数据。
逐只存到 data/ohlcv/<code>.csv（date,open,high,low,close,volume）。

用法：
    python fetch_ohlcv.py --slices 4 --slice-index 0   # 各分片分别跑
"""

import os
import sys

import run as R
from realdata import fetch_a_share_daily

DATA_DIR = "data/ohlcv"


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
    print(f"[ohlcv] cached={len(cached)} pending={len(pending)} | slice {len(mine)} ({slice_index+1}/{num_slices})")
    os.makedirs(DATA_DIR, exist_ok=True)
    n_ok = 0
    for i, sym in enumerate(mine, 1):
        df = fetch_a_share_daily(sym, start="20150101")
        if df is not None and not df.empty:
            df[["date", "open", "high", "low", "close", "volume"]].to_csv(
                os.path.join(DATA_DIR, f"{sym}.csv"), index=False)
            n_ok += 1
        if i % 20 == 0 or i == len(mine):
            print(f"[ohlcv] {i}/{len(mine)} done ({n_ok} ok)")
            sys.stdout.flush()
    print(f"[ohlcv] slice finished: {n_ok}/{len(mine)} ok")


if __name__ == "__main__":
    si = 0; ns = 1
    if "--slices" in sys.argv:
        ns = int(sys.argv[sys.argv.index("--slices") + 1])
    if "--slice-index" in sys.argv:
        si = int(sys.argv[sys.argv.index("--slice-index") + 1])
    fetch_all(si, ns)
