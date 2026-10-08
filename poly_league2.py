"""
poly_league2.py - 用【单一时点】全样本检验「冷门联赛错价更大」的假设
============================================================================
为什么重做：poly_league.py 用了"全 6 个时点都有价格"的受控样本，
但那个样本被世界杯主导（1582/2768 = 57%），根本无法比较联赛。

修正：受控样本的目的是消除【跨时点比较】的样本构成偏差。
      而【同一时点内比较联赛】不需要这个限制 ——
      同一个时点、同一批市场、只是按联赛分组，不存在构成偏差。
      所以直接用 lead_days=7（以及 3/14）的全部市场。

假设：联赛越冷门（成交额越低）→ 机构覆盖越薄 → 校准误差越大
"""

import csv
import collections
import sys

sys.path.insert(0, r"D:\26050\Documents\quant_system")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
from scipy import stats

DS = r"D:\26050\Documents\polymarket_sports\data\dataset_all.csv"
OUT = []
BUCKET_LO, BUCKET_HI = 0.10, 0.15


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def main():
    by_mid = collections.defaultdict(dict)
    with open(DS, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                r["price"] = float(r["price"])
                r["outcome"] = int(r["outcome"])
                r["volume"] = float(r["volume"] or 0)
                r["lead_days"] = int(r["lead_days"])
            except Exception:
                continue
            by_mid[r["market_id"]][r["lead_days"]] = r
    log("市场数 %d" % len(by_mid))

    lead_set = sorted({l for d in by_mid.values() for l in d}, reverse=True)
    for L in (14, 7, 3):
        n = sum(1 for d in by_mid.values() if L in d)
        log("  %2d 天前可用: %6d 个市场" % (L, n))

    for LEAD in (7, 3, 14):
        sel = [d[LEAD] for d in by_mid.values() if LEAD in d]
        log("")
        log("=" * 100)
        log("时点 = 结算前 %d 天（n=%d）" % (LEAD, len(sel)))
        log("=" * 100)

        df = pd.DataFrame([{
            "league": r.get("league_name") or r.get("league") or "?",
            "price": r["price"], "outcome": r["outcome"], "volume": r["volume"],
        } for r in sel])

        # 联赛汇总
        lg = df.groupby("league").agg(n=("price", "size"),
                                      vol=("volume", "sum"),
                                      mp=("price", "mean"),
                                      mo=("outcome", "mean")).reset_index()
        q = lg["vol"].quantile([0.25, 0.5, 0.75]).to_dict()

        def tier(v):
            return ("T1 最热" if v >= q[0.75] else "T2" if v >= q[0.5]
                    else "T3" if v >= q[0.25] else "T4 最冷")

        lg["tier"] = lg["vol"].apply(tier)

        log("")
        log("  --- A. 全价格档：按联赛冷门程度分档 ---")
        log("  %-10s %8s %10s %12s %12s %8s" %
            ("档位", "市场数", "均价", "实际胜率", "偏差", "SE"))
        log("  " + "-" * 92)
        for t in ("T1 最热", "T2", "T3", "T4 最冷"):
            s = df[df["league"].isin(set(lg.loc[lg["tier"] == t, "league"]))]
            if len(s) < 30:
                log("  %-10s %8d %10s %10s %12s %8s" % (t, len(s), "-", "-", "-", "-"))
                continue
            p, o = s["price"].mean(), s["outcome"].mean()
            log("  %-10s %8d %10.4f %12.4f %+12.4f %8.4f" %
                (t, len(s), p, o, o - p, np.sqrt(o * (1 - o) / len(s))))

        log("")
        log("  --- B. 只看 0.10-0.15 档（买 NO 的 edge）---")
        log("  %-10s %8s %10s %12s %12s %8s %7s %s" %
            ("档位", "市场数", "均价", "实际胜率", "edge", "SE", "t值", "显著?"))
        log("  " + "-" * 92)
        resB = {}
        for t in ("T1 最热", "T2", "T3", "T4 最冷"):
            s = df[(df["league"].isin(set(lg.loc[lg["tier"] == t, "league"])))
                   & (df["price"] >= BUCKET_LO) & (df["price"] < BUCKET_HI)]
            if len(s) < 20:
                log("  %-10s %8d %10s %10s %12s %8s %7s %s" %
                    (t, len(s), "-", "-", "-", "-", "-", "样本少"))
                continue
            p, o = s["price"].mean(), s["outcome"].mean()
            e = p - o
            se = np.sqrt(o * (1 - o) / len(s))
            tt = e / se
            resB[t] = (len(s), p, o, e, se, tt)
            log("  %-10s %8d %10.4f %12.4f %+12.4f %8.4f %7.2f %s" %
                (t, len(s), p, o, e, se, tt, "是" if abs(tt) > 2 else "否"))
        log("")
        if len(resB) >= 3:
            ts = [resB[t][3] for t in ("T1 最热", "T2", "T3", "T4 最冷") if t in resB]
            log("    edge 按【热→冷】序列: %s" % "  ".join("%+.4f" % x for x in ts))
            if len(ts) >= 3:
                hot, cold = ts[0], ts[-1]
                log("    最冷档 vs 最热档: %+.4f vs %+.4f  =>  %s" %
                    (cold, hot,
                     "冷门更大 [假设成立]" if cold > hot else "冷门【不】更大 [假设不成立]"))

        # 逐联赛 + 多重比较校正
        log("")
        log("  --- C. 逐联赛（0.10-0.15 档，n>=25）+ BH 多重比较校正 ---")
        det = []
        for _, r in lg.iterrows():
            s = df[(df["league"] == r["league"]) & (df["price"] >= BUCKET_LO)
                   & (df["price"] < BUCKET_HI)]
            n = len(s)
            if n < 25:
                continue
            p, o = s["price"].mean(), s["outcome"].mean()
            e = p - o
            se = np.sqrt(o * (1 - o) / n)
            det.append({"league": r["league"], "n": n, "p": p, "o": o,
                        "edge": e, "se": se, "t": e / se, "vol": r["vol"]})
        if not det:
            log("    无满足 n>=25 的联赛")
        else:
            dt = pd.DataFrame(det).sort_values("edge", ascending=False)
            dt["pval"] = 2 * (1 - stats.norm.cdf(dt["t"].abs()))
            dt = dt.sort_values("pval").reset_index(drop=True)
            m_ = len(dt)
            dt["bh"] = [(i + 1) / m_ * 0.05 for i in range(m_)]
            dt["pass"] = dt["pval"] <= dt["bh"]
            log("    满足 n>=25 的联赛 = %d 个（即做了 %d 次检验）" % (m_, m_))
            log("    纯噪声下期望假显著数 = %.1f" % (m_ * 0.05))
            log("    BH 校正后通过 = **%d** 个" % int(dt["pass"].sum()))
            log("")
            log("    %-24s %6s %9s %8s %10s %8s %10s" %
                ("联赛", "样本", "均价", "胜率", "edge", "p值", "联赛成交额"))
            log("    " + "-" * 92)
            for _, r in dt.head(12).iterrows():
                log("    %-24s %6d %9.4f %8.4f %+10.4f %8.4f %10.0f" %
                    (r["league"][:24], r["n"], r["p"], r["o"], r["edge"],
                     r["pval"], r["vol"]))
            if dt["pass"].any():
                log("")
                log("    *** BH 校正后仍显著的联赛 ***")
                for _, r in dt[dt["pass"]].iterrows():
                    log("       %-24s n=%-4d edge=%+.4f p=%.5f 成交额=$%.0f" %
                        (r["league"][:24], r["n"], r["edge"], r["pval"], r["vol"]))

        # 相关性：edge 大小 vs 联赛成交额
        if det:
            dt = pd.DataFrame(det)
            dt = dt[dt["n"] >= 25]
            if len(dt) >= 6:
                rho, pv = stats.spearmanr(dt["vol"], dt["edge"].abs())
                log("")
                log("  --- D. 关键相关性检验 ---")
                log("     Spearman(联赛成交额, |edge|) = %.3f  (p=%.4f, n=%d)" %
                    (rho, pv, len(dt)))
                if rho < 0 and pv < 0.05:
                    log("     => **负相关且显著**：联赛越小众，错价越大  [用户假设成立]")
                elif rho < 0:
                    log("     => 负相关但不显著（p=%.3f）：方向符合假设，但证据不足" % pv)
                else:
                    log("     => 无负相关：**用户假设不成立**（小众联赛并未错价更大）")

    with open(r"D:\26050\Documents\quant_system\output_poly_league2.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_league2.txt")


if __name__ == "__main__":
    main()
