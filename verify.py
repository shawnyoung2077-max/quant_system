"""
verify.py - 数据就绪后的一键验收
================================
数据抓全后运行本脚本，验证：
  1. 面板完整性（行/列/日期范围/缺失）
  2. 真实全市场因子挖掘（IC 报告）
  3. 真实全市场回测 + 图表

用法：python verify.py
"""

import os

import pandas as pd

from factor import momentum, evaluate_factor, factor_to_weights
from backtest import portfolio_backtest, buy_and_hold
from metrics import print_summary
from analysis import plot_equity_curve

DATA_PATH = "data/a_share_close.csv"


def main():
    if not os.path.exists(DATA_PATH):
        print("PANEL NOT READY - run: python fetch_market.py")
        return
    prices = pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)
    print("=" * 50)
    print("VERIFY - data panel")
    print("=" * 50)
    print(f"rows={prices.shape[0]}  stocks={prices.shape[1]}")
    print(f"range: {prices.index[0].date()} ~ {prices.index[-1].date()}")
    n_days_10y = (prices.index[-1] - prices.index[0]).days / 365.25
    print(f"span ~{n_days_10y:.1f} years")
    missing = prices.isna().sum().sum()
    print(f"missing cells: {missing}")

    # 清理：丢缺失过多股票、保留覆盖率足够日期、前向填充
    from run import _clean_panel
    aligned = _clean_panel(prices)
    print(f"cleaned panel: {aligned.shape}")

    print("\n" + "=" * 50)
    print("VERIFY - factor mining on real market")
    print("=" * 50)
    f = momentum(aligned, lookback=60)
    evaluate_factor(f, aligned, period=5, name="momentum_60")

    print("\n" + "=" * 50)
    print("VERIFY - backtest on real market")
    print("=" * 50)
    w = factor_to_weights(f, top_pct=0.2, long_only=True)
    ret, turnover = portfolio_backtest(aligned, w)
    benchmark = buy_and_hold(aligned)
    print_summary(ret, benchmark, turnover=turnover, title="Momentum-60 Top20% long")
    plot_equity_curve(ret, benchmark, "Momentum-60 long (real market)",
                      "output_real/verify_equity.png")
    print("[OK] verify done. plots saved.")


if __name__ == "__main__":
    main()
