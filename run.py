"""
run.py - 统一回测 / alpha 挖掘运行器
====================================
让用户用命令行自主回测、自主挖掘 alpha，不需要改代码。

用法：
    python run.py list                                   # 看数据 + 可用因子
    python run.py mine  --factor momentum --lookback 60 --period 5
                                                         # 挖掘/评价因子（IC 报告）
    python run.py backtest --factor momentum --lookback 60 --top 0.2 --long-only
                                                         # 用因子做策略并回测

可用 --factor：
    momentum / reversal / price_ma / volatility / volume

示例：
    python run.py backtest --factor momentum --lookback 60 --top 0.2 --long-only
    python run.py mine --factor reversal --lookback 5 --period 5
"""

import argparse
import os

import numpy as np
import pandas as pd

from data import make_sample_prices
from factor import (
    momentum, short_term_reversal, price_vs_ma, volatility_factor,
    volume_factor, evaluate_factor, factor_to_weights,
)
from backtest import portfolio_backtest, buy_and_hold
from metrics import print_summary
from analysis import plot_equity_curve, plot_drawdown
from alpha_score import score_factor, print_score
from alpha_library import (meets_threshold, save, remove, clear,
                           list_alphas, print_library)
from factor import factor_ic, forward_returns
from research import factor_significance, print_fm_report, alpha_report

DATA_PATH = "data/a_share_close.csv"
OUT_DIR = "output"
os.makedirs(OUT_DIR, exist_ok=True)

FACTORS = {
    "momentum": momentum,
    "reversal": short_term_reversal,
    "price_ma": price_vs_ma,
    "volatility": volatility_factor,
}


def load_prices():
    """加载真实A股面板；若未就绪则退回合成数据。"""
    if os.path.exists(DATA_PATH):
        prices = pd.read_csv(DATA_PATH, index_col=0, parse_dates=True)
        prices = _clean_panel(prices)
        if prices.shape[1] >= 5:
            print(f"[data] {prices.shape[0]} days x {prices.shape[1]} stocks (real A-share)")
            return prices
    print("[data] real panel not ready, using synthetic data")
    return make_sample_prices(n_assets=40, n_days=1500, seed=7)


def _clean_panel(prices, col_keep=0.05, row_keep=0.02):
    """清理面板（宽松版）：只剔除几乎全空的股票，保留全部~800只；几乎不全的日期才剔除。"""
    prices = prices.dropna(axis=1, thresh=int(prices.shape[0] * col_keep))
    if prices.shape[1] >= 5:
        prices = prices.dropna(axis=0, thresh=int(prices.shape[1] * row_keep))
    prices = prices.ffill()
    return prices


def compute_factor(prices, factor_name, lookback, **kw):
    fn = FACTORS[factor_name]
    close = prices
    if factor_name == "volume":
        # 用价格的 20 日滚动成交额代理（真实量能需 amount 面板）
        amount = close * close.pct_change().abs().rolling(5).mean().fillna(0) * 1000
        return fn(amount, close, lookback)
    return fn(close, lookback)


def cmd_list(prices, args=None):
    print(f"\n数据: {prices.shape[0]} 天 x {prices.shape[1]} 只股票")
    print(f"时间范围: {prices.index[0].date()} ~ {prices.index[-1].date()}")
    print("\n可用因子:")
    for k, v in FACTORS.items():
        print(f"  - {k:<12} {v.__doc__.strip().splitlines()[0]}")
    print("\n常用命令:")
    print("  python run.py mine --factor momentum --lookback 60 --period 5")
    print("  python run.py backtest --factor reversal --lookback 5 --top 0.2 --long-only")


def cmd_mine(prices, args):
    f = compute_factor(prices, args.factor, args.lookback)
    evaluate_factor(f, prices, period=args.period, name=args.factor)


