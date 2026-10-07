# -*- coding: utf-8 -*-
"""
Polymarket 体育市场数据采集库

    catalog  : 473 个联赛/赛事目录（含 primaryTagId）
    markets  : 按联赛拉已结算市场（含结果 outcomePrices）
    prices   : 按 clobTokenId 拉日频历史价格

用法:
    python pm_sports.py catalog
    python pm_sports.py markets --leagues nba nfl mlb epl ucl atp ufc
    python pm_sports.py prices --limit 800
"""
import argparse, datetime as dt, json, os, time, sys
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"; os.environ["no_proxy"] = "*"

from config import GAMMA, HEADERS as H
CLOB = "https://clob.polymarket.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "application/json"}

ROOT = os.path.dirname(os.path.abspath(__file__))
D_DATA = os.path.join(ROOT, "data")
D_MK = os.path.join(D_DATA, "markets")
D_PX = os.path.join(D_DATA, "prices")

# 优先采集的联赛（流动性好、市场多）
PRIORITY_LEAGUES = [
    "atp", "wta",                       # 网球：市场最多
    "nba", "nfl", "mlb", "nhl", "wnba", "ncaab", "cfb",
    "epl", "ucl", "lal", "mls",         # 足球
    "ufc", "boxing",                    # 格斗
    "f1", "nascar",                     # 赛车
    "cs2", "lol", "dota2",              # 电竞
    "ipl", "t20", "bbl",                # 板球
]


def log(m):
    print(str(m).encode("ascii", "replace").decode(), flush=True)


