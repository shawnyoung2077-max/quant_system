# -*- coding: utf-8 -*-
"""
校准分析：检验「冷门高估 / 热门低估」

核心逻辑:
    买入 YES 的期望值 = 实际胜率 - 价格
    买入 NO  的期望值 = 价格 - 实际胜率
    所以只要某价格区间的「实际胜率」系统性偏离「价格」，就存在可套利结构。

用法:
    python analyze.py                # 全样本
    python analyze.py --lead 7       # 只看结算前 7 天
    python analyze.py --min-vol 10000
"""
import argparse, csv, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import defaultdict

from config import paths as _pm_paths
_P = _pm_paths()
ROOT = os.path.dirname(os.path.abspath(__file__))
D = _P["data"]

# 自动挑选可用的数据集（优先级：全量 > 可交易 > 旧版）
_CAND = ["dataset_all.csv", "dataset_tradable.csv", "dataset.csv"]
DS = None
for _c in _CAND:
    _p = os.path.join(D, _c)
    if os.path.exists(_p):
        DS = _p
        break
if DS is None:
    DS = os.path.join(D, "dataset_all.csv")


def load(lead=None, min_vol=0, league=None):
    rows = []
    with open(DS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["price"] = float(r["price"])
            r["outcome"] = int(r["outcome"])
            r["volume"] = float(r["volume"] or 0)
            r["lead_days"] = int(r["lead_days"])
            if lead is not None and r["lead_days"] != lead:
                continue
            if r["volume"] < min_vol:
                continue
            if league and r["league"] != league:
                continue
            rows.append(r)
    return rows


def bucket_of(p, w=0.05):
    b = int(p / w) * w
    return round(min(b, 0.95), 2)


def report(rows, title):
    print("=" * 84)
    print("### %s   (n=%d)" % (title, len(rows)))
    print("=" * 84)
    if not rows:
        print("  无数据\n"); return
    print("  %-10s %6s %10s %11s %10s %10s" %
          ("价格区间", "样本", "平均价格", "实际胜率", "偏差", "每$1期望"))
    print("  " + "-" * 70)

    g = defaultdict(list)
    for r in rows:
        g[bucket_of(r["price"])].append(r)

    tot_ev = 0.0
    for b in sorted(g):
        rs = g[b]
        n = len(rs)
        mp = sum(r["price"] for r in rs) / n
        rate = sum(r["outcome"] for r in rs) / n
        edge = rate - mp
        side = "买YES" if edge > 0 else "买NO"
        print("  %-10s %6d %10.3f %11.3f %10.3f %8.3f %s" %
              ("%.2f-%.2f" % (b, b + 0.05), n, mp, rate, edge, abs(edge), side))
        tot_ev += abs(edge) * n
    print("  " + "-" * 70)
    print("  加权平均绝对偏差: %.4f" %
          (tot_ev / len(rows) if rows else 0))

    # 整体指标
    brier = sum((r["price"] - r["outcome"]) ** 2 for r in rows) / len(rows)
    eps = 1e-9
    ll = -sum(r["outcome"] * math.log(max(r["price"], eps)) +
              (1 - r["outcome"]) * math.log(max(1 - r["price"], eps)) for r in rows) / len(rows)
    print("  Brier %.4f   对数损失 %.4f   基准(价格均值) %.4f" %
          (brier, ll, sum(r["price"] for r in rows) / len(rows)))
    print()


def strategy(rows, lo, hi, side):
    """在 [lo,hi) 价格区间买 side，统计"""
    sel = [r for r in rows if lo <= r["price"] < hi]
    if not sel:
        return None
    n = len(sel)
    mp = sum(r["price"] for r in sel) / n
    rate = sum(r["outcome"] for r in sel) / n
    if side == "YES":
        ev = rate - mp
    else:
        ev = (1 - rate) - (1 - mp)
    return n, mp, rate, ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead", type=int, default=None)
    ap.add_argument("--min-vol", type=float, default=0)
    ap.add_argument("--league", default=None)
    a = ap.parse_args()
    if not os.path.exists(DS):
        print("找不到数据集，请先运行 build_dataset.py")
        print("  已尝试: %s" % [os.path.join("data", c) for c in _CAND])
        return
    print("使用数据集: %s" % os.path.basename(DS))

    print()
    print("#" * 84)
    print("# Polymarket 体育市场 · 校准分析")
    print("#" * 84)
    print()

    allrows = load()
    print("数据集总行数: %d" % len(allrows))
    print()

    # 1) 各观察时点
    for lead in sorted(set(r["lead_days"] for r in allrows), reverse=True):
        report(load(lead=lead, min_vol=a.min_vol), "结算前 %d 天" % lead)

    # 2) 分联赛（用 lead=7）
    rows7 = load(lead=7, min_vol=1000)
    if rows7:
        bylg = defaultdict(list)
        for r in rows7:
            bylg[r["league"]].append(r)
        print("=" * 84)
        print("### 分联赛校准（结算前 7 天，volume>=1000）")
        print("=" * 84)
        print("  %-8s %6s %10s %11s %10s" % ("联赛", "样本", "平均价格", "实际胜率", "偏差"))
        print("  " + "-" * 60)
        for lg, rs in sorted(bylg.items(), key=lambda x: -len(x[1]))[:15]:
            n = len(rs)
            mp = sum(r["price"] for r in rs) / n
            rate = sum(r["outcome"] for r in rs) / n
            print("  %-8s %6d %10.3f %11.3f %10.3f" % (lg, n, mp, rate, rate - mp))
        print()

    # 2.5) ⭐ 按成交额分层：偏差到底在哪种市场里
    if allrows:
        print("=" * 84)
        print("### ⭐ 成交额分层：偏差集中在哪种市场？（结算前 7 天）")
        print("=" * 84)
        base = load(lead=7)
        if base:
            vols = sorted(r["volume"] for r in base)
            cuts = [vols[int(q * (len(vols) - 1))] for q in (0.2, 0.4, 0.6, 0.8)]
            cuts = sorted(set(cuts))
            print("  样本 %d，成交量分位切点: %s" % (len(base), [round(c) for c in cuts]))
            print()
            print("  %-22s %7s %11s %11s %10s" %
                  ("成交额区间", "样本", "均价<0.15", "实际胜率", "偏差"))
            print("  " + "-" * 66)
            bands = []
            prev = -1
            for c in cuts + [float("inf")]:
                bands.append((prev, c)); prev = c
            for lo, hi in bands:
                sel = [r for r in base if lo < r["volume"] <= hi]
                if lo < 0:
                    sel = [r for r in base if r["volume"] <= hi]
                # 只看冷门段（检验 favorite-longshot 最敏感的地方）
                lon = [r for r in sel if r["price"] < 0.15]
                name = "$%s-%s" % (round(lo) if lo > 0 else 0,
                                   round(hi) if hi != float("inf") else "+")
                if not lon:
                    print("  %-22s %7d %11s %11s %10s" % (name, len(sel), "-", "-", "-"))
                    continue
                mp = sum(r["price"] for r in lon) / len(lon)
                rate = sum(r["outcome"] for r in lon) / len(lon)
                print("  %-22s %7d %11.3f %11.3f %+10.3f" %
                      (name, len(lon), mp, rate, rate - mp))
            print()
            print("  解读: 若『偏差』在低成交额档最大、高成交额档趋近 0，")
            print("        说明这个套利机会只在没人交易的冷门市场里 —— 小资金吃不到。")
            print()

    # 3) 冷门 / 热门（lead=1，最接近结算）
    r1 = load(lead=1, min_vol=1000)
    if r1:
        print("=" * 84)
        print("### ⭐ 冷门 / 热门检验（结算前 1 天，volume>=1000）")
        print("=" * 84)
        for label, lo, hi, side in [
                ("极冷门  0.00-0.05", 0.00, 0.05, "YES"),
                ("冷门    0.05-0.15", 0.05, 0.15, "YES"),
                ("中间    0.35-0.65", 0.35, 0.65, None),
                ("热门    0.85-0.95", 0.85, 0.95, "NO"),
                ("极热门  0.95-1.00", 0.95, 1.01, "NO")]:
            res = strategy(r1, lo, hi, side or "YES")
            if not res:
                continue
            n, mp, rate, ev = res
            print("  %-18s n=%-5d 均价 %.3f  实际 %.3f  %s每$1期望 %+.4f" %
                  (label, n, mp, rate, (side + " ") if side else "      ", ev))
        print()
        print("  解读: 期望为正说明该方向可买；数字越大机会越大。")
        print()


if __name__ == "__main__":
    main()
