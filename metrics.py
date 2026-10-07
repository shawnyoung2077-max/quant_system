"""
metrics.py - 策略性能指标计算模块
============================
给回测产生的收益序列计算标准化绩效指标（夏普、回撤、年化收益等），
用于评估策略质量，也是面试里最常被问到的部分。

约定：
- 输入是"每日收益率"序列（pandas Series，index 为日期）。
- 年化按 252 个交易日计。
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def to_daily_returns(prices: pd.Series) -> pd.Series:
    """由价格序列计算日收益率（简单收益率）。"""
    return prices.pct_change().dropna()


def annualized_return(daily_returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """年化收益率（对数法，防溢出）：exp(mean(log1p)) * periods - 1。"""
    if len(daily_returns) == 0:
        return 0.0
    r = daily_returns.clip(lower=-0.9999)
    logret = np.log1p(r)
    if not np.isfinite(logret).all():
        return 0.0
    return float(np.expm1(np.nanmean(logret) * periods))


def annualized_volatility(daily_returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """年化波动率。"""
    return daily_returns.std(ddof=1) * np.sqrt(periods)


def sharpe_ratio(daily_returns: pd.Series, risk_free: float = 0.0,
                 periods: int = TRADING_DAYS) -> float:
    """夏普比率 = (年化收益 - 无风险) / 年化波动。risk_free 为年化无风险利率。"""
    vol = annualized_volatility(daily_returns, periods)
    if vol == 0:
        return 0.0
    return (annualized_return(daily_returns, periods) - risk_free) / vol


def max_drawdown(prices: pd.Series) -> float:
    """最大回撤（从峰值到谷值的最大跌幅，正数表示跌了百分之多少）。"""
    if len(prices) == 0:
        return 0.0
    cummax = prices.cummax()
    drawdown = prices / cummax - 1.0
    return -drawdown.min()


def calmar_ratio(daily_returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """卡玛比率 = 年化收益 / 最大回撤。"""
    mdd = max_drawdown((1.0 + daily_returns).cumprod())
    if mdd == 0:
        return 0.0
    return annualized_return(daily_returns, periods) / mdd


def win_rate(daily_returns: pd.Series) -> float:
    """盈利率（正收益天数占比）。"""
    if len(daily_returns) == 0:
        return 0.0
    return float((daily_returns > 0).mean())


def profit_factor(daily_returns: pd.Series) -> float:
    """盈亏比 = 总盈利 / 总亏损绝对值。"""
    wins = daily_returns[daily_returns > 0].sum()
    losses = -daily_returns[daily_returns < 0].sum()
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def cumulative_returns(daily_returns: pd.Series) -> pd.Series:
    """累计净值曲线（从 1 开始）。"""
    return (1.0 + daily_returns).cumprod()


def summary(daily_returns: pd.Series) -> dict:
    """汇总所有核心指标。"""
    prices = cumulative_returns(daily_returns)
    return {
        "total_return": float((1.0 + daily_returns).prod() - 1.0),
        "annualized_return": annualized_return(daily_returns),
        "annualized_vol": annualized_volatility(daily_returns),
        "sharpe": sharpe_ratio(daily_returns),
        "max_drawdown": max_drawdown(prices),
        "calmar": calmar_ratio(daily_returns),
        "win_rate": win_rate(daily_returns),
        "profit_factor": profit_factor(daily_returns),
        "n_days": len(daily_returns),
    }


def print_summary(daily_returns: pd.Series, benchmark: pd.Series = None,
                  turnover: pd.Series = None, title: str = "策略绩效摘要") -> None:
    """打印格式化指标摘要。可选传入 benchmark（基准日收益）与 turnover（日换手率）。"""
    s = summary(daily_returns)
    print("=" * 46)
    print(f"          {title}")
    print("=" * 46)
    print(f"  总收益率      : {s['total_return']:>10.2%}")
    print(f"  年化收益率    : {s['annualized_return']:>10.2%}")
    print(f"  年化波动率    : {s['annualized_vol']:>10.2%}")
    print(f"  夏普比率      : {s['sharpe']:>10.2f}")
    print(f"  最大回撤      : {s['max_drawdown']:>10.2%}")
    print(f"  卡玛比率      : {s['calmar']:>10.2f}")
    print(f"  胜率(按天)    : {s['win_rate']:>10.2%}")
    print(f"  盈亏比        : {s['profit_factor']:>10.2f}")
    print(f"  交易日数      : {s['n_days']:>10d}")
    if benchmark is not None:
        bs = summary(benchmark)
        print(f"  基准年化      : {bs['annualized_return']:>10.2%}")
        print(f"  基准夏普      : {bs['sharpe']:>10.2f}")
    if turnover is not None:
        print(f"  日均换手率    : {turnover.mean():>10.2%}")
        print(f"  年化换手率    : {turnover.mean() * TRADING_DAYS:>10.2f} 倍")
    print("=" * 46)
