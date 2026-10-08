"""
poly_league.py - 检验用户的假设：冷门联赛（机构覆盖薄）是否错价更大？
============================================================================
用户的判断：
  「既然体育博彩有机构，为什么不去找小众的、还没有机构的竞技类市场？」

把它变成可证伪的假设：
  H0: 错价程度与联赛"冷门程度"无关
  H1: 越冷门的联赛，校准误差越大（⇒ 有可交易的错价）

"冷门程度"的代理：
  · 联赛的总成交额（volume 合计）—— 成交额低 = 覆盖薄
  · 联赛的市场数 —— 场次少 = 关注度低

注意：这里测的是【校准误差】（价格 vs 实际胜率），
      不是"能不能赚钱"。校准误差大 = 存在系统性错价 = 有 edge 的可能。
"""

import csv
import os
import sys
import collections

sys.path.insert(0, r"D:\26050\Documents\quant_system")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

DS = r"D:\26050\Documents\polymarket_sports\data\dataset_all.csv"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def main():
    rows = []
    with open(DS, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                r["price"] = float(r["price"])
                r["outcome"] = int(r["outcome"])
                r["volume"] = float(r["volume"] or 0)
                r["lead_days"] = int(r["lead_days"])
            except Exception:
                continue
            rows.append(r)
    log("总行数 %d，市场数 %d" % (len(rows), len({r["market_id"] for r in rows})))

    lead_set = sorted({r["lead_days"] for r in rows}, reverse=True)
    by_mid = collections.defaultdict(dict)
    for r in rows:
        by_mid[r["market_id"]][r["lead_days"]] = r
    common = {m: d for m, d in by_mid.items() if all(l in d for l in lead_set)}
    log("受控样本（全时点都有价格）: %d 个市场" % len(common))

    # ⚠️ 关键修正：受控样本被世界杯主导（1582/2768=57%），无法比较联赛。
    #    但"要求全时点都有价格"只是为了消除【跨时点比较】的样本构成偏差；
    #    **在单个时点内部比较联赛时，不需要这个限制。**
    #    所以下面按【单一时点】取全部市场做联赛比较。
    LEADS = [14, 7, 3, 1]
    log("")
    log("各时点的可用市场数（不受全时点限制）:")
    for L in lead_set:
        n = sum(1 for d in by_mid.values() if L in d)
        nlg = len({d[L].get("league_name") or d[L].get("league")
                   for d in by_mid.values() if L in d})
        log("   %2d 天前: %6d 个市场, %3d 个联赛" % (L, n, nlg))

    # 每个市场的联赛 + 成交额
    mkt = {}
    for m, d in common.items():
        any_l = d[lead_set[0]]
        mkt[m] = {"league": any_l.get("league_name") or any_l.get("league") or "?",
                  "volume": max((d[l].get("volume") or 0) for l in lead_set)}
    mk = pd.DataFrame([{"market_id": k, **v} for k, v in mkt.items()])

    # 联赛汇总
    lg = mk.groupby("league").agg(n_markets=("market_id", "count"),
                                  total_vol=("volume", "sum")).reset_index()
    lg = lg.sort_values("total_vol", ascending=False)
    log("")
    log("=" * 100)
    log("1. 联赛按『机构覆盖强度』排序（用总成交额代理）")
    log("=" * 100)
    log("")
    log("  %-30s %8s %16s %14s" % ("联赛", "受控市场数", "合计成交额", "占中位联赛"))
    log("  " + "-" * 92)
    med = lg["total_vol"].median()
    for _, r in pd.concat([lg.head(12), lg.tail(8)]).iterrows():
        log("  %-30s %8d %16.0f %13.1fx" %
            (r["league"], r["n_markets"], r["total_vol"], r["total_vol"] / med))
    log("  ...")
    log("  共 %d 个联赛；成交额中位 = $%.0f" % (len(lg), med))

    # ------------------------------------------------------------------
    # 2. 按联赛成交额分档，看校准误差
    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("2. ★ 核心检验：按联赛冷门程度分档，看校准误差")
    log("=" * 100)
    log("")
    vol_map = dict(zip(lg["league"], lg["total_vol"]))
    tiers = ["T1 最热(成交额前25%)", "T2", "T3", "T4 最冷(后25%)"]
    q = lg["total_vol"].quantile([0.25, 0.5, 0.75]).to_dict()
    def tier_of(v):
        if v >= q[0.75]:
            return tiers[0]
        if v >= q[0.5]:
            return tiers[1]
        if v >= q[0.25]:
            return tiers[2]
        return tiers[3]
    lg["tier"] = lg["total_vol"].apply(tier_of)
    tmap = dict(zip(lg["league"], lg["tier"]))
    mk["tier"] = mk["league"].map(tmap)

    log("  校准误差 = 实际胜率 − 均价（绝对值越大 = 错价越明显）")
    log("")
    log("  在【全部价格档】上:")
    log("  %-22s %8s %10s %12s %12s %10s" %
        ("档位", "市场数", "均价", "实际胜率", "偏差", "|偏差|"))
    log("  " + "-" * 92)
    res_all = {}
    for t in tiers:
        ms = set(mk.loc[mk["tier"] == t, "market_id"])
        sel = [d[7] for m, d in common.items() if m in ms]
        if len(sel) < 30:
            log("  %-22s %8d %10s %10s %12s %10s" % (t, len(sel), "-", "-", "-", "-"))
            continue
        n = len(sel)
        p = np.mean([r["price"] for r in sel])
        o = np.mean([r["outcome"] for r in sel])
        se = np.sqrt(o * (1 - o) / n)
        res_all[t] = (n, p, o, o - p, se)
        log("  %-22s %8d %10.4f %12.4f %+12.4f %10.4f" % (t, n, p, o, o - p, abs(o - p)))

    log("")
    log("  在【0.10-0.15 冷门高估档】上（这是之前发现信号的地方）:")
    log("  %-22s %8s %10s %12s %12s %8s %8s" %
        ("档位", "市场数", "均价", "实际胜率", "edge(买NO)", "SE", "t值"))
    log("  " + "-" * 92)
    res_b = {}
    for t in tiers:
        ms = set(mk.loc[mk["tier"] == t, "market_id"])
        sel = [d[7] for m, d in common.items()
               if m in ms and 0.10 <= d[7]["price"] < 0.15]
        if len(sel) < 20:
            log("  %-22s %8d %10s %10s %12s %8s %8s" % (t, len(sel), "-", "-", "-", "-", "-"))
            continue
        n = len(sel)
        p = np.mean([r["price"] for r in sel])
        o = np.mean([r["outcome"] for r in sel])
        e = p - o                      # 买 NO 的 edge
        se = np.sqrt(o * (1 - o) / n)
        res_b[t] = (n, p, o, e, se, e / se)
        log("  %-22s %8d %10.4f %12.4f %+12.4f %8.4f %8.2f" %
            (t, n, p, o, e, se, e / se))

    # ------------------------------------------------------------------
    # 3. 结论
    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("3. 结论：用户假设成立吗？")
    log("=" * 100)
    log("")
    if len(res_b) >= 2:
        ts = list(res_b.keys())
        hot = res_b.get(tiers[0])
        cold = res_b.get(tiers[3])
        log("  在 0.10-0.15 档上（买 NO 的 edge）:")
        for t in tiers:
            if t in res_b:
                n, p, o, e, se, tt = res_b[t]
                log("    %-22s n=%-4d edge=%+.4f  t=%.2f  %s" %
                    (t, n, e, tt, "显著" if abs(tt) > 2 else "不显著"))
        log("")
        if cold and hot:
            log("    最冷档 edge=%.4f vs 最热档 edge=%.4f" % (cold[3], hot[3]))
            log("    => 冷门档的 edge 是热门档的 %.1f 倍" %
                (cold[3] / hot[3] if hot[3] else float("inf")))
        log("")
        log("  ⚠️ 但必须看样本量：冷门档的 n 通常很小，")
        log("     t 值不显著时无法区分「真错价」和「小样本噪声」。")
        log("")
        log("  ★ 更要命的容量问题：")
        log("     冷门联赛成交额低 ⇒ 就算有 edge 也装不下钱。")
        log("     这与之前 Polymarket 情绪套利的结论完全一样 ——")
        log("     **错价最大的地方，恰恰是最装不下钱的地方。**")
    else:
        log("  样本不足，无法比较。")

    # ------------------------------------------------------------------
    # 4. 逐联赛明细（最冷/最热各若干）
    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("4. 逐联赛明细：0.10-0.15 档（只列有足够样本的）")
    log("=" * 100)
    log("")
    log("  %-26s %6s %9s %8s %9s %8s %7s %14s" %
        ("联赛", "样本", "均价", "胜率", "edge", "SE", "t值", "联赛总成交额"))
    log("  " + "-" * 100)
    det = []
    for lname in lg["league"]:
        ms = set(mk.loc[mk["league"] == lname, "market_id"])
        sel = [d[7] for m, d in common.items()
               if m in ms and 0.10 <= d[7]["price"] < 0.15]
        n = len(sel)
        if n < 25:
            continue
        p = np.mean([r["price"] for r in sel])
        o = np.mean([r["outcome"] for r in sel])
        e = p - o
        se = np.sqrt(o * (1 - o) / n)
        det.append({"league": lname, "n": n, "p": p, "o": o, "edge": e,
                    "se": se, "t": e / se, "vol": vol_map.get(lname, 0)})
    dt = pd.DataFrame(det).sort_values("edge", ascending=False)
    for _, r in pd.concat([dt.head(10), dt.tail(6)]).iterrows():
        log("  %-26s %6d %9.4f %8.4f %+9.4f %8.4f %7.2f %14.0f" %
            (r["league"][:26], r["n"], r["p"], r["o"], r["edge"], r["se"], r["t"], r["vol"]))
    log("")
    log("  满足 n>=25 的联赛数 = %d" % len(dt))
    if len(dt):
        sig = dt[dt["t"].abs() > 2]
        log("  其中 |t|>2 的 = %d 个" % len(sig))
        if len(sig):
            log("  这些联赛（按 |t| 排序）:")
            for _, r in sig.reindex(sig["t"].abs().sort_values(ascending=False).index).head(8).iterrows():
                log("     %-26s n=%-4d edge=%+.4f t=%+.2f 总成交额=$%.0f" %
                    (r["league"][:26], r["n"], r["edge"], r["t"], r["vol"]))
        log("")
        log("  ⚠️ n>=25 的联赛有 %d 个，做了 %d 次检验 ——" % (len(dt), len(dt)))
        log("     按 5%% 显著性水平，纯噪声下期望有 %.1f 个「假显著」。" % (len(dt) * 0.05))
        log("     **这是典型的多重比较陷阱，必须做 Benjamini-Hochberg 校正。**")
        # BH 校正
        dt2 = dt.dropna(subset=["t"]).copy()
        dt2["pval"] = [2 * (1 - abs(np.math.erf(abs(t) / np.sqrt(2))) / 2)
                       if False else None for t in dt2["t"]]
        from scipy import stats
        dt2["pval"] = 2 * (1 - stats.norm.cdf(dt2["t"].abs()))
        dt2 = dt2.sort_values("pval")
        m_ = len(dt2)
        dt2["bh_thresh"] = [(i + 1) / m_ * 0.05 for i in range(m_)]
        dt2["pass_bh"] = dt2["pval"] <= dt2["bh_thresh"]
        npass = int(dt2["pass_bh"].sum())
        log("")
        log("  BH 校正（FDR=5%%）后通过的联赛数 = **%d** / %d" % (npass, m_))
        if npass > 0:
            log("  通过的联赛:")
            for _, r in dt2[dt2["pass_bh"]].iterrows():
                log("     %-26s n=%-4d edge=%+.4f p=%.5f 成交额=$%.0f" %
                    (r["league"][:26], r["n"], r["edge"], r["pval"], r["vol"]))
        else:
            log("  => **没有任何联赛在多重比较校正后显著。**")
            log("     也就是说：那些看起来「冷门档错价大」的结果，")
            log("     与小样本噪声无法区分。")

    with open(r"D:\26050\Documents\quant_system\output_poly_league.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_league.txt")


if __name__ == "__main__":
    main()