def num(v, default=0.0):
    """把可能是字符串的数值安全转成 float"""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def get(url, timeout=50, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(2 + i * 2)
    return None


def ensure_dirs():
    for d in (D_DATA, D_MK, D_PX):
        os.makedirs(d, exist_ok=True)


# ---------------- catalog ----------------
def fetch_catalog():
    ensure_dirs()
    sp = get(GAMMA + "/sports")
    if not isinstance(sp, list):
        log("catalog 拉取失败"); return
    out = os.path.join(D_DATA, "sports_catalog.json")
    json.dump(sp, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("catalog: %d 个联赛 -> %s" % (len(sp), out))


def load_catalog():
    p = os.path.join(D_DATA, "sports_catalog.json")
    if not os.path.exists(p):
        return {}
    return {s.get("sport"): s for s in json.load(open(p, encoding="utf-8"))}


# ---------------- markets ----------------
def fetch_markets(leagues, pages=6, page_size=100):
    """按联赛拉已结算市场。events?tag_id=<primaryTagId>&closed=true"""
    ensure_dirs()
    cat = load_catalog()
    if not cat:
        log("请先跑 catalog"); return
    grand = 0
    for key in leagues:
        s = cat.get(key)
        if not s:
            log("  [%-8s] 不在目录中，跳过" % key); continue
        tid = s.get("primaryTagId")
        rows, seen = [], set()
        for pg in range(pages):
            ev = get("%s/events?tag_id=%s&closed=true&limit=%d&offset=%d"
                     % (GAMMA, tid, page_size, pg * page_size))
            if not isinstance(ev, list) or not ev:
                break
            for e in ev:
                for m in (e.get("markets") or []):
                    mid = m.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    if not m.get("closed"):
                        continue
                    if not m.get("outcomePrices") or not m.get("clobTokenIds"):
                        continue
                    rows.append({
                        "market_id": mid,
                        "question": m.get("question"),
                        "slug": m.get("slug"),
                        "event_id": e.get("id"),
                        "event_title": e.get("title"),
                        "league": key,
                        "league_name": s.get("name"),
                        "sports_market_type": m.get("sportsMarketType"),
                        "outcomes": m.get("outcomes"),
                        "outcome_prices": m.get("outcomePrices"),
                        "clob_token_ids": m.get("clobTokenIds"),
                        "volume": num(m.get("volumeNum") or m.get("volume")),
                        "liquidity": num(m.get("liquidityNum") or m.get("liquidity")),
                        "start_date": m.get("startDate"),
                        "end_date": m.get("endDate"),
                        "game_start_time": m.get("gameStartTime"),
                        "closed_time": m.get("closedTime"),
                        "neg_risk": m.get("negRisk"),
                        "best_bid": m.get("bestBid"),
                        "best_ask": m.get("bestAsk"),
                        "last_trade_price": m.get("lastTradePrice"),
                        "spread": m.get("spread"),
                        "one_day_change": m.get("oneDayPriceChange"),
                        "one_week_change": m.get("oneWeekPriceChange"),
                        "resolution_source": m.get("resolutionSource"),
                    })
            time.sleep(0.3)
        if rows:
            rows.sort(key=lambda r: -num(r.get("volume")))
            p = os.path.join(D_MK, "%s.json" % key)
            json.dump(rows, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            grand += len(rows)
            log("  [%-8s] %4d 个已结算市场  成交量合计 %.0f"
                % (key, len(rows), sum(num(r.get("volume")) for r in rows)))
        else:
            log("  [%-8s] 无数据" % key)
    log("markets 合计: %d" % grand)


def load_all_markets():
    if not os.path.isdir(D_MK):
        return []
    out = []
    for f in sorted(os.listdir(D_MK)):
        if f.endswith(".json"):
            out += json.load(open(os.path.join(D_MK, f), encoding="utf-8"))
    return out


# ---------------- prices ----------------
def fetch_prices(limit=800, min_volume=1000, fidelity=1440):
    """按 clobTokenId 拉日频历史价格（interval=max）"""
    ensure_dirs()
    mk = load_all_markets()
    mk = [m for m in mk if num(m.get("volume")) >= min_volume]
    mk.sort(key=lambda m: -num(m.get("volume")))
    mk = mk[:limit]
    log("待拉价格的市场: %d（volume>=%d，取前 %d）" % (len(mk), min_volume, limit))

    done = 0
    for i, m in enumerate(mk, 1):
        mid = str(m["market_id"])
        outp = os.path.join(D_PX, "%s.json" % mid)
        if os.path.exists(outp):
            done += 1
            continue
        ids = m.get("clob_token_ids")
        if isinstance(ids, str):
            try: ids = json.loads(ids)
            except Exception: ids = None
        if not ids:
            continue
        rec = {"market_id": mid, "question": m.get("question"),
               "league": m.get("league"), "outcomes": m.get("outcomes"),
               "outcome_prices": m.get("outcome_prices"),
               "end_date": m.get("end_date"), "volume": m.get("volume"),
               "series": {}}
        ok = False
        for ti, tok in enumerate(ids[:2]):
            j = get("%s/prices-history?market=%s&interval=max&fidelity=%d"
                    % (CLOB, tok, fidelity))
            h = (j or {}).get("history") or []
            rec["series"]["tok%d" % ti] = h
            if h:
                ok = True
            time.sleep(0.25)
        if ok:
            json.dump(rec, open(outp, "w", encoding="utf-8"), ensure_ascii=False)
            done += 1
        if i % 25 == 0:
            log("  [%d/%d] 已完成 %d" % (i, len(mk), done))
    log("prices 完成: %d 个市场有价格序列" % done)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["catalog", "markets", "prices", "status"])
    ap.add_argument("--leagues", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--min-volume", type=float, default=1000)
    ap.add_argument("--pages", type=int, default=6)
    a = ap.parse_args()
    ensure_dirs()
    if a.cmd == "catalog":
        fetch_catalog()
    elif a.cmd == "markets":
        fetch_markets(a.leagues or PRIORITY_LEAGUES, pages=a.pages)
    elif a.cmd == "prices":
        fetch_prices(limit=a.limit, min_volume=a.min_volume)
    else:
        mk = load_all_markets()
        px = len(os.listdir(D_PX)) if os.path.isdir(D_PX) else 0
        log("市场: %d  价格文件: %d" % (len(mk), px))
        from collections import Counter
        c = Counter(m.get("league") for m in mk)
        for k, v in c.most_common():
            log("  %-10s %d" % (k, v))


if __name__ == "__main__":
    main()
