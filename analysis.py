"""
analysis.py - 可视化模块
=======================
生成策略净值曲线、回撤曲线、滚动夏普，并保存为 PNG（便于报告/简历）。
使用 matplotlib 非交互后端（Agg），无需图形界面也能出图。
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from metrics import cumulative_returns, max_drawdown


def plot_equity_curve(net_returns: pd.Series, benchmark: pd.Series = None,
                      title: str = "Equity Curve", save_path: str = "equity.png"):
    """绘制策略净值 vs 基准（可选）。"""
    fig, ax = plt.subplots(figsize=(11, 5))
    strategy = cumulative_returns(net_returns)
    ax.plot(strategy.index, strategy.values, label="Strategy", color="#1f77b4", lw=1.6)
    if benchmark is not None:
        bench = cumulative_returns(benchmark)
        ax.plot(bench.index, bench.values, label="Benchmark (buy&hold)", color="#d62728",
                lw=1.2, alpha=0.8)
    ax.set_title(title)
    ax.set_ylabel("Cumulative Return")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)
    return save_path


def plot_drawdown(daily_returns: pd.Series, title: str = "Drawdown",
                  save_path: str = "drawdown.png"):
    """绘制回撤曲线。"""
    eq = cumulative_returns(daily_returns)
    dd = eq / eq.cummax() - 1.0
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.fill_between(dd.index, dd.values, 0, color="#9467bd", alpha=0.4)
    ax.set_title(title + f"  (Max DD={max_drawdown(eq):.2%})")
    ax.set_ylabel("Drawdown")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)
    return save_path


def plot_rolling_sharpe(daily_returns: pd.Series, window: int = 252,
                        title: str = "Rolling Sharpe", save_path: str = "rolling_sharpe.png"):
    """绘制滚动夏普。"""
    roll = daily_returns.rolling(window).mean() / daily_returns.rolling(window).std().replace(0, np.nan)
    roll = roll * np.sqrt(252)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(roll.index, roll.values, color="#2ca02c", lw=1.4)
    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.set_title(title)
    ax.set_ylabel(f"Sharpe ({window}d window)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)
    return save_path
