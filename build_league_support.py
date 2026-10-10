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

本脚本把「每个联赛在该价格档的历史证据」固化成一张表，
让 report.py 能把实盘结果按"有/无历史支撑"分层，
避免再用一个聚合数字去代表所有联赛。
"""

import os
import sys
import csv
import numpy as np
import pandas as pd

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
MIN_N = 30              # 少于此样本量不给结论，只标 no_support


def main():
    df = pd.read_csv(SRC, low_memory=False)
    df = df[df["price"].notna() & df["outcome"].notna()]
    band = df[(df["price"] >= BAND_LO) & (df["price"] < BAND_HI)
              & (df["lead_days"].isin(LEADS))]

    rows = []
    for lg, g in band.groupby("league_name"):
        n = len(g)
        p = float(g["price"].mean())
        o = float(g["outcome"].mean())
        se = float(np.sqrt(o * (1 - o) / n)) if 0 < o < 1 else float("nan")
        bias = o - p
        t = bias / se if se and se > 0 else float("nan")
        rows.append({
            "league_name": lg,
            "n_band": n,
            "mean_price": round(p, 4),
            "mean_outcome": round(o, 4),
            "bias": round(bias, 4),
            "t": round(t, 2) if t == t else "",
            "supported": 1 if (n >= MIN_N and t == t and t > 2) else 0,
        })
    rows.sort(key=lambda r: -r["n_band"])

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    sup = [r for r in rows if r["supported"]]
    print("联赛数 %d，其中【有历史支撑】(n>=%d 且 t>2) 的 %d 个"
          % (len(rows), MIN_N, len(sup)))
    print("  这 %d 个联赛覆盖本档样本 %d 行 / 全部 %d 行 = %.1f%%"
          % (len(sup), sum(r["n_band"] for r in sup), sum(r["n_band"] for r in rows),
             sum(r["n_band"] for r in sup) / max(1, sum(r["n_band"] for r in rows)) * 100))
    print("写出 -> %s" % OUT)
    return rows


if __name__ == "__main__":
    main()
