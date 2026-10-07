"""
data_ohlcv.py - OHLCV（开高低量）数据面板
==========================================
从 data/ohlcv/<code>.csv（抓取自 fetch_ohlcv.py）拼出对齐的开高低量面板，
供需要 high/low/volume 的因子使用（如条件反转、量价类 alpha）。

用法：
    from data_ohlcv import load_ohlcv_panels
    o = load_ohlcv_panels(symbols=...)   # {close,high,low,volume}，均为 date×stock
"""

import os
import glob

import numpy as np
import pandas as pd

DATA_DIR = "data/ohlcv"


def list_available():
    if not os.path.exists(DATA_DIR):
        return set()
    return {os.path.basename(f)[:-4] for f in glob.glob(os.path.join(DATA_DIR, "*.csv"))}


def load_ohlcv_panels(symbols=None, cached_only=True) -> dict:
    """
    加载 OHLCV 面板。返回 {close,high,low,volume}，各为 date×stock DataFrame。
    symbols 缺省用所有已缓存股票。
    """
    avail = list_available()
    if symbols is not None:
        avail = [s for s in symbols if s in avail]
    else:
        avail = sorted(avail)
    cols = {"open": [], "high": [], "low": [], "close": [], "volume": []}
    for code in avail:
        path = os.path.join(DATA_DIR, f"{code}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        for c in cols:
            if c in df.columns:
                cols[c].append(df[c].rename(code))
    out = {}
    for c in cols:
        if cols[c]:
            out[c] = pd.concat(cols[c], axis=1).sort_index()
    return out
