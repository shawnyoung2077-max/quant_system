"""
fetch_market.py - 抓取全市场数据（断点续传）
============================================
抓取 沪深300 + 中证500（约800只A股）10 年日线，逐只存到 data/stocks/<code>.csv，
并记录进度到 data/progress.txt。中断后重跑会自动跳过已完成的股票（断点续传）。

全部完成后调用 assemble() 把逐只数据组装成"日期 x 股票"面板存 a_share_close.csv。

用法：
    python fetch_market.py              # 开始/继续抓取
    python fetch_market.py --assemble   # 只组装面板（跳过抓取）
"""

import os
import sys
import time

import numpy as np
import pandas as pd

from realdata import fetch_a_share_daily, to_tx_symbol

DATA_DIR = "data/stocks"
PROGRESS_FILE = "data/progress.txt"
PANEL_FILE = "data/a_share_close.csv"
CONST_FILE = "data/constituents.txt"
START_DATE = "20150101"  # 约 10 年


def get_constituents() -> list:
    """返回沪深300 + 中证500 成分股代码（去重）。优先读本地缓存，避免每次重启联网。"""
    # 读缓存
    if os.path.exists(CONST_FILE):
        with open(CONST_FILE, "r", encoding="utf-8") as f:
            cached = [ln.strip() for ln in f if ln.strip()]
        if cached:
            print(f"[constituents] loaded {len(cached)} from cache")
            return cached
    # 联网拉取
    import akshare as ak
    codes = []
    for idx in ("000300", "000905"):
        try:
            df = ak.index_stock_cons_csindex(symbol=idx)
            col = "成分券代码" if "成分券代码" in df.columns else df.columns[4]
            codes.extend(df[col].astype(str).str.zfill(6).tolist())
        except Exception as e:
            print(f"[warn] constituents {idx} failed: {type(e).__name__}")
    codes = sorted(set(codes))
    # 写缓存
    if codes:
        os.makedirs("data", exist_ok=True)
        with open(CONST_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(codes))
        print(f"[constituents] saved {len(codes)} to cache")
    print(f"[constituents] {len(codes)} unique stocks")
    return codes


def load_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_symbol(symbol: str, df: pd.DataFrame) -> None:
    """保存单只股票日线并记录进度。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}.csv")
    df.to_csv(path, index=False)
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(symbol + "\n")


def _fetch_one(code: str) -> bool:
    """抓取单只并保存，返回是否成功。"""
    df = fetch_a_share_daily(code, start=START_DATE)
    if df is not None and not df.empty:
        df = df[["date", "close"]]
        save_symbol(code, df)
        return True
    return False


def fetch_all(slice_index: int = 0, num_slices: int = 1) -> None:
    """
    逐只抓取，断点续传。
    本环境同进程多线程会触发 V8 崩溃，故单进程串行；
    要并行请启动多个独立进程并给不同 --slice-index（各自独立 V8，不会崩）。
    """
    codes = get_constituents()
    done = load_progress()
    pending = [c for c in codes if c not in done]
    # 分片：只处理属于本进程的子集
    mine = pending[slice_index::num_slices] if num_slices > 1 else pending
    print(f"[fetch] done={len(done)} pending={len(pending)} | this slice {len(mine)} ({slice_index+1}/{num_slices})")

    n_ok = 0
    for i, code in enumerate(mine, 1):
        try:
            if _fetch_one(code):
                n_ok += 1
        except Exception:
            pass
        if i % 50 == 0 or i == len(mine):
            print(f"[fetch] {i}/{len(mine)} processed ({n_ok} ok)")
            sys.stdout.flush()
    print(f"[fetch] slice finished: {n_ok}/{len(mine)} ok")


def assemble() -> pd.DataFrame:
    """把所有 data/stocks/<code>.csv 组装成面板并保存。"""
    files = [f for f in os.listdir(DATA_DIR) if f.endswith(".csv")]
    frames = []
    skipped = 0
    for fn in files:
        code = fn[:-4]
        df = pd.read_csv(os.path.join(DATA_DIR, fn), parse_dates=["date"])
        if df.empty:
            skipped += 1
            continue
        s = df.set_index("date")["close"].rename(code)
        s = s[~s.index.duplicated()]
        frames.append(s)
    panel = pd.concat(frames, axis=1).sort_index()
    panel = panel[panel.index >= pd.Timestamp(START_DATE)]
    panel = panel.dropna(axis=1, how="all")
    os.makedirs("data", exist_ok=True)
    panel.to_csv(PANEL_FILE)
    print(f"[assemble] {panel.shape[1]} stocks x {panel.shape[0]} days (skipped {skipped})")
    print(f"[assemble] saved -> {PANEL_FILE}")
    return panel


if __name__ == "__main__":
    if "--assemble" in sys.argv:
        assemble()
    else:
        slice_index = 0
        num_slices = 1
        if "--slices" in sys.argv:
            num_slices = int(sys.argv[sys.argv.index("--slices") + 1])
        if "--slice-index" in sys.argv:
            slice_index = int(sys.argv[sys.argv.index("--slice-index") + 1])
        fetch_all(slice_index=slice_index, num_slices=num_slices)
        # 只有完整单进程（不分片）才自动组装，避免多进程重复组装
        if num_slices <= 1:
            print("[fetch] done, now assembling...")
            assemble()
