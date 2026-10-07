"""
field_screen.py - 字段筛选器（先筛字段，再挖 alpha）
=====================================================
按方法论：在写任何 alpha 表达式之前，先筛"字段"本身有没有预测力。

对每个字段 × 变换 × 周期，算横截面 rank IC（字段值与未来收益的 Spearman 相关）：
  - 变换：raw / ts_delta（差分）/ ts_zscore（滚动标准化）
  - 周期：1/5/20/60 日
并报告覆盖率（% 非空，看是否有 coverage cliff）。

输出按 IC 排名的字段清单 —— 比一百个瞎提交的 alpha 更值钱，因为它告诉你该把时间花在哪。

用法：python field_screen.py
"""

import numpy as np
import pandas as pd

from factor import _row_spearman, forward_returns
from data_ohlcv import load_ohlcv_panels
from fundamentals import build_factor_panels


def ts_delta(f, window=1):
    return f.diff(window)


def ts_zscore(f, window=60):
    return (f - f.rolling(window).mean()) / f.rolling(window).std().replace(0, np.nan)


def build_fields(prices):
    """构造字段集：价格、量、波动、价值/规模/盈利/投资等原始字段。"""
    o = load_ohlcv_panels(symbols=list(prices.columns))
    close, high, low, vol = o["close"], o["high"], o["low"], o["volume"]
    fields = {
        "close": close,
        "high": high,
        "low": low,
        "volume": vol,
        "dollar_vol": vol * close,
        "intraday_range": (high - low) / close.replace(0, np.nan),
        "pos_in_range": (close - low) / (high - low).replace(0, np.nan),
        "ret_1d": close.pct_change(1),
        "ret_5d": close.pct_change(5),
        "ret_20d": close.pct_change(20),
        "ret_60d": close.pct_change(60),
        "volatility_20d": close.pct_change().rolling(20).std(),
    }
    # 基本面字段（已缓存）
    try:
        fund = build_factor_panels(prices, symbols=list(prices.columns),
                                   factors=["bm", "size", "roe", "investment"],
                                   progress=False, cached_only=True)
        for k, v in fund.items():
            fields[k] = v
    except Exception as e:
        print(f"[warn] 基本面字段加载失败: {type(e).__name__}")
    # 对齐
    aligned = {}
    for k, v in fields.items():
        v = v.reindex(prices.index).reindex(prices.columns, axis=1)
        if v.notna().sum().sum() > 0:
            aligned[k] = v
    return aligned


def screen(prices, fields=None, horizons=(1, 5, 20, 60),
           transforms=("raw", "delta", "zscore")):
    """跑字段筛选，返回 DataFrame（field×transform×horizon 的 IC/ICIR/覆盖率）。"""
    if fields is None:
        fields = build_fields(prices)
    rows = []
    for fname, f in fields.items():
        cov = float(f.notna().sum().sum() / (f.shape[0] * f.shape[1]))
        for tr in transforms:
            if tr == "raw":
                ft = f
            elif tr == "delta":
                ft = ts_delta(f)
            elif tr == "zscore":
                ft = ts_zscore(f)
            else:
                continue
            for h in horizons:
                fwd = forward_returns(prices, h)
                ic = _row_spearman(ft, fwd).dropna()
                if len(ic) < 10:
                    continue
                rows.append({
                    "field": fname, "transform": tr, "horizon": h,
                    "IC": float(ic.mean()),
                    "ICIR": float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0,
                    "coverage": cov,
                })
    return pd.DataFrame(rows)


def print_ranking(df):
    """打印按 |IC| 排名的字段清单。"""
    piv = df.pivot_table(index=["field", "transform"], columns="horizon",
                         values="IC", aggfunc="mean")
    piv = piv[[c for c in (1, 5, 20, 60) if c in piv.columns]]
    piv["best_abs_IC"] = piv.abs().max(axis=1)
    best = df.loc[df.groupby("field")["IC"].apply(lambda s: s.abs().idxmax())]
    print("=" * 78)
    print(" 字段 IC 一览（IC 为横截面 rank 相关，>0.02 才有价值，>0.05 算强）")
    print("=" * 78)
    print(piv.round(4).to_string())
    print("\n各字段最强变换×周期：")
    top = best.sort_values("IC", key=lambda s: s.abs(), ascending=False)
    print(top[["field", "transform", "horizon", "IC", "ICIR", "coverage"]]
          .round(4).to_string(index=False))
    return piv


if __name__ == "__main__":
    import run as R
    prices = R.load_prices()
    print("构建字段集…")
    fields = build_fields(prices)
    print(f"字段数: {len(fields)}")
    df = screen(prices, fields)
    print_ranking(df)
