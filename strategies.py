"""
strategies.py - 经典策略（信号生成）
==================================
每个策略输出"信号"，交给回测引擎：
  - 单资产策略：返回目标仓位 Series（-1~1）
  - 横截面策略：返回目标权重 DataFrame（可多空）

所有信号都已 shift，避免"未来函数/前视偏差"（lookahead bias）——
这是面试必考点，务必在策略里正确滞后信号。
"""

import numpy as np
import pandas as pd


def _safe_normalize(sel: pd.DataFrame) -> pd.DataFrame:
    """按行归一化权重，处理除零。"""
    s = sel.sum(axis=1).replace(0, np.nan)
    return sel.div(s, axis=0).fillna(0.0)


# ---------- 单资产策略 ----------

def ma_crossover(close: pd.Series, fast: int = 20, slow: int = 50,
                 shift: int = 1) -> pd.Series:
    """均线交叉：快线上穿慢线做多(1)，否则空仓(0)。已滞后避免前视。"""
    fast_ma = close.rolling(fast).mean()
    slow_ma = close.rolling(slow).mean()
    pos = (fast_ma > slow_ma).astype(float)
    pos = pos.shift(shift)
    return pos.fillna(0.0)


def time_series_momentum(close: pd.Series, lookback: int = 252, hold: int = 21,
                         long_only: bool = True, shift: int = 1) -> pd.Series:
    """时序动量：过去 lookback 日收益>0 做多，<0 做空（或 0 若 long_only）。"""
    mom = close.pct_change(lookback)
    if long_only:
        pos = (mom > 0).astype(float)
    else:
        pos = np.sign(mom).astype(float)
    pos = pos.shift(shift + hold)  # 信号滞后 hold 天（持仓期），避免前视
    return pos.fillna(0.0)


def mean_reversion_single(close: pd.Series, lookback: int = 20,
                          z_thresh: float = 1.0, shift: int = 1) -> pd.Series:
    """单资产均值回归：价格低于滚动均值超过 z_thresh 个标准差则做多，反之做空。"""
    ma = close.rolling(lookback).mean()
    std = close.rolling(lookback).std().replace(0, np.nan)
    z = (close - ma) / std
    pos = np.where(z < -z_thresh, 1.0, np.where(z > z_thresh, -1.0, 0.0))
    pos = pd.Series(pos, index=close.index).shift(shift)
    return pos.fillna(0.0)


# ---------- 横截面策略（多资产，返回目标权重）----------

def cross_sectional_momentum(prices: pd.DataFrame, lookback: int = 60,
                             top_pct: float = 0.2, shift: int = 1) -> pd.DataFrame:
    """横截面动量：过去 lookback 日收益排名，做多前 top_pct，做空后 top_pct（多空对冲）。"""
    mom = prices.pct_change(lookback)
    ranks = mom.rank(axis=1, pct=True)
    top = ranks > (1.0 - top_pct)
    bot = ranks < top_pct
    weights = _safe_normalize(top.astype(float)) - _safe_normalize(bot.astype(float))
    weights = weights.shift(shift)
    return weights.fillna(0.0)


def cross_sectional_mean_reversion(prices: pd.DataFrame, lookback: int = 5,
                                   top_pct: float = 0.2, shift: int = 1) -> pd.DataFrame:
    """横截面均值回归：做多最近跌得多的（后 top_pct），做空涨得多的（前 top_pct）。"""
    ret = prices.pct_change(lookback)
    ranks = ret.rank(axis=1, pct=True)
    bot = ranks < top_pct        # 低收益（超跌）
    top = ranks > (1.0 - top_pct)  # 高收益（超涨）
    weights = _safe_normalize(bot.astype(float)) - _safe_normalize(top.astype(float))
    weights = weights.shift(shift)
    return weights.fillna(0.0)


def equal_weight_top_n(prices: pd.DataFrame, score: pd.DataFrame,
                       n: int = 10, shift: int = 1) -> pd.DataFrame:
    """通用选股：按 score 排名取前 n 只等权做多（可用于因子选股）。"""
    ranks = score.rank(axis=1, method="first")
    sel = ranks <= n
    weights = _safe_normalize(sel.astype(float))
    weights = weights.shift(shift)
    return weights.fillna(0.0)
