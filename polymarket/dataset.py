# -*- coding: utf-8 -*-
"""
把原始数据 → 建模表（支持过滤）

每行 = (一个已结算市场, 一个观察时点)
核心字段:
    price        观察时点 outcome[0] 的隐含概率
    outcome      outcome[0] 最终是否胜出 (0/1)
    volume       该市场累计成交额
    n_points     价格序列点数（反映市场活跃度）
    lead_days    距结算天数
    drift        final_price - price

用法:
    python build_dataset.py                          # 全量
    python build_dataset.py --min-volume 10000       # 只保留成交额 >= 1万
    python build_dataset.py --out data/dataset_tradable.csv --min-volume 10000 --min-points 20
"""
import argparse, csv, datetime as dt, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import Counter

from config import paths as _pm_paths
_P = _pm_paths()
ROOT = os.path.dirname(os.path.abspath(__file__))
D = _P["data"]
D_MK = _P["markets"]
D_PX = os.path.join(D, "prices")

LEADS = [30, 21, 14, 7, 3, 1]


def log(m):
    print(str(m).encode("ascii", "replace").decode(), flush=True)


def num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def parse_ts(s):
    if not s:
        return None
    s = str(s)
    for f in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S%z",
              "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(s, f).replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception:
            pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_markets(wanted=None):
    """只加载 wanted 里的 market_id（wanted=None 表示全部）。
    手动流式解析，避免把上百万条全读进内存。"""
    out = {}
    if not os.path.isdir(D_MK):
        return out
    for f in os.listdir(D_MK):
        if not f.endswith(".json"):
            continue
        p = os.path.join(D_MK, f)
        if wanted is not None and len(out) >= len(wanted):
            break
        try:
            ms = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for m in ms:
            mid = str(m.get("market_id"))
            if wanted is None or mid in wanted:
                out[mid] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=0)
    ap.add_argument("--min-points", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(D, "dataset.csv"))
    a = ap.parse_args()

    # 先列出价格文件，只加载这些市场（避免读入全部上百万条）
    pxfiles = sorted(os.listdir(D_PX)) if os.path.isdir(D_PX) else []
    pxrecs, wanted = [], set()
    for fn in pxfiles:
        try:
            rec = json.load(open(os.path.join(D_PX, fn), encoding="utf-8"))
        except Exception:
            continue
        pxrecs.append(rec)
        wanted.add(str(rec.get("market_id")))
    log("价格文件 %d 个，需匹配的市场元数据 %d 个" % (len(pxrecs), len(wanted)))
    mk = load_markets(wanted)
    log("匹配到市场元数据 %d 个" % len(mk))

    rows, skip = [], Counter()
    for rec in pxrecs:
        mid = str(rec.get("market_id"))
        m = mk.get(mid)
        if not m:
            skip["no_meta"] += 1; continue

        vol = num(m.get("volume"))
        if vol < a.min_volume:
            skip["low_volume"] += 1; continue

        op = m.get("outcome_prices")
        if isinstance(op, str):
            try: op = json.loads(op)
            except Exception: op = None
        if not op or len(op) < 2:
            skip["no_outcome"] += 1; continue
        try:
            outcome = 1 if float(op[0]) >= 0.5 else 0
        except Exception:
            skip["no_outcome"] += 1; continue

        end = parse_ts(m.get("end_date")) or parse_ts(m.get("closed_time"))
        if not end:
            skip["no_end"] += 1; continue

        # 兼容两种格式：新格式 history=[{t,p}]，旧格式 series={tok0:[...]}
        ser = rec.get("history")
        if ser is None:
            ser = (rec.get("series") or {}).get("tok0") or []
        ser = sorted({(int(p["t"]), float(p["p"])) for p in ser if "t" in p and "p" in p})
        if len(ser) < a.min_points:
            skip["low_points"] += 1; continue

        final_price = ser[-1][1]
        n_points = len(ser)

        for lead in LEADS:
            target = end - lead * 86400
            cand = [p for t, p in ser if t <= target]
            if not cand:
                continue
            price = cand[-1]
            rows.append({
                "market_id": mid,
                "league": m.get("league") or "",
                "league_name": (m.get("league_name") or "")[:40],
                "question": (m.get("question") or "")[:110],
                "sports_market_type": m.get("sports_market_type") or "",
                "neg_risk": 1 if m.get("neg_risk") else 0,
                "volume": round(vol, 1),
                "n_points": n_points,
                "lead_days": lead,
                "price": round(price, 4),
                "outcome": outcome,
                "final_price": round(final_price, 4),
                "drift": round(final_price - price, 4),
            })

    if not rows:
        log("无数据。跳过原因: %s" % dict(skip)); return

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    log("跳过: %s" % dict(skip))
    log("写出 %d 行 -> %s" % (len(rows), a.out))
    log("独立市场数: %d" % len({r["market_id"] for r in rows}))
    log("按 lead_days: %s" % dict(sorted(Counter(r["lead_days"] for r in rows).items(), reverse=True)))
    log("成交量分位: ")
    vs = sorted({r["volume"] for r in rows})
    for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        log("   %4.0f%%  %12.1f" % (q * 100, vs[int(q * (len(vs) - 1))]))


if __name__ == "__main__":
    main()
