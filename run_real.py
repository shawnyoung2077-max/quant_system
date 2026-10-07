"""
run_real.py - 用真实 A股数据跑回测
==================================
把真实行情接入框架，演示完整流程。若本地已有 a_share_close.csv 则直接读取，
否则用 realdata 现场抓取。

运行：python run_real.py
"""

import os

from realdata import fetch_a_share_panel, load_a_share_panel
from strategies import (
    cross_sectional_momentum,
    cross_sectional_mean_reversion,
    equal_weight_top_n,
)
from backtest import portfolio_backtest, buy_and_hold
from metrics import print_summary
from analysis import plot_equity_curve, plot_drawdown, plot_rolling_sharpe

DATA_PATH = "data/a_share_close.csv"
OUT_DIR = "output_real"
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs("data", exist_ok=True)


def main():
    # 1) 取真实 A股数据（有缓存则读，无则抓取）
    if os.path.exists(DATA_PATH):
        prices = load_a_share_panel(DATA_PATH)
        print(f"[data] loaded {prices.shape[0]} days x {prices.shape[1]} stocks from {DATA_PATH}")
    else:
        prices = fetch_a_share_panel(start="20220101", save_path=DATA_PATH)
        print(f"[data] fetched {prices.shape[0]} days x {prices.shape[1]} stocks")

    # 对齐：只用全部股票都有的交易日
    prices = prices.dropna(how="any")
    # 等权买入持有基准
    benchmark = buy_and_hold(prices)

    # 2) 横截面动量（多空对冲）
    w_mom = cross_sectional_momentum(prices, lookback=60, top_pct=0.2)
    ret_mom, to_mom = portfolio_backtest(prices, w_mom)
    print("\n===== Cross-Sectional Momentum on A-Share (L/S) =====")
    print_summary(ret_mom, benchmark, turnover=to_mom, title="A-Share CS Momentum")

    # 3) 横截面均值回归（多空对冲）
    w_rev = cross_sectional_mean_reversion(prices, lookback=5, top_pct=0.2)
    ret_rev, to_rev = portfolio_backtest(prices, w_rev)
    print("\n===== Cross-Sectional Mean Reversion on A-Share (L/S) =====")
    print_summary(ret_rev, benchmark, turnover=to_rev, title="A-Share CS Mean Reversion")

    # 4) 用 60 日动量打分，取前 20% 等权做多（纯多头，A股实际可执行）
    score = prices.pct_change(60)
    w_long = equal_weight_top_n(prices, score, n=int(max(1, prices.shape[1] * 0.2)))
    ret_long, to_long = portfolio_backtest(prices, w_long)
    print("\n===== A-Share Top-20% Momentum (long-only) =====")
    print_summary(ret_long, benchmark, turnover=to_long, title="A-Share Top-20% Mom")

    # 5) 可视化
    plot_equity_curve(ret_mom, benchmark, "A-Share CS Momentum (L/S)",
                      f"{OUT_DIR}/equity_mom.png")
    plot_equity_curve(ret_long, benchmark, "A-Share Top-20% Momentum (long)",
                      f"{OUT_DIR}/equity_long.png")
    plot_drawdown(ret_long, "A-Share Long-Only Drawdown", f"{OUT_DIR}/drawdown_long.png")
    plot_rolling_sharpe(ret_long, window=252, title="A-Share Long-Only Rolling Sharpe",
                        save_path=f"{OUT_DIR}/rolling_sharpe_long.png")
    print(f"\n[plots] saved to ./{OUT_DIR}/")


if __name__ == "__main__":
    main()
