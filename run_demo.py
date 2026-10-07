"""
run_demo.py - 端到端演示
========================
用合成数据演示完整流程：
  数据 -> 策略信号 -> 回测(含成本) -> 指标 -> 可视化

运行：python run_demo.py
"""

import os

from data import make_sample_prices
from strategies import (
    cross_sectional_momentum,
    cross_sectional_mean_reversion,
    time_series_momentum,
)
from backtest import portfolio_backtest, single_asset_backtest, buy_and_hold
from metrics import print_summary
from analysis import (
    plot_equity_curve,
    plot_drawdown,
    plot_rolling_sharpe,
)

OUT_DIR = "output"
os.makedirs(OUT_DIR, exist_ok=True)


def main():
    # 1) 数据（无真实数据时用合成数据演示）
    prices = make_sample_prices(n_assets=30, n_days=1200, seed=42)
    print(f"[data] {prices.shape[0]} days x {prices.shape[1]} assets")

    benchmark = buy_and_hold(prices)  # 等权买入持有

    # 2) 横截面动量（多空对冲）回测
    w_mom = cross_sectional_momentum(prices, lookback=60, top_pct=0.2)
    ret_mom, to_mom = portfolio_backtest(prices, w_mom)
    print("\n===== Cross-Sectional Momentum (L/S) =====")
    print_summary(ret_mom, benchmark, turnover=to_mom)

    # 3) 横截面均值回归（多空对冲）
    w_rev = cross_sectional_mean_reversion(prices, lookback=5, top_pct=0.2)
    ret_rev, to_rev = portfolio_backtest(prices, w_rev)
    print("\n===== Cross-Sectional Mean Reversion (L/S) =====")
    print_summary(ret_rev, benchmark, turnover=to_rev)

    # 4) 单资产时序动量（以第一只股票为例）
    close = prices["A01"]
    pos_ts = time_series_momentum(close, lookback=63, hold=10)
    ret_ts, to_ts = single_asset_backtest(close, pos_ts)
    print("\n===== Time-Series Momentum (A01, long-only) =====")
    print_summary(ret_ts, benchmark=benchmark, turnover=to_ts)

    # 5) 可视化
    plot_equity_curve(ret_mom, benchmark, "Cross-Sectional Momentum (L/S)",
                      f"{OUT_DIR}/equity_mom.png")
    plot_equity_curve(ret_rev, benchmark, "Cross-Sectional Mean Reversion (L/S)",
                      f"{OUT_DIR}/equity_rev.png")
    plot_drawdown(ret_mom, "CS Momentum Drawdown", f"{OUT_DIR}/drawdown_mom.png")
    plot_rolling_sharpe(ret_mom, window=252, title="CS Momentum Rolling Sharpe",
                        save_path=f"{OUT_DIR}/rolling_sharpe_mom.png")
    print(f"\n[plots] saved to ./{OUT_DIR}/")


if __name__ == "__main__":
    main()
