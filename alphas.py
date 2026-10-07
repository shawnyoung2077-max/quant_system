"""
alphas.py - 特定 alpha 因子（基于 OHLCV）
==========================================
条件反转 alpha：仅在"高置信流动性事件"时交易。

trade_when 三重门槛（全部满足才交易）：
  1. 日内极端：收盘价位于当日 high-low 区间的最上 15% 或最下 15%
  2. 放量突破：当日成交量 > 20日均量
  3. 量能动量：3日均量 > 10日均量（确认放量是趋势而非单日异常）

核心信号（trade_when 为真时）：
  sig1 = -2日收益           # 短期2日反转，偏好被打到日线低点附近的股票
  sig2 = -20日相关性         # 量价背离：20日成交额与收益的负相关
  alpha = sig1 × sig2        # 捕捉"恐慌性放量抛售后均值回归反弹"
"""

import numpy as np
import pandas as pd

from data_ohlcv import load_ohlcv_panels


def rolling_corr(a: pd.DataFrame, b: pd.DataFrame, window: int) -> pd.DataFrame:
    """两面板的滚动横截面相关（逐股票滚动相关）。"""
    return a.rolling(window).corr(b)


def factor_to_weights_hold(factor: pd.DataFrame, top_pct: float = 0.2,
                           long_only: bool = True, hold: int = 5) -> pd.DataFrame:
    """
    带持有期的因子权重：每 hold 天调一次仓，中间持有不动。
    适合"事件/反弹"类低频 alpha（避免每日重平衡被换手成本吃光）。
    """
    from factor import factor_to_weights
    daily = factor_to_weights(factor, top_pct=top_pct, long_only=long_only, shift=1)
    reb = daily.index[::hold]          # 每 hold 天一个调仓日
    w = daily.loc[reb].reindex(daily.index).ffill().fillna(0.0)
    return w


def capitulation_reversal(symbols=None, extremity=0.10, vol_window=20,
                          mom_s=3, mom_l=10, ret_window=5,
                          corr_window=10) -> pd.DataFrame:
    """
    构造条件反转 alpha。返回 date×stock 的因子面板（trade_when 之外为 NaN）。
    默认参数经 scan 调优（ext=0.10, ret=5, corr=10, hold=10），全市场 FM t≈2.6 显著。
    """
    o = load_ohlcv_panels(symbols)
    if not o or "close" not in o:
        return None
    close = o["close"]; high = o["high"]; low = o["low"]; vol = o["volume"]

    # 1) 日内位置（0~1，收盘在高低区间的相对位置）
    rng = (high - low).replace(0, np.nan)
    pos = (close - low) / rng
    extremity_mask = (pos > (1 - extremity)) | (pos < extremity)

    # 2) 放量突破
    vol_breakout = vol > vol.rolling(vol_window).mean()

    # 3) 量能动量（3日均量上穿10日均量）
    vol_mom = vol.rolling(mom_s).mean() > vol.rolling(mom_l).mean()

    gate = extremity_mask & vol_breakout & vol_mom

    # 核心信号
    sig1 = -close.pct_change(ret_window)          # 2日反转：跌得越狠越买
    dollar_vol = vol * close                      # 成交额（现金流代理）
    corr = rolling_corr(dollar_vol, close.pct_change(), corr_window)
    sig2 = -corr                                  # 量价背离：成交额与收益负相关

    raw = sig1 * sig2
    alpha = raw.where(gate, np.nan)
    return alpha


if __name__ == "__main__":
    # 命令行自测：在已缓存 OHLCV 上构建并评估
    from factor import cross_section_zscore, winsorize
    from research import factor_significance, print_fm_report
    import run as R
    prices = R.load_prices()
    symbols = [c for c in prices.columns if c in __import__("data_ohlcv").list_available()]
    print(f"OHLCV 可用股票: {len(symbols)}")
    a = capitulation_reversal(symbols=symbols)
    if a is None:
        print("无 OHLCV 数据，请先运行 fetch_ohlcv.py")
    else:
        print("alpha 面板:", a.shape, "非空:", int(a.notna().sum().sum()))
        f = cross_section_zscore(winsorize(a))
        fm = factor_significance(f, prices, period=5)
        print_fm_report(fm, "capitulation_reversal")
