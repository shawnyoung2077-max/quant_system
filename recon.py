"""
recon.py - 勘察：现在有哪些可下注的冷门联赛标的？
============================================================================
建模前必须确认的 5 件事：
  1. 冷门联赛现在有开放市场吗？有哪些？
  2. 价格档分布如何？（我们要的是能被判断"贵/便宜"的市场）
  3. 市场结束时间分布？（决定下注和结算的节奏）
  4. 是二元市场吗？（三向市场在 Polymarket 拆成多个二元市场）
  5. 结算数据可得吗？
"""

import io
import json
import sys
import collections
import datetime as dt

sys.path.insert(0, r"D:\26050\Documents\quant_system")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CAT = r"D:\26050\Documents\polymarket_sports\data\sports_catalog.json"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def get(u, p=None, tries=3):
    import time
    for i in range(tries):
        try:
            r = requests.get(u, params=p, timeout=40,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(2 + 2 * i)
                continue
            return {"__err__": r.status_code}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": str(e)[:80]}
    return None


def main():
    cat = json.load(io.open(CAT, encoding="utf-8"))
    log("sports catalog: %d 个联赛" % len(cat))

    rows = []
    tags = [(c.get("primaryTagId"), c.get("name") or c.get("sport"))
            for c in cat if c.get("primaryTagId")]
    log("抓取 %d 个联赛 tag ..." % len(tags))
    for i, (t, nm) in enumerate(tags[:140]):
        j = get(GAMMA + "/markets", {"limit": 100, "closed": "false", "tag_id": t})
        if isinstance(j, list):
            for m in j:
                m["__tag"] = t
                m["__tag_name"] = nm
                rows.append(m)
        if (i + 1) % 50 == 0:
            log("  ... %d/%d, 累计 %d" % (i + 1, len(tags[:140]), len(rows)))
    log("抓到原始市场 %d 条" % len(rows))

    # 去重
    seen = {}
    for m in rows:
        k = m.get("id") or m.get("conditionId")
        if k:
            seen[k] = m
    log("去重后 %d 个市场" % len(seen))

    recs = []
    now = dt.datetime.now(dt.timezone.utc)
    for m in seen.values():
        try:
            bid = float(m.get("bestBid")) if m.get("bestBid") not in (None, "") else None
            ask = float(m.get("bestAsk")) if m.get("bestAsk") not in (None, "") else None
            ed = m.get("endDate")
            end = pd.to_datetime(ed, errors="coerce", utc=True) if ed else pd.NaT
            recs.append({
                "league": m.get("__tag_name") or "",
                "sports_type": m.get("sportsMarketType") or "",
                "q": (m.get("question") or "")[:80],
                "bid": bid, "ask": ask,
                "mid": ((bid + ask) / 2) if (bid is not None and ask is not None) else None,
                "spread": (ask - bid) if (bid is not None and ask is not None) else None,
                "vol24": float(m.get("volume24hr") or 0),
                "volnum": float(m.get("volumeNum") or 0),
                "liq": float(m.get("liquidityNum") or 0),
                "end": end,
                "days_to_end": (end - now).total_seconds() / 86400 if pd.notna(end) else None,
                "closed": m.get("closed"),
                "negRisk": m.get("negRisk"),
                "feeType": m.get("feeType"),
                "groupTitle": m.get("groupItemTitle"),
                "id": m.get("id"),
                "slug": m.get("slug"),
            })
        except Exception:
            continue
    df = pd.DataFrame(recs)
    log("有效记录 %d" % len(df))

    log("")
    log("=" * 100)
    log("1. 市场类型分布（我们要 moneyline 这类'价格=概率'的）")
    log("=" * 100)
    for k, v in df["sports_type"].value_counts().head(15).items():
        log("  %-32s %d" % (k or "(空)", v))

    log("")
    log("=" * 100)
    log("2. 价格档分布（有报价的市场）")
    log("=" * 100)
    q = df[df["mid"].notna()]
    log("  有报价: %d / %d (%.0f%%)" % (len(q), len(df), len(q) / max(len(df), 1) * 100))
    if len(q):
        bins = [0, .05, .10, .15, .20, .30, .50, .70, .90, 1.01]
        cut = pd.cut(q["mid"], bins)
        for k, v in cut.value_counts().sort_index().items():
            log("  %-16s %d" % (str(k), v))

    log("")
    log("=" * 100)
    log("3. 距结算天数分布（决定节奏）")
    log("=" * 100)
    d = df["days_to_end"].dropna()
    log("  中位=%.1f 天  均值=%.1f 最小=%.2f 最大=%.1f" %
        (d.median(), d.mean(), d.min(), d.max()))
    for lo, hi, nm in [(-1, 0.25, "24小时内"), (0.25, 1, "1天内"), (1, 3, "1-3天"),
                       (3, 7, "3-7天"), (7, 30, "7-30天"), (30, 1e9, "30天以上")]:
        log("  %-10s %5d" % (nm, int(((d >= lo) & (d < hi)).sum())))

    log("")
    log("=" * 100)
    log("4. 冷门联赛盘点（成交额低 = 机构覆盖薄 = 我们想找的）")
    log("=" * 100)
    lg = df.groupby("league").agg(
        n=("id", "count"),
        vol24=("vol24", "sum"),
        volnum=("volnum", "sum"),
        quoted=("mid", lambda s: s.notna().sum()),
    ).reset_index().sort_values("volnum", ascending=False)
    log("  %-26s %6s %8s %14s %14s" % ("联赛", "市场数", "有报价", "24h成交额", "累计成交额"))
    log("  " + "-" * 92)
    for _, r in lg.head(25).iterrows():
        log("  %-26s %6d %8d %14.0f %14.0f" %
            (str(r["league"])[:26], r["n"], r["quoted"], r["vol24"], r["volnum"]))

    log("")
    log("=" * 100)
    log("5. 目标标的：冷门联赛 + 有报价 + 近期结算")
    log("=" * 100)
    # 冷门 = 联赛累计成交额低于中位
    med = lg["volnum"].median()
    cold = set(lg.loc[lg["volnum"] < med, "league"])
    log("  冷门联赛（累计成交额 < 中位 $%.0f）: %d 个" % (med, len(cold)))
    tgt = df[(df["league"].isin(cold)) & df["mid"].notna()
             & (df["days_to_end"].notna()) & (df["days_to_end"] > 0)
             & (df["days_to_end"] < 14)]
    log("  目标候选（冷门 + 有报价 + 14天内结算）: %d 个" % len(tgt))
    if len(tgt):
        log("")
        log("  %-14s %-40s %7s %7s %7s %8s %8s" %
            ("联赛", "问题", "bid", "ask", "mid", "距结算", "24h额"))
        log("  " + "-" * 100)
        for _, r in tgt.sort_values("vol24", ascending=False).head(20).iterrows():
            log("  %-14s %-40s %7.3f %7.3f %7.3f %7.1f %8.0f" %
                (str(r["league"])[:14], r["q"][:40], r["bid"], r["ask"], r["mid"],
                 r["days_to_end"], r["vol24"]))

    # 三向市场检查
    log("")
    log("=" * 100)
    log("6. 三向市场是怎么表示的？（足球有主/平/客）")
    log("=" * 100)
    fc = df[df["league"].isin(cold) & df["mid"].notna()]
    grp = fc[fc["groupTitle"].notna()]["groupTitle"].value_counts().head(10)
    log("  groupItemTitle 取值（前10）: %s" % list(grp.index))
    # 找同一 event 下的多个市场
    ev = collections.Counter()
    for m in seen.values():
        for e in (m.get("events") or []):
            ev[e.get("id")] += 1
    multi = [k for k, v in ev.items() if v >= 3]
    log("  含 >=3 个市场的 event 数 = %d（三向市场会拆成多个二元市场）" % len(multi))

    df.to_csv(r"D:\26050\Documents\quant_system\output_recon.csv", index=False)
    with open(r"D:\26050\Documents\quant_system\output_recon.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_recon.txt / output_recon.csv")


if __name__ == "__main__":
    main()
