"""
backtest.py - 向量化回测引擎
============================
对给定"目标权重"（多资产）或"目标仓位"（单资产）做回测，
考虑交易成本（佣金 + 滑点）、换手率、权重漂移，输出每日净收益。

两种模式：
  1. portfolio_backtest : 多资产组合（因子选股/横截面策略）
  2. single_asset_backtest: 单资产（时序动量/均线交叉等）

成本说明：cost_rate = (佣金 + 滑点) / 10000（单位 bps）。
"""

import numpy as np
import pandas as pd


def portfolio_backtest(prices: pd.DataFrame, target_weights: pd.DataFrame,
                       commission_bps: float = 10.0, slippage_bps: float = 5.0):
    """
    多资产组合向量化回测。

    Parameters
    ----------
    prices : DataFrame, 行=日期, 列=资产, 值为价格（收盘价或调整后收盘价）
    target_weights : DataFrame, 行=日期, 列=资产, 值为目标权重（允许 0~1，可负=做空）
    commission_bps : 佣金（基点）
    slippage_bps  : 滑点（基点）

    Returns
    -------
    (net_returns, turnover) : (Series 每日净收益, Series 每日换手率)
    """
    asset_returns = prices.pct_change().fillna(0.0)
    # 目标权重按日前向填充，缺失补 0
    w = target_weights.reindex(prices.index).ffill().fillna(0.0)

    # 组合毛收益：用上一期权重乘以本期资产收益
    gross = (w.shift(1).fillna(0.0) * asset_returns).sum(axis=1)

    # 权重漂移：w_{t-1} * (1+r_t) / (1+g_t)
    drift = w.shift(1).fillna(0.0) * (1.0 + asset_returns)
    drift = drift.div((1.0 + gross).replace(0, np.nan), axis=0).fillna(0.0)

    # 换手率：|目标权重 - 漂移后权重| 之和
    turnover = (w - drift).abs().sum(axis=1)

    # 交易成本
    cost_rate = (commission_bps + slippage_bps) / 10000.0
    cost = turnover * cost_rate

    net_returns = gross - cost
    return net_returns, turnover


def single_asset_backtest(close: pd.Series, position: pd.Series,
                          commission_bps: float = 10.0, slippage_bps: float = 5.0):
    """
    单资产回测。

    Parameters
    ----------
    close : Series, 价格
    position : Series, 目标仓位（-1~1，占资金比例；1=满仓做多，-1=满仓做空）
    commission_bps / slippage_bps : 交易成本（基点）

    Returns
    -------
    (net_returns, turnover)
    """
    returns = close.pct_change().fillna(0.0)
    prev_pos = position.shift(1).fillna(0.0)
    gross = prev_pos * returns
    turnover = (position - prev_pos).abs()
    cost_rate = (commission_bps + slippage_bps) / 10000.0
    net_returns = gross - turnover * cost_rate
    return net_returns, turnover


def buy_and_hold(prices: pd.DataFrame) -> pd.Series:
    """等权买入持有的基准收益（用于对比）。"""
    returns = prices.pct_change().fillna(0.0)
    return returns.mean(axis=1)
