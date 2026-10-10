"""
build_league_support.py —— 生成「历史支撑度」表
============================================================================
为什么需要它：

实盘第一轮（2026-10-09/10，56 笔已结算、33 个独立市场）测出
    k = 实际胜率 - 市场隐含概率 = -11.5pp
而当初支撑这个策略的历史结论是同价格档 +7.8pp。

追下去发现根因不是"策略错"，而是**证据基础与实盘部署几乎不重叠**：

  · 历史数据集 74,242 行 / 179 个联赛 / [0.15,0.30) 档 11,239 行
  · 它的主力是 World Cup(19k行)、LaLiga、NFL、Premier League、Bundesliga …
  · 但实盘把 32/56 笔押在 Eerste Divisie(历史 8 行, 本档 0 行) 和 Ligue 2(85 行, 本档 3 行)
  · 且历史效应本身高度异质：同档同 lead，联赛间 bias 从 -10.0pp 到 +36.3pp

也就是说："这一档 +8pp" 是对**大联赛/世界杯**成立的平均结论，
不能搬到荷乙/法乙上 —— 这是典型的分布外外推。

旧版把每个快照行当独立样本、逐联赛用 t>2 判定，容易因重复市场和多重检验
误把噪声放进 valid 白名单。新版以 market_id 为独立簇，先扣 taker 费，
对逐市场结果做区间估计，并用 Benjamini-Hochberg 控制逐联赛筛选的假发现率。
历史文件没有盘口价差，因此该检验仍未扣除价差成本。

本脚本把「每个联赛在该价格档的历史证据」固化成一张表，
让 report.py 能把实盘结果按"有/无历史支撑"分层，
避免再用一个聚合数字去代表所有联赛。
"""

import os
import sys
import csv
import numpy as np
import pandas as pd
from scipy import stats

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "modeling", "historical_rows.csv")
OUT = os.path.join(HERE, "modeling", "league_support.csv")

# 与 BET_RULES 里的主力档保持一致
BAND_LO, BAND_HI = 0.15, 0.30
LEADS = (1, 3)          # 实盘真正能成交的 lead（3/7 天基本无盘口，见 docs）
MIN_N = 30              # 独立市场数；快照行不作为独立样本
BOOTSTRAPS = 4000
SEED = 20261010


def bh_adjust(pvals):
    p = np.asarray(pvals, dtype=float)
    order = np.argsort(p)
    q = np.empty(len(p), dtype=float)
    running = 1.0
    for rank in range(len(p) - 1, -1, -1):
        i = order[rank]
        running = min(running, p[i] * len(p) / (rank + 1))
        q[i] = running
    return q


def main():
    df = pd.read_csv(SRC, low_memory=False)
    df = df[df["price"].notna() & df["outcome"].notna()
            & df["market_id"].notna()]
    df["market_id"] = df["market_id"].astype(str)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["outcome"] = pd.to_numeric(df["outcome"], errors="coerce")
    df["lead_days"] = pd.to_numeric(df["lead_days"], errors="coerce")
    df = df.dropna(subset=["price", "outcome", "lead_days"])
    band = df[(df["price"] >= BAND_LO) & (df["price"] < BAND_HI)
              & (df["lead_days"].isin(LEADS))]
    # One record per market and lead. Repeated snapshots must not inflate n.
    band = (band.sort_values("lead_days")
            .drop_duplicates(["market_id", "lead_days"], keep="last").copy())
    band["gross_bias"] = band["outcome"] - band["price"]
    band["fee_per_share"] = 0.05 * band["price"] * (1 - band["price"])
    band["net_bias"] = band["gross_bias"] - band["fee_per_share"]

    rows = []
    rng = np.random.default_rng(SEED)
    for lg, g in band.groupby("league_name"):
        # Markets are the independent units; average across 1d/3d entries first.
        per_market = g.groupby("market_id")["net_bias"].mean().to_numpy(float)
        gross_market = g.groupby("market_id")["gross_bias"].mean().to_numpy(float)
        n = len(per_market)
        bias = float(per_market.mean()) if n else float("nan")
        se = float(per_market.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
        t = bias / se if se and se > 0 else float("nan")
        pval = float(stats.t.sf(t, n - 1)) if n >= MIN_N and np.isfinite(t) else 1.0
        if n:
            draw = rng.integers(0, n, size=(BOOTSTRAPS, n))
            boot = per_market[draw].mean(axis=1)
            ci_low, ci_high = np.quantile(boot, [0.025, 0.975])
        else:
            ci_low = ci_high = float("nan")
        rows.append({
            "league_name": lg,
            "n_band": n, "n_rows": int(len(g)),
            "mean_price": round(float(g["price"].mean()), 4),
            "mean_outcome": round(float(g["outcome"].mean()), 4),
            "gross_bias": round(float(gross_market.mean()), 4) if n else "",
            "bias": round(bias, 4) if np.isfinite(bias) else "",
            "se_cluster": round(se, 4) if np.isfinite(se) else "",
            "t_cluster": round(t, 2) if np.isfinite(t) else "",
            "ci_low_boot": round(float(ci_low), 4) if np.isfinite(ci_low) else "",
            "ci_high_boot": round(float(ci_high), 4) if np.isfinite(ci_high) else "",
            "p_one_sided": pval,
            "supported": 0,
        })
    eligible = [i for i, r in enumerate(rows) if r["n_band"] >= MIN_N]
    if eligible:
        qvals = bh_adjust([rows[i]["p_one_sided"] for i in eligible])
        for i, q in zip(eligible, qvals):
            rows[i]["q_bh"] = round(float(q), 6)
            rows[i]["supported"] = int(
                q < 0.05 and float(rows[i]["ci_low_boot"]) > 0)
    for i, r in enumerate(rows):
        r.setdefault("q_bh", "")
    rows.sort(key=lambda r: -r["n_band"])

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    sup = [r for r in rows if r["supported"]]
    print("联赛数 %d，其中【有历史支撑】(独立市场 n>=%d，fee-adjusted CI>0，BH q<0.05) 的 %d 个"
          % (len(rows), MIN_N, len(sup)))
    print("  这 %d 个联赛覆盖本档样本 %d 行 / 全部 %d 行 = %.1f%%"
          % (len(sup), sum(r["n_band"] for r in sup), sum(r["n_band"] for r in rows),
             sum(r["n_band"] for r in sup) / max(1, sum(r["n_band"] for r in rows)) * 100))
    print("写出 -> %s" % OUT)
    return rows


if __name__ == "__main__":
    main()
