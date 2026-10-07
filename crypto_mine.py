"""
crypto_mine.py - 加密货币 alpha 挖掘（复用平台纪律：IC初筛 → 全网格 → 最强入库）
数据：crypto_data 缓存的 Binance 全量历史
评分：复用 alpha_score（ppy=365，加密货币全年无休）
"""
import sys, os
sys.path.insert(0, r"D:\26050\Documents\quant_system")
import numpy as np, pandas as pd
import factor as F
from alpha_score import score_factor
import alpha_library as L

PPY = 365  # 加密货币：全年 365 天


def load_crypto_prices(interval="1d", field="close"):
    from crypto_data import build_panel, list_available
    p = build_panel(interval, field)
    if p.empty:
        return p
    # 清理：去掉长期无数据的币
    p = p.dropna(axis=1, thresh=int(len(p) * 0.6)).ffill()
    p = p.dropna(axis=0, thresh=int(p.shape[1] * 0.5))
    return p


def screen(prices, periods=(1, 3, 5, 10, 20, 60, 120)):
    """IC 初筛（加密货币：单币样本少，IC 主要看时序/截面预测力）。"""
    rows = []
    cands = {}
    for n in (3, 5, 10, 20, 60, 120, 200):
        cands[f"mom{n}"] = lambda n=n: F.momentum(prices, n)
    for n in (1, 3, 5, 7):
        cands[f"rev{n}"] = lambda n=n: F.short_term_reversal(prices, n)
    for n in (7, 20, 60):
        cands[f"vol{n}"] = lambda n=n: F.volatility_factor(prices, n)
        cands[f"pma{n}"] = lambda n=n: F.price_vs_ma(prices, n)
    cands["volume"] = lambda: F.volume_factor(prices)
    for name, fn in cands.items():
        try:
            f = fn().reindex(prices.index).reindex(prices.columns, axis=1)
        except Exception:
            continue
        for p in periods:
            try:
                ic = F.factor_ic(f, F.forward_returns(prices, p)).replace([np.inf, -np.inf], np.nan).dropna()
                if len(ic) < 30: continue
                rows.append({"factor": name, "period": p, "IC": round(float(ic.mean()), 4),
                             "ICIR": round(float(ic.mean() / ic.std()), 4) if ic.std() > 0 else 0,
                             "|IC|": abs(round(float(ic.mean()), 4))})
            except Exception:
                pass
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    best = d.sort_values("|IC|", ascending=False).drop_duplicates("factor").sort_values("|IC|", ascending=False)
    return best


def grid(prices, top_list=(0.1, 0.2, 0.3), hold_list=(1, 3, 7), ls=True):
    """全网格：动量/反转/波动 × lookback × top × hold。"""
    rows = []
    specs = []
    for n in (5, 10, 20, 30, 60, 120):
        specs.append((f"mom{n}", lambda n=n: F.momentum(prices, n)))
        specs.append((f"rev{n}", lambda n=n: F.short_term_reversal(prices, n)))
    for n in (10, 20, 60):
        specs.append((f"vol{n}", lambda n=n: F.volatility_factor(prices, n)))
    for name, fn in specs:
        base = fn().reindex(prices.index).reindex(prices.columns, axis=1)
        z = F.zscore(base)
        for top in top_list:
            for hold in hold_list:
                try:
                    sc = score_factor(prices, z, top_pct=top, long_only=not ls,
                                      commission_bps=10, slippage_bps=5, hold=hold, ppy=PPY)
                except Exception:
                    continue
                rows.append({"factor": name, "top": top, "hold": hold,
                             "grade": sc["grade"], "sharpe": round(sc["sharpe"], 3),
                             "fitness": round(sc["fitness"], 3), "ann_ret": round(sc["returns"], 4),
                             "turn_d": round(sc["turnover"], 3), "good": sc["good"]})
                print(f"  {name:7s} top={top} hold={hold} -> {sc['grade']} "
                      f"sh={sc['sharpe']:.2f} fit={sc['fitness']:.2f} ret={sc['returns']:.1%}", flush=True)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    prices = load_crypto_prices("1d")
    print(f"[crypto] panel {prices.shape[0]}d x {prices.shape[1]}coins  {prices.index.min().date()}~{prices.index.max().date()}")
    print("\n===== IC 初筛 =====")
    print(screen(prices).to_string(index=False))
    print("\n===== 全网格（long-short 市场中性）=====")
    g = grid(prices)
    g = g.sort_values("fitness", ascending=False)
    print("\n===== Top15 =====")
    print(g.head(15).to_string(index=False))
    g.to_csv("output_crypto_grid.csv", index=False)
    print("saved output_crypto_grid.csv")
