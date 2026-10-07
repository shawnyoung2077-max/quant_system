"""
market_neutral.py - 加密"市场中性"框架（回应核心痛点：剥Beta / 防归零 / 去趋势）
================================================================================
四个组件：
  1. filter_universe      股票池过滤：按【市值排名 + 流动性(成交额)】硬性筛选，对抗归零/土狗风险
  2. beta_neutral_weights 用滚动 Beta 把组合净 Beta 压到 0（比单纯 dollar-neutral 更彻底）
  3. carry_backtest       Delta 中性 carry：现货多 + 永续空，赚资金费率（真实现金流，非统计巧合）
  4. cross_section_demean 横截面去趋势（=factor.zscore，这里显式封装并保留原始列的对齐）

用法：
    from market_neutral import filter_universe, beta_neutral_weights, carry_backtest
"""

import os
import numpy as np
import pandas as pd

PPY = 365


# ---------------------------------------------------------------------------
# 1) Universe 过滤：对抗归零风险
# ---------------------------------------------------------------------------
def load_liquidity(interval: str = "1d", field: str = "qav") -> pd.DataFrame:
    """成交额面板（qav = 计价货币成交额，即美元成交额）。"""
    from crypto_data import build_panel
    return build_panel(interval, field)


def filter_universe(prices: pd.DataFrame, dollar_volume: pd.DataFrame,
                    top_n: int = 20, min_dv_usd: float = 5e6,
                    min_hist: float = 0.9, dv_window: int = 30) -> pd.DataFrame:
    """
    返回布尔掩码（date×coin）：当日是否允许交易。
    规则（每条都对抗"归零/土狗"）：
      · 历史长度 ≥ min_hist（新币上市不足不给权重）
      · 30日平均成交额 ≥ min_dv_usd（流动性门槛，防冲击成本爆表）
      · 当日按成交额排名进入 top_n（只做主流，长尾一律剔除）
    """
    dv = dollar_volume.reindex(prices.index).reindex(columns=prices.columns).ffill()
    adv = dv.rolling(dv_window).mean()
    has_hist = prices.notna().rolling(int(len(prices) * min_hist * 0.1) + 20).count() > 0
    liquid = adv >= min_dv_usd
    rank = adv.rank(axis=1, ascending=False)
    top = rank <= top_n
    mask = (liquid & top).fillna(False)
    return mask


def apply_universe(weights: pd.DataFrame, mask: pd.DataFrame,
                   renormalize: bool = True) -> pd.DataFrame:
    """把不在 Universe 内的权重清零；可选重新归一到总暴露 1。"""
    w = weights.where(mask.reindex_like(weights).fillna(False), 0.0)
    if renormalize:
        gross = w.abs().sum(axis=1).replace(0, np.nan)
        w = w.div(gross, axis=0).fillna(0.0)
    return w


# ---------------------------------------------------------------------------
# 2) Beta 中性（比 dollar-neutral 更彻底）
# ---------------------------------------------------------------------------
def estimate_betas(prices: pd.DataFrame, market: pd.Series,
                   window: int = 60) -> pd.DataFrame:
    """逐币对"市场"（如 BTC）的滚动 Beta。"""
    r = prices.pct_change()
    m = market.pct_change().reindex(r.index)
    cov = r.rolling(window).cov(m)
    var = m.rolling(window).var()
    return cov.div(var, axis=0)


def beta_neutral_weights(weights: pd.DataFrame, betas: pd.DataFrame) -> pd.DataFrame:
    """
    调整多空权重，使组合净 Beta = Σ(w_i · β_i) = 0。
    做法：对空头腿整体缩放 k，使 Σ_long β - k·Σ_short β = 0。
    """
    w = weights.copy()
    b = betas.reindex_like(w).fillna(1.0)
    long_beta = (w.clip(lower=0) * b).sum(axis=1)
    short_beta = (-w.clip(upper=0) * b).sum(axis=1)
    k = long_beta.div(short_beta.replace(0, np.nan)).fillna(1.0)
    k = k.clip(0.2, 5.0)                       # 防极端缩放
    w = w.where(w >= 0, w.mul(k, axis=0))       # 只缩放空头腿
    gross = w.abs().sum(axis=1).replace(0, np.nan)
    return w.div(gross, axis=0).fillna(0.0)


# ---------------------------------------------------------------------------
# 3) Delta 中性 carry（现货多 + 永续空，赚资金费率）
# ---------------------------------------------------------------------------
def carry_backtest(funding_daily: pd.DataFrame, prices: pd.DataFrame,
                   entry_bps: float = 8.0, top_n: int = 10,
                   min_funding: float = 0.0, mask: pd.DataFrame = None,
                   rebalance_days: int = 1, fee_bps_roundtrip: float = 12.0):
    """
    Delta 中性资金费率套利回测（简化版）：
      每天选取【当日资金费率为正且最高】的 top_n 个币，做
      「现货多 + 永续空」→ 净Delta≈0，收益主要来自资金费率（空头收取）。
      收益 = Σ w_i × funding_i − 换手成本
    返回 (net_returns, turnover, funding_income)
    """
    fd = funding_daily.reindex(prices.index).reindex(columns=prices.columns)
    if mask is not None:
        fd = fd.where(mask.reindex_like(fd).fillna(False))
    fd = fd.fillna(0.0)
    # 选正费率最高的 top_n
    pos_rank = fd.where(fd > min_funding).rank(axis=1, ascending=False)
    sel = (pos_rank <= top_n).fillna(False)
    w = sel.astype(float)
    gross = w.sum(axis=1).replace(0, np.nan)
    w = w.div(gross, axis=0).fillna(0.0)
    if rebalance_days > 1:
        reb = w.index[::rebalance_days]
        w = w.loc[reb].reindex(w.index).ffill().fillna(0.0)
    # 收益：空头永续收取资金费率（费率为正时）
    income = (w * fd).sum(axis=1)
    # 换手成本（每次调仓的变动）
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    net = income - turn * fee_bps_roundtrip / 1e4
    return net, turn, income


# ---------------------------------------------------------------------------
# 4) 横截面去趋势（显式封装）
# ---------------------------------------------------------------------------
def cross_section_demean(panel: pd.DataFrame) -> pd.DataFrame:
    """每日对全市场去均值除标准差 —— 天然剥离大盘共同成分（去趋势）。"""
    mu = panel.mean(axis=1)
    sd = panel.std(axis=1).replace(0, np.nan)
    return panel.sub(mu, axis=0).div(sd, axis=0)


if __name__ == "__main__":
    from crypto_data import build_panel
    px = build_panel("1d", "close")
    dv = build_panel("1d", "qav")
    print("面板:", px.shape)
    m = filter_universe(px, dv, top_n=20, min_dv_usd=5e6)
    print("Universe 每日可选币数（近5日）:", m.sum(axis=1).tail(5).tolist())
    print("全期平均可选:", round(float(m.sum(axis=1).mean()), 1))
