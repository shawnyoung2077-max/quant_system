"""
reproduce.py - 配置化论文复现运行器
====================================
用一份配置（数据窗口/universe/因子/参数）锁定，一键复现一篇因子研究：
  生成因子面板 -> 组合构建 -> Fama-MacBeth 显著性 -> FF alpha -> 存报告。
保证可复现（同配置同结果）。

用法：
    python reproduce.py --config configs/momentum_study.json
配置示例见 configs/ 目录。
"""

import argparse
import json
import os
import time

import numpy as np
import pandas as pd

import run as R
from factor import momentum, forward_returns, factor_to_weights, evaluate_factor
from backtest import portfolio_backtest, buy_and_hold
from research import factor_significance, print_fm_report, winsorize, cross_section_zscore
from stats import ff_alpha
from metrics import summary


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def window_universe(prices: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """按配置裁剪数据窗口与股票池。"""
    start = cfg.get("start", None)
    end = cfg.get("end", None)
    universe = cfg.get("universe", None)
    if start: prices = prices[prices.index >= pd.Timestamp(start)]
    if end: prices = prices[prices.index <= pd.Timestamp(end)]
    if universe:
        prices = prices[[c for c in prices.columns if c in universe]]
    # 清洗（宽松）
    prices = prices.dropna(axis=1, thresh=int(prices.shape[0] * 0.05))
    prices = prices.dropna(axis=0, thresh=int(prices.shape[1] * 0.02)).ffill()
    return prices


def _py(x):
    """把 numpy 标量转成 Python 原生类型（可 JSON 序列化）。"""
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, dict):
        return {k: _py(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_py(v) for v in x]
    return x


def run_study(cfg: dict, out_dir: str = "studies") -> dict:
    prices = window_universe(R.load_prices(), cfg)
    factor_name = cfg["factor"]
    lookback = cfg.get("lookback", 60)
    top = cfg.get("top", 0.2)
    period = cfg.get("period", 5)
    long_only = cfg.get("long_only", True)
    long_short = not long_only

    print(f"[study] {factor_name} lb={lookback} | {prices.shape[1]}股 x {prices.shape[0]}天 "
          f"({prices.index[0].date()}~{prices.index[-1].date()})")

    # 因子：价格类用预设算子，基本面类用 fundamentals
    FUND_FACTORS = ["bm", "size", "roe", "roa", "investment", "profit_growth"]
    if factor_name in FUND_FACTORS:
        from fundamentals import build_factor_panels
        panels = build_factor_panels(prices, symbols=list(prices.columns), progress=False)
        f = panels[factor_name]
    else:
        f = R.compute_factor(prices, factor_name, lookback)

    # 1) 组合回测
    w = factor_to_weights(f, top_pct=top, long_only=long_only)
    ret, turnover = portfolio_backtest(prices, w)
    bench = buy_and_hold(prices)
    s = summary(ret)

    # 2) Fama-MacBeth 显著性
    fm = factor_significance(f, prices, period=period)
    print_fm_report(fm, f"{factor_name}_{lookback}")

    # 3) FF alpha（相对市场等权基准）
    mkt = buy_and_hold(prices)
    a = ff_alpha(ret, mkt)
    alpha = a["params"]["alpha"]; alpha_t = a["tstats"]["alpha"]
    print(f"\nFF alpha: {alpha:.5f}  t={alpha_t:.2f}  显著={abs(alpha_t) > 2}  beta_mkt={a['params'].get('MKT'):.3f}  R2={a['r2']:.3f}")

    # 4) IC/ICIR
    icr = evaluate_factor(f, prices, period=period, name=factor_name, quiet=True)

    report = {
        "config": cfg,
        "data": {"stocks": int(prices.shape[1]), "days": int(prices.shape[0]),
                 "start": str(prices.index[0].date()), "end": str(prices.index[-1].date())},
        "metrics": {"annualized_return": s["annualized_return"], "sharpe": s["sharpe"],
                    "max_drawdown": s["max_drawdown"], "total_return": s["total_return"],
                    "turnover": float(turnover.mean() * 252)},
        "fm": {"lambda": fm.get("lambda_mean"), "tstat": fm.get("nw_tstat"),
               "p": fm.get("p_value"), "significant": fm.get("p_value", 1) < 0.05},
        "ff_alpha": {"alpha": alpha, "alpha_t": alpha_t, "beta_mkt": float(a["params"].get("MKT")),
                     "r2": a["r2"], "significant": abs(alpha_t) > 2},
        "ic": {"ic_mean": icr["ic_mean"], "icir": icr["icir"]},
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    os.makedirs(out_dir, exist_ok=True)
    name = cfg.get("name", f"{factor_name}_{lookback}")
    path = os.path.join(out_dir, f"{name}.json")
    with open(path, "w", encoding="utf-8") as fout:
        json.dump(_py(report), fout, ensure_ascii=False, indent=2)
    print(f"\n[study] 报告已保存: {path}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="studies")
    args = ap.parse_args()
    cfg = load_config(args.config)
    run_study(cfg, args.out)
