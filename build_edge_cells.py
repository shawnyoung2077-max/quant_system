"""
build_edge_cells.py —— 生成「联赛 x 价格细档」的偏差表（交易门槛的数据来源）
============================================================================
为什么需要它：

用户的要求是「限制在偏差较大的时候才交易」。但当时的下注规则只是
`价格落在 [0.15, 0.30) 就买 YES` —— 那是一个**价格区间**，不是**偏差**。
两者完全不同：同一区间里，偏差可以从 -14.5pp 到 +31.4pp。

所以需要一张能回答「这个联赛、这个价位的市场，历史上到底高估还是低估、
偏差够不够大」的表。本脚本产出它。

口径（与 build_league_support.py 保持一致，并补上实测的价差成本）：

    gross_bias = mean(outcome) - mean(price)
    cost       = fee_per_share + spread_cost
    net_bias   = gross_bias - cost

    fee_per_share = 0.05 * p * (1-p)     官方 taker 公式（体育类 feeRate=0.05）
    spread_cost   = 0.0087               本项目实测「成交价 - 当时中间价」均值

    ⚠ 为什么必须扣价差：历史 price 来自 CLOB /prices-history，
      那是成交/中间价；而我们下单付的是 ask，实测平均高 0.87 分。
      不扣掉这一项，算出来的"偏差"就是虚的。

统计：
    · 按 market_id 聚类 bootstrap（同一场比赛的多个市场高度相关）
    · BH 多重比较校正
    · supported = 聚类区间下界 > 0 且 q < 0.05（**只代表统计显著**）

经济门槛（net_bias 要多大才值得下手）由 scan.py 的 MIN_EDGE_PP 决定 ——
两者分开，这样调门槛不用重建表。
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
OUT = os.path.join(HERE, "modeling", "edge_cells.csv")

BAND_LO, BAND_HI = 0.15, 0.30
BINS = [0.15, 0.18, 0.21, 0.24, 0.27, 0.30]
LEADS = (1, 3)              # 实盘真正能成交的 lead（3 天以外基本无盘口，见 docs）
MIN_N = 40                  # 单元格最少市场数
FEE_RATE = 0.05             # 官方 taker（体育）
SPREAD_COST = 0.0087        # 实测：成交价 - 当时中间价 的均值
N_BOOT = 2000
SEED = 20261010


def bh_adjust(pvals):
    p = np.asarray(pvals, float)
    order = np.argsort(p)
    adj = np.empty(len(p))
    running = 1.0
    for rank_idx in range(len(p) - 1, -1, -1):
        i = order[rank_idx]
        running = min(running, p[i] * len(p) / (rank_idx + 1))
        adj[i] = running
    return adj


def main():
    df = pd.read_csv(SRC, low_memory=False)
    df = df[df["price"].notna() & df["outcome"].notna()]
    d = df[(df["price"] >= BAND_LO) & (df["price"] < BAND_HI)
           & (df["lead_days"].isin(LEADS))].copy()
    d["bin_lo"] = pd.cut(d["price"], BINS, right=False, labels=BINS[:-1])
    print("档内样本 %d 行 / %d 市场 / %d 联赛"
          % (len(d), d["market_id"].nunique(), d["league_name"].nunique()))

    rng = np.random.default_rng(SEED)
    rows = []
    for (lg, blo), g in d.groupby(["league_name", "bin_lo"], observed=True):
        m = g.groupby("market_id").agg(price=("price", "mean"),
                                       outcome=("outcome", "mean"))
        n = len(m)
        price = float(m["price"].mean())
        out = float(m["outcome"].mean())
        gross = out - price
        fee = FEE_RATE * price * (1 - price)
        net = gross - fee - SPREAD_COST

        # 按市场聚类 bootstrap
        arr = (m["outcome"] - m["price"]).to_numpy(float)
        if n >= 2:
            draws = rng.integers(0, n, size=(N_BOOT, n))
            boot = arr[draws].mean(axis=1)
            ci_lo, ci_hi = np.quantile(boot, [0.025, 0.975])
            se = float(arr.std(ddof=1) / np.sqrt(n))
            t = gross / se if se > 0 else np.nan
            pval = float(stats.t.sf(t, n - 1)) if np.isfinite(t) else 1.0
        else:
            ci_lo = ci_hi = np.nan
            se = np.nan
            t = np.nan
            pval = 1.0

        rows.append({
            "league_name": lg,
            "price_lo": float(blo),
            "price_hi": float(blo) + 0.03,
            "n_market": n,
            "n_row": len(g),
            "mean_price": round(price, 4),
            "mean_outcome": round(out, 4),
            "gross_bias": round(gross, 4),
            "cost": round(fee + SPREAD_COST, 4),
            "net_bias": round(net, 4),
            "se_cluster": round(se, 4) if np.isfinite(se) else "",
            "t_cluster": round(t, 2) if np.isfinite(t) else "",
            "ci_low_boot": round(float(ci_lo), 4) if np.isfinite(ci_lo) else "",
            "ci_high_boot": round(float(ci_hi), 4) if np.isfinite(ci_hi) else "",
            "p_one_sided": pval,
            "supported": 0,
        })

    elig = [i for i, r in enumerate(rows) if r["n_market"] >= MIN_N]
    if elig:
        q = bh_adjust([rows[i]["p_one_sided"] for i in elig])
        for k, i in enumerate(elig):
            rows[i]["q_bh"] = round(float(q[k]), 6)
            rows[i]["supported"] = int(
                q[k] < 0.05 and float(rows[i]["ci_low_boot"] or -1) > 0)
    for r in rows:
        r.setdefault("q_bh", "")

    rows.sort(key=lambda r: (-r["supported"], -r["net_bias"]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    sup = [r for r in rows if r["supported"]]
    print("单元格 %d 个；n>=%d 的 %d 个；统计显著（supported=1）的 %d 个"
          % (len(rows), MIN_N, len(elig), len(sup)))
    for th in (0.03, 0.05, 0.08, 0.10):
        s = [r for r in rows if r["supported"] and r["net_bias"] >= th]
        print("   净偏差门槛 %+.2f -> 还剩 %2d 个单元格，覆盖历史 %5d 个市场"
              % (th, len(s), sum(r["n_market"] for r in s)))
    print("写出 -> %s" % OUT)
    return rows


if __name__ == "__main__":
    main()
