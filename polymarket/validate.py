# -*- coding: utf-8 -*-
"""干净对照：只保留所有时间窗口都有价格的同一批市场，消除样本构成偏差"""
import csv, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import defaultdict

from config import paths as _pm_paths
_P = _pm_paths()
ROOT = os.path.dirname(os.path.abspath(__file__))
DS = _P["dataset_all"]

rows = []
with open(DS, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        r["price"] = float(r["price"]); r["outcome"] = int(r["outcome"])
        r["volume"] = float(r["volume"] or 0); r["lead_days"] = int(r["lead_days"])
        r["n_points"] = int(r["n_points"]); r["neg_risk"] = int(r["neg_risk"] or 0)
        rows.append(r)

lead_set = sorted({r["lead_days"] for r in rows}, reverse=True)
print("总行数 %d，观察时点 %s" % (len(rows), lead_set))
print()

# 找出在所有 lead 都有数据的市场
by_mid = defaultdict(dict)
for r in rows:
    by_mid[r["market_id"]][r["lead_days"]] = r
common = {m: d for m, d in by_mid.items() if all(l in d for l in lead_set)}
print("在所有 %d 个时点都有数据的市场: %d 个（原 %d 个）"
      % (len(lead_set), len(common), len(by_mid)))
print()

def bucket(p, w=0.05):
    return round(min(int(p / w) * w, 0.95), 2)

def report(sel, label):
    if len(sel) < 30:
        print("  %s: 样本不足 (%d)" % (label, len(sel))); return
    n = len(sel)
    mp = sum(r["price"] for r in sel) / n
    mo = sum(r["outcome"] for r in sel) / n
    print("  %-14s n=%-6d 均价 %.3f  实际 %.3f  偏差 %+.4f" % (label, n, mp, mo, mo - mp))

# ============ 1) 同一批市场，各时点的整体偏差 ============
print("=" * 88)
print("### 1) 同一批市场（%d 个）在各时点的整体偏差" % len(common))
print("=" * 88)
for lead in lead_set:
    report([d[lead] for d in common.values()], "结算前 %d 天" % lead)
print()

# ============ 2) 只看简单二元 moneyline ============
print("=" * 88)
print("### 2) 只看 negRisk=0 且 moneyline 的干净二元市场")
print("=" * 88)
clean = {m: d for m, d in common.items()
         if all(d[l]["neg_risk"] == 0 and
                (d[l]["sports_market_type"] or "") == "moneyline" for l in lead_set)}
print("  符合条件的市场: %d 个" % len(clean))
for lead in lead_set:
    report([d[lead] for d in clean.values()], "结算前 %d 天" % lead)
print()

# ============ 3) 分档校准（同一批市场，含每档样本数） ============
for lead in (7, 3, 1):
    sel = [d[lead] for d in common.values()]
    print("=" * 88)
    print("### 3) 分档校准 · 结算前 %d 天 · 同一批市场 (n=%d)" % (lead, len(sel)))
    print("=" * 88)
    print("  %-10s %7s %10s %11s %10s" % ("价格区间", "样本", "平均价格", "实际胜率", "偏差"))
    g = defaultdict(list)
    for r in sel:
        g[bucket(r["price"])].append(r)
    for b in sorted(g):
        rs = g[b]; n = len(rs)
        mp = sum(r["price"] for r in rs) / n
        mo = sum(r["outcome"] for r in rs) / n
        flag = "  <- 样本少" if n < 100 else ""
        print("  %-10s %7d %10.3f %11.3f %+10.4f%s" %
              ("%.2f-%.2f" % (b, b + 0.05), n, mp, mo, mo - mp, flag))
    print()

# ============ 4) 成交额分层（同一批市场，lead=7） ============
print("=" * 88)
print("### 4) 成交额分层 · 结算前 7 天 · 同一批市场")
print("=" * 88)
sel = [d[7] for d in common.values()]
for lo, hi in [(0,2e5),(2e5,5e5),(5e5,1e6),(1e6,5e6),(5e6,1e12)]:
    sub = [r for r in sel if lo <= r["volume"] < hi]
    n = len(sub)
    if n < 20:
        print("  $%.0fK-%.0fK  样本不足 (%d)" % (lo/1e3, hi/1e3 if hi < 1e12 else 0, n)); continue
    mp = sum(r["price"] for r in sub) / n
    mo = sum(r["outcome"] for r in sub) / n
    # 只看冷门段
    lon = [r for r in sub if r["price"] < 0.20]
    if lon:
        lp = sum(r["price"] for r in lon) / len(lon)
        lo_ = sum(r["outcome"] for r in lon) / len(lon)
        print("  $%-8.0fK-%-8.0fK  n=%-5d 全档偏差 %+.4f | 冷门档(<0.20) n=%-5d 偏差 %+.4f"
              % (lo/1e3, hi/1e3 if hi < 1e12 else 0, n, mo - mp, len(lon), lo_ - lp))
    else:
        print("  $%-8.0fK-%-8.0fK  n=%-5d 全档偏差 %+.4f | 冷门档无样本"
              % (lo/1e3, hi/1e3 if hi < 1e12 else 0, n, mo - mp))
