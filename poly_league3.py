"""
poly_league3.py - 最终检验：逐联赛校准误差 vs 联赛冷门程度（用全部价格档提高样本量）
============================================================================
为什么需要：0.10-0.15 单档太窄，导致 n>=25 的联赛只有 4-7 个，没有统计力度。
改用【全部价格档】的校准误差（每个联赛几千个市场），力度大幅提升。

指标：每联赛的 |平均胜率 − 平均价格|，以及加权校准误差
      （更严格的指标：Brier / 分档校准的卡方统计量）
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

DS = r"D:\26050\Documents\quant_system\..\polymarket_sports\data\dataset_all.csv"
DS = r"D:\26050\Documents\polymarket_sports\data\dataset_all.csv"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def main():
    by_mid = collections.defaultdict(dict)
    with open(DS, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                r["price"] = float(r["price"]); r["outcome"] = int(r["outcome"])
                r["volume"] = float(r["volume"] or 0); r["lead_days"] = int(r["lead_days"])
            except Exception:
                continue
            by_mid[r["market_id"]][r["lead_days"]] = r

    for LEAD in (7, 3):
        sel = [d[LEAD] for d in by_mid.values() if LEAD in d]
        df = pd.DataFrame([{"league": r.get("league_name") or "?",
                            "price": r["price"], "outcome": r["outcome"],
                            "volume": r["volume"]} for r in sel])
        log("=" * 104)
        log("时点 = 结算前 %d 天，n = %d 个市场，%d 个联赛" %
            (LEAD, len(df), df["league"].nunique()))
        log("=" * 104)

        # 逐联赛：校准误差 = 平均(实际) − 平均(价格)，以及 Brier 分解中的 reliability
        rows = []
        for lname, s in df.groupby("league"):
            n = len(s)
            if n < 60:
                continue
            p, o = s["price"].mean(), s["outcome"].mean()
            se = np.sqrt(o * (1 - o) / n)
            vol = s["volume"].sum()
            # reliability（分档后偏差的平方加权）—— 更严格的错价指标
            rel = 0.0
            for lo in np.arange(0, 1.0, 0.05):
                b = s[(s["price"] >= lo) & (s["price"] < lo + 0.05)]
                if len(b) >= 10:
                    rel += len(b) / n * (b["outcome"].mean() - b["price"].mean()) ** 2
            rows.append({"league": lname, "n": n, "vol": vol, "mp": p, "mo": o,
                         "bias": o - p, "se": se, "t": (o - p) / se,
                         "reliability": rel})
        dt = pd.DataFrame(rows).sort_values("vol", ascending=False)
        log("")
        log("  满足 n>=60 的联赛 = %d 个" % len(dt))
        if len(dt) < 6:
            log("  样本不足")
            continue

        log("")
        log("  %-26s %7s %14s %9s %9s %9s %7s %11s" %
            ("联赛", "样本", "成交额", "均价", "实际", "偏差", "t值", "reliability"))
        log("  " + "-" * 100)
        for _, r in dt.head(10).iterrows():
            log("  %-26s %7d %14.0f %9.4f %9.4f %+9.4f %7.2f %11.5f" %
                (r["league"][:26], r["n"], r["vol"], r["mp"], r["mo"],
                 r["bias"], r["t"], r["reliability"]))
        log("  ...")
        for _, r in dt.tail(6).iterrows():
            log("  %-26s %7d %14.0f %9.4f %9.4f %+9.4f %7.2f %11.5f" %
                (r["league"][:26], r["n"], r["vol"], r["mp"], r["mo"],
                 r["bias"], r["t"], r["reliability"]))

        log("")
        log("  --- 核心相关性检验 ---")
        for col, name in (("bias", "有符号偏差"), ("reliability", "错价强度(reliability)")):
            x = dt["vol"].to_numpy(float)
            y = dt[col].abs().to_numpy(float) if col == "bias" else dt[col].to_numpy(float)
            rho, pv = stats.spearmanr(x, y)
            log("   Spearman(联赛成交额, |%s|) = %+.3f  p=%.4f  n=%d  %s" %
                (name, rho, pv, len(dt),
                 "显著" if pv < 0.05 else "不显著"))
            log("      方向: %s" % ("冷门错价更大 [假设成立]" if rho < 0 else "冷门错价更小 [假设不成立]"))

        # 分档比较
        log("")
        log("  --- 按成交额四分位分档 ---")
        dt2 = dt.copy()
        # ⚠️ 修正标注错误：pd.qcut 按【升序】贴标签，
        #    所以第一个标签对应【成交额最低】的那一档。
        #    之前写成 ["T1最热",...,"T4最冷"] 是反的（实测"T4最冷"的平均成交额 $1.06B，
        #    是全部四档里最高的），导致结论方向被写反。
        dt2["tier"] = pd.qcut(dt2["vol"], 4,
                              labels=["Q1 成交额最低", "Q2", "Q3", "Q4 成交额最高"])
        log("  %-16s %6s %14s %12s %14s" %
            ("档位", "联赛数", "平均成交额", "平均|偏差|", "平均reliability"))
        log("  " + "-" * 92)
        for t in ["Q1 成交额最低", "Q2", "Q3", "Q4 成交额最高"]:
            s = dt2[dt2["tier"] == t]
            if not len(s):
                continue
            log("  %-16s %6d %14.0f %12.5f %14.5f" %
                (t, len(s), s["vol"].mean(), s["bias"].abs().mean(),
                 s["reliability"].mean()))
        lo = dt2[dt2["tier"] == "Q1 成交额最低"]      # 真正的冷门
        hi = dt2[dt2["tier"] == "Q4 成交额最高"]      # 真正的热门
        if len(lo) and len(hi):
            log("")
            log("  冷门(Q1) vs 热门(Q4):")
            log("     平均成交额    $%.0f  vs  $%.0f" % (lo["vol"].mean(), hi["vol"].mean()))
            log("     |偏差|        %.5f  vs  %.5f   (%.2fx)" %
                (lo["bias"].abs().mean(), hi["bias"].abs().mean(),
                 lo["bias"].abs().mean() / max(hi["bias"].abs().mean(), 1e-9)))
            log("     reliability   %.5f  vs  %.5f   (%.2fx)" %
                (lo["reliability"].mean(), hi["reliability"].mean(),
                 lo["reliability"].mean() / max(hi["reliability"].mean(), 1e-9)))
            for col, nm in (("bias", "|偏差|"), ("reliability", "reliability")):
                a = lo[col].abs() if col == "bias" else lo[col]
                b = hi[col].abs() if col == "bias" else hi[col]
                try:
                    u, pu = stats.mannwhitneyu(a, b, alternative="greater")
                    log("     Mann-Whitney（冷门 > 热门）%s: p = %.4f  %s" %
                        (nm, pu, "显著" if pu < 0.05 else "不显著"))
                except Exception as e:
                    log("     Mann-Whitney %s 失败: %s" % (nm, str(e)[:50]))
        dt.to_csv(r"D:\26050\Documents\quant_system\output_poly_league3_%dd.csv" % LEAD,
                  index=False)
        log("")

    with open(r"D:\26050\Documents\quant_system\output_poly_league3.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("已写出 output_poly_league3.txt")


if __name__ == "__main__":
    main()
