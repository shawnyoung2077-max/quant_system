"""
run_demo.py - 端到端演示（脱机可跑，不需要任何真实数据）
==========================================================

演示两条完整链路：数据 -> 信号 -> 回测(含成本) -> 指标 -> 可视化

**两组对照（这是重点）**

    组 1  合成数据中「植入」一个持续性横截面 alpha
          -> 期望：动量策略应能捕捉到，产生正收益
          -> 证明了什么：回测管线能检测到真实存在的信号

    组 2  纯随机游走（无任何可利用信息）
          -> 期望：各策略收益应接近 0 或为负（被交易成本吃掉）
          -> 证明了什么：管线不会凭空造出虚假 alpha

只做组 1 是"演示"；加上组 2 才是"验证"。
一个回测框架如果在无信号数据上也能跑出漂亮曲线，那它是不可信的。

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
from metrics import summary as perf_summary
from analysis import (
    plot_equity_curve,
    plot_drawdown,
    plot_rolling_sharpe,
)

OUT_DIR = "output"
os.makedirs(OUT_DIR, exist_ok=True)

# 演示参数（集中在此，便于复现）
CFG = dict(n_assets=30, n_days=1200, seed=42)
# 植入信号强度：经扫描选定，使「有信号」组呈现**现实量级**的结果
#   signal=0.180 + lookback=252  ->  Sharpe 1.42 / 年化 20.5% / 日均换手 14.9%
# 注意：这是为了演示可读性而选的，**不是**在优化策略参数。
# 取太高（如 0.35）会得到 Sharpe 79 这种一眼假的数字，反而失去可信度。
PLANTED_SIGNAL = 0.180
SIGNAL_PERSISTENCE = 0.95
LOOKBACK_MOM = 252
LOOKBACK_REV = 5
TOP_PCT = 0.2
TS_LOOKBACK = 63
TS_HOLD = 10


def run_suite(prices, tag, verbose=True):
    """对给定价格面板跑一组策略，返回 {策略名: dict(ret, turnover, metrics)}"""
    benchmark = buy_and_hold(prices)
    results = {}

    w = cross_sectional_momentum(prices, lookback=LOOKBACK_MOM, top_pct=TOP_PCT)
    ret, to = portfolio_backtest(prices, w)
    results["横截面动量 (L/S)"] = dict(
        ret=ret, turnover=to,
        metrics=perf_summary(ret))

    w2 = cross_sectional_mean_reversion(prices, lookback=LOOKBACK_REV, top_pct=TOP_PCT)
    ret2, to2 = portfolio_backtest(prices, w2)
    results["横截面均值回归 (L/S)"] = dict(
        ret=ret2, turnover=to2,
        metrics=perf_summary(ret2))

    close = prices.iloc[:, 0]
    pos = time_series_momentum(close, lookback=TS_LOOKBACK, hold=TS_HOLD)
    ret3, to3 = single_asset_backtest(close, pos)
    results["时序动量 (%s, 多头)" % prices.columns[0]] = dict(
        ret=ret3, turnover=to3,
        metrics=perf_summary(ret3))

    if verbose:
        print("\n" + "=" * 76)
        print("  %s" % tag)
        print("=" * 76)
        print("  %-26s %11s %11s %10s %10s" %
              ("策略", "累计收益", "年化收益", "夏普", "日均换手"))
        print("  " + "-" * 72)
        for name, r in results.items():
            m = r["metrics"]
            try:
                tv = float(r["turnover"].mean()) * 100
            except Exception:
                tv = 0.0
            print("  %-26s %10.2f%% %10.2f%% %10.2f %9.2f%%" %
                  (name,
                   m.get("total_return", 0) * 100,
                   m.get("annualized_return", 0) * 100,
                   m.get("sharpe", 0),
                   tv))
    results["_benchmark"] = benchmark
    return results


def main():
    print("=" * 76)
    print("  quant_system 端到端演示")
    print("  数据无关回测框架 · 脱机可跑（无需真实行情）")
    print("=" * 76)

    # ---------------- 组 1：植入信号 ----------------
    print("\n>>> 组 1：合成数据中植入持续性横截面 alpha（signal=%.3f）" % PLANTED_SIGNAL)
    print("    预期：动量类策略能捕捉到该信号，但 Sharpe 应在现实量级（0.8~1.5）")
    px_sig = make_sample_prices(signal=PLANTED_SIGNAL,
                                signal_persistence=SIGNAL_PERSISTENCE, **CFG)
    print("    [data] %d 天 x %d 资产（已植入信号）" % px_sig.shape)
    res_sig = run_suite(px_sig, "组 1 · 有信号")

    # ---------------- 组 2：纯随机 ----------------
    print("\n>>> 组 2：纯随机游走（signal=0.0，无任何可利用信息）")
    print("    预期：各策略应接近 0 或为负（被成本吃掉）—— 管线不应造出虚假 alpha")
    px_rnd = make_sample_prices(signal=0.0, **CFG)
    print("    [data] %d 天 x %d 资产（无信号）" % px_rnd.shape)
    res_rnd = run_suite(px_rnd, "组 2 · 无信号")

    # ---------------- 对照汇总 ----------------
    print("\n" + "=" * 76)
    print("  对照汇总：夏普比率（有信号 vs 无信号）")
    print("=" * 76)
    print("  %-26s %14s %14s" % ("策略", "有信号", "无信号"))
    print("  " + "-" * 72)
    for name in res_sig:
        if name.startswith("_"):
            continue
        a = res_sig[name]["metrics"].get("sharpe", 0)
        b = res_rnd.get(name, {}).get("metrics", {}).get("sharpe", 0)
        flag = "   <-- 检出信号" if (a - b) > 0.5 else ""
        print("  %-26s %14.2f %14.2f%s" % (name, a, b, flag))

    # ---------------- 可视化 ----------------
    bench = res_sig["_benchmark"]
    plot_equity_curve(res_sig["横截面动量 (L/S)"]["ret"], bench,
                      "Cross-Sectional Momentum (planted signal)",
                      f"{OUT_DIR}/equity_mom.png")
    plot_equity_curve(res_sig["横截面均值回归 (L/S)"]["ret"], bench,
                      "Cross-Sectional Mean Reversion (planted signal)",
                      f"{OUT_DIR}/equity_rev.png")
    plot_drawdown(res_sig["横截面动量 (L/S)"]["ret"], "CS Momentum Drawdown",
                  f"{OUT_DIR}/drawdown_mom.png")
    plot_rolling_sharpe(res_sig["横截面动量 (L/S)"]["ret"], window=252,
                        title="CS Momentum Rolling Sharpe",
                        save_path=f"{OUT_DIR}/rolling_sharpe_mom.png")
    print("\n[plots] saved to ./%s/" % OUT_DIR)
    print("\n演示结束。接真实数据请用 run_real.py 或 quant_system 的数据层。")


if __name__ == "__main__":
    main()
