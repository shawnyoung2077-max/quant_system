"""
scan_capitulation.py - 扫描条件反转 alpha 参数找最优组合
========================================================
遍历 ret_window / corr_window / extremity / hold，按年化+夏普排名。
用法：python scan_capitulation.py
"""

import itertools

import numpy as np
import pandas as pd

import run as R
from data_ohlcv import list_available, load_ohlcv_panels
from alphas import capitulation_reversal, factor_to_weights_hold
from factor import factor_to_weights
from research import cross_section_zscore, winsorize
from backtest import portfolio_backtest
from metrics import sharpe_ratio

prices = R.load_prices()
avail = [c for c in prices.columns if c in list_available()]
print(f"OHLCV 股票: {len(avail)}")
o = load_ohlcv_panels(symbols=avail)

GRID = {
    "ret_window": [2, 3, 5],
    "corr_window": [10, 20, 30],
    "extremity": [0.10, 0.15, 0.20],
    "hold": [5, 10, 15],
}


def build_factor(o, ret_window, corr_window, extremity, vol_window=20, mom_s=3, mom_l=10):
    close = o["close"]; high = o["high"]; low = o["low"]; vol = o["volume"]
    rng = (high - low).replace(0, np.nan)
    pos = (close - low) / rng
    gate = ((pos > (1 - extremity)) | (pos < extremity)) \
           & (vol > vol.rolling(vol_window).mean()) \
           & (vol.rolling(mom_s).mean() > vol.rolling(mom_l).mean())
    sig1 = -close.pct_change(ret_window)
    dollar_vol = vol * close
    corr = dollar_vol.rolling(corr_window).corr(close.pct_change())
    raw = sig1 * (-corr)
    return raw.where(gate, np.nan)


results = []
combos = list(itertools.product(GRID["ret_window"], GRID["corr_window"],
                                GRID["extremity"], GRID["hold"]))
for rw, cw, ex, hd in combos:
    try:
        a = build_factor(o, rw, cw, ex)
        f = cross_section_zscore(winsorize(a)).reindex(prices.index)
        w = factor_to_weights_hold(f, top_pct=0.2, long_only=True, hold=hd)
        ret, to = portfolio_backtest(prices, w)
        ann = (1 + ret).prod() ** (252 / len(ret)) - 1
        sh = sharpe_ratio(ret)
        results.append({"ret": rw, "corr": cw, "ext": ex, "hold": hd,
                        "annual": ann, "sharpe": sh, "turnover": float(to.mean() * 252)})
    except Exception as e:
        print("err", rw, cw, ex, hd, type(e).__name__)

df = pd.DataFrame(results).sort_values("sharpe", ascending=False)
print("=" * 72)
print(" 条件反转 alpha 参数扫描（按夏普排名，纯多头）")
print("=" * 72)
print(df.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
best = df.iloc[0]
print("\n最佳: ret_window=%d corr_window=%d extremity=%.2f hold=%d" %
      (best.ret, best.corr, best.ext, best.hold))
print("年化=%.2f%% 夏普=%.2f 换手=%.1f" % (best.annual * 100, best.sharpe, best.turnover))