def cmd_backtest(prices, args):
    f = compute_factor(prices, args.factor, args.lookback)
    w = factor_to_weights(f, top_pct=args.top, long_only=not args.long_short)
    benchmark = buy_and_hold(prices)
    ret, turnover = portfolio_backtest(prices, w)
    print(f"\n===== Factor strategy: {args.factor} (lb={args.lookback}) =====")
    print_summary(ret, benchmark, turnover=turnover, title=f"{args.factor} strategy")
    tag = "long" if args.long_only else "ls"
    plot_equity_curve(ret, benchmark, f"{args.factor} (lb={args.lookback})",
                      f"{OUT_DIR}/equity_{args.factor}_{tag}.png")
    plot_drawdown(ret, f"{args.factor} drawdown",
                  f"{OUT_DIR}/drawdown_{args.factor}_{tag}.png")
    print(f"[plots] saved to ./{OUT_DIR}/")


def cmd_scan(prices, args):
    """扫描因子参数网格，排出 IC/ICIR 最优组合。"""
    lbs = list(range(args.lb_min, args.lb_max + 1, args.lb_step))
    rows = []
    for lb in lbs:
        f = compute_factor(prices, args.factor, lb)
        rep = evaluate_factor(f, prices, period=args.period,
                              name=f"{args.factor}_{lb}", quiet=True)
        rows.append({
            "lookback": lb, "IC": rep["ic_mean"], "ICIR": rep["icir"],
            "IC胜率": rep["ic_positive_ratio"], "多空价差": rep["long_short_spread"],
        })
    df = pd.DataFrame(rows).sort_values("ICIR", ascending=False)
    print(f"\n===== Scan: {args.factor}  (predict next {args.period}d) =====")
    print(df.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    best = df.iloc[0]
    print(f"\n最佳 ICIR 组合: lookback={int(best['lookback'])}  IC={best['IC']:.4f}  ICIR={best['ICIR']:.4f}")


def cmd_score(prices, args):
    """WorldQuant 风格给因子评分（等级 A-F）。"""
    f = compute_factor(prices, args.factor, args.lookback)
    r = score_factor(prices, f, top_pct=args.top, long_only=not args.long_short)
    print_score(r, name=f"{args.factor} (lb={args.lookback})")


def cmd_libsave(prices, args):
    """评分因子，只有达到 min-grade 门槛才存入 Alpha 库，否则丢弃。"""
    f = compute_factor(prices, args.factor, args.lookback)
    sc = score_factor(prices, f, top_pct=args.top, long_only=not args.long_short)
    # 同时算 IC
    ic = factor_ic(f, forward_returns(prices, args.period))
    ic = ic.replace([np.inf, -np.inf], np.nan).dropna()
    ic_mean = float(ic.mean())
    icir = float(ic.mean() / ic.std()) if len(ic) > 1 and ic.std() > 0 else 0.0
    print_score(sc, name=f"{args.factor} (lb={args.lookback})")
    print(f"\nIC={ic_mean:.4f}  ICIR={icir:.4f}  门槛 min-grade={args.min_grade}")

    grade = sc["grade"]
    if meets_threshold(grade, args.min_grade):
        rid = save({
            "name": args.name or f"{args.factor}_{args.lookback}",
            "factor": args.factor, "lookback": args.lookback,
            "top_pct": args.top, "long_only": not args.long_short,
            "grade": grade, "good": sc["good"],
            "sharpe": sc["sharpe"], "fitness": sc["fitness"],
            "returns": sc["returns"], "turnover": sc["turnover"],
            "drawdown": sc["drawdown"], "margin": sc["margin"],
            "conc": sc["weight_concentration"],
            "ic": ic_mean, "icir": icir,
        })
        print(f"[保存] 达标 ({grade} >= {args.min_grade})，已存入库，id={rid}")
    else:
        print(f"[丢弃] 等级 {grade} 未达门槛 {args.min_grade}，不入库（已丢弃）")


def cmd_liblist(prices, args):
    print_library()


def cmd_librm(prices, args):
    if remove(args.id):
        print(f"[删除] id={args.id} 已从库移除")
    else:
        print(f"[删除] 未找到 id={args.id}")


def cmd_libclear(prices, args):
    n = clear()
    print(f"[清空] 已删除 {n} 条记录")


def cmd_fm(prices, args):
    """Fama-MacBeth 横截面回归，检验因子显著性。"""
    f = compute_factor(prices, args.factor, args.lookback)
    fm = factor_significance(f, prices, period=args.period)
    print_fm_report(fm, name=f"{args.factor} (lb={args.lookback})")


def main():
    ap = argparse.ArgumentParser(description="统一回测 / alpha 挖掘运行器")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="查看数据和可用因子")
    p_list.set_defaults(func=cmd_list)

    p_mine = sub.add_parser("mine", help="评价一个因子的预测力 (IC)")
    p_mine.add_argument("--factor", required=True, choices=list(FACTORS))
    p_mine.add_argument("--lookback", type=int, default=60)
    p_mine.add_argument("--period", type=int, default=5)
    p_mine.set_defaults(func=cmd_mine)

    p_scan = sub.add_parser("scan", help="扫描因子参数网格，找最优 IC/ICIR")
    p_scan.add_argument("--factor", required=True, choices=list(FACTORS))
    p_scan.add_argument("--lb-min", type=int, default=5)
    p_scan.add_argument("--lb-max", type=int, default=120)
    p_scan.add_argument("--lb-step", type=int, default=5)
    p_scan.add_argument("--period", type=int, default=5)
    p_scan.set_defaults(func=cmd_scan)

    p_fm = sub.add_parser("fm", help="Fama-MacBeth 检验因子显著性")
    p_fm.add_argument("--factor", required=True, choices=list(FACTORS))
    p_fm.add_argument("--lookback", type=int, default=60)
    p_fm.add_argument("--period", type=int, default=5)
    p_fm.set_defaults(func=cmd_fm)

    p_score = sub.add_parser("score", help="WorldQuant风格给因子评分（等级A-F）")
    p_score.add_argument("--factor", required=True, choices=list(FACTORS))
    p_score.add_argument("--lookback", type=int, default=60)
    p_score.add_argument("--top", type=float, default=0.2)
    p_score.add_argument("--long-short", action="store_true", help="多空对冲（默认纯多头）")
    p_score.set_defaults(func=cmd_score)

    p_lib = sub.add_parser("libsave", help="评分并存入Alpha库（不达标自动丢弃）")
    p_lib.add_argument("--factor", required=True, choices=list(FACTORS))
    p_lib.add_argument("--lookback", type=int, default=60)
    p_lib.add_argument("--top", type=float, default=0.2)
    p_lib.add_argument("--long-short", action="store_true", help="多空对冲（默认纯多头）")
    p_lib.add_argument("--period", type=int, default=5)
    p_lib.add_argument("--min-grade", default="B", choices=["A", "B", "C", "D", "F"],
                       help="入库门槛等级（默认B）")
    p_lib.add_argument("--name", default=None, help="给这条alpha起名")
    p_lib.set_defaults(func=cmd_libsave)

    p_liblist = sub.add_parser("liblist", help="查看Alpha库")
    p_liblist.set_defaults(func=cmd_liblist)

    p_librm = sub.add_parser("librm", help="从库删除")
    p_librm.add_argument("--id", type=int, required=True)
    p_librm.set_defaults(func=cmd_librm)

    p_libclr = sub.add_parser("libclear", help="清空库")
    p_libclr.set_defaults(func=cmd_libclear)

    p_bt = sub.add_parser("backtest", help="用因子做策略并回测")
    p_bt.add_argument("--factor", required=True, choices=list(FACTORS))
    p_bt.add_argument("--lookback", type=int, default=60)
    p_bt.add_argument("--top", type=float, default=0.2)
    p_bt.add_argument("--long-short", action="store_true", help="多空对冲（默认纯多头）")
    p_bt.set_defaults(func=cmd_backtest)

    args = ap.parse_args()
    prices = load_prices()
    if hasattr(args, "func"):
        args.func(prices, args)
    else:
        cmd_list(prices)


if __name__ == "__main__":
    main()
