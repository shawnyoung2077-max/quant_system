"""
scan.py - 扫描 Polymarket，记录观测，并按规则生成纸面下注
============================================================================
流程：
  1. 抓所有体育联赛的未结算市场
  2. 过滤：允许的市场类型 / 有盘口 / 价差够小 / 价格在目标档 / 距结算合适
  3. 计算"冷门联赛"标记
  4. 写入 observations（全部合格市场，不论是否下注）
  5. 按规则选一部分写入 bets（纸面下注）
"""

import io
import json
import sys
import time
import datetime as dt

import requests
import pandas as pd

from . import config as C
from . import db as D


def _get(url, params=None, tries=None, timeout=None):
    tries = tries or C.HTTP_RETRIES
    timeout = timeout or C.HTTP_TIMEOUT
    last = ""
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(2 + 2 * i)
                continue
            return {"__err__": r.status_code}
        except Exception as e:
            last = str(e)[:120]
            if i == tries - 1:
                return {"__err__": last}
            time.sleep(1.5 + i)
    return None


def fetch_all_markets(verbose=True):
    """按 sports catalog 的 primaryTagId 抓所有未结算体育市场。"""
    cat = json.load(io.open(C.SPORTS_CATALOG, encoding="utf-8"))
    tags = []
    for c in cat:
        t = c.get("primaryTagId")
        if t:
            tags.append((t, c.get("name") or c.get("sport") or str(t)))
    tags = tags[:C.MAX_LEAGUE_TAGS]

    seen = {}
    for i, (t, nm) in enumerate(tags):
        j = _get(C.GAMMA + "/markets",
                 {"limit": C.MARKETS_PER_TAG, "closed": "false", "tag_id": t})
        if isinstance(j, list):
            for m in j:
                k = m.get("id") or m.get("conditionId")
                if k and k not in seen:
                    m["__league_name"] = nm
                    seen[k] = m
        if verbose and (i + 1) % 50 == 0:
            print("  [scan] tag %d/%d, 累计 %d" % (i + 1, len(tags), len(seen)),
                  flush=True)
    return list(seen.values())


def to_frame(markets):
    now = dt.datetime.now(dt.timezone.utc)
    recs = []
    for m in markets:
        try:
            bid = float(m["bestBid"]) if m.get("bestBid") not in (None, "") else None
            ask = float(m["bestAsk"]) if m.get("bestAsk") not in (None, "") else None
            ed = m.get("endDate")
            end = pd.to_datetime(ed, errors="coerce", utc=True) if ed else pd.NaT
            d2e = (end - now).total_seconds() / 86400 if pd.notna(end) else None
            recs.append({
                "market_id": m.get("id") or m.get("conditionId"),
                "slug": m.get("slug") or "",
                "question": m.get("question") or "",
                "league": m.get("__league_name") or "",
                "market_type": m.get("sportsMarketType") or "",
                "group_title": m.get("groupItemTitle") or "",
                "bid": bid, "ask": ask,
                "mid": ((bid + ask) / 2) if (bid is not None and ask is not None) else None,
                "spread": (ask - bid) if (bid is not None and ask is not None) else None,
                "days_to_end": d2e,
                "end_date": end.isoformat() if pd.notna(end) else None,
                "vol24": float(m.get("volume24hr") or 0),
                "volnum": float(m.get("volumeNum") or 0),
                "liq": float(m.get("liquidityNum") or 0),
                "neg_risk": 1 if m.get("negRisk") else 0,
                "fee_type": m.get("feeType") or "",
            })
        except Exception:
            continue
    df = pd.DataFrame(recs)
    return df


def mark_cold(df):
    """按联赛累计成交额分位标记冷门。这是用户假设的核心变量。"""
    if df.empty:
        df["is_cold"] = []
        return df, set()
    lg = df.groupby("league")["volnum"].sum()
    thr = lg.quantile(C.COLD_QUANTILE)
    cold = set(lg[lg <= thr].index)
    df["is_cold"] = df["league"].isin(cold).astype(int)
    return df, cold


def filter_candidates(df, verbose=False, wide=False):
    """
    筛选。wide=True → 观测口径（宽，记录所有类型/价格档）
             wide=False → 下注口径（窄，只留明确规则的子集）
    verbose 时打印漏斗。
    """
    if df.empty:
        return df
    m = df.copy()
    lo = C.OBS_PRICE_LO if wide else C.BET_PRICE_LO
    hi = C.OBS_PRICE_HI if wide else C.BET_PRICE_HI
    msp = C.OBS_MAX_SPREAD if wide else C.BET_MAX_SPREAD
    types = C.OBS_TYPES if wide else C.BET_TYPES

    steps = [("全部抓到的市场", len(m))]
    m = m[m["mid"].notna()]
    steps.append(("有盘口 (mid 非空)", len(m)))
    m = m[m["spread"].notna() & (m["spread"] <= msp)]
    steps.append(("价差 <= %.2f" % msp, len(m)))
    if types is not None:
        m = m[m["market_type"].isin(types)]
        steps.append(("市场类型合格", len(m)))
    else:
        steps.append(("市场类型：不筛（全部记录）", len(m)))
    m = m[m["days_to_end"].notna()]
    steps.append(("有结算时间", len(m)))
    m = m[(m["days_to_end"] >= C.MIN_DAYS) & (m["days_to_end"] <= C.MAX_DAYS)]
    steps.append(("距结算 %.2f~%.0f 天" % (C.MIN_DAYS, C.MAX_DAYS), len(m)))
    m = m[(m["mid"] >= lo) & (m["mid"] < hi)]
    steps.append(("价格档 %.2f-%.2f" % (lo, hi), len(m)))
    m = m[m["vol24"] >= C.MIN_VOL24]
    steps.append(("24h成交额 >= %.0f" % C.MIN_VOL24, len(m)))
    if verbose:
        print("  [funnel] %s口径筛选：" % ("观测" if wide else "下注"), flush=True)
        prev = None
        for nm, n in steps:
            drop = "" if prev is None else "  (剔除 %d)" % (prev - n)
            print("      %-40s %6d%s" % (nm, n, drop), flush=True)
            prev = n
    return m


def entry_price(df_row, side):
    """
    实际成交价（含跨越价差）：
      买 YES → 付 YES 卖一 = ask
      买 NO  → 付 NO 卖一 = 1 - YES 买一 = 1 - bid
    """
    if side == "YES":
        return float(df_row["ask"])
    return 1.0 - float(df_row["bid"])


def calc_fee(price, shares, fee_rate=None):
    """官方公式 fee = C x feeRate x p x (1-p)。Maker 为 0（本系统按 Taker 计）。"""
    if not C.USE_OFFICIAL_FEE:
        return 0.0
    r = C.SPORTS_FEE_RATE if fee_rate is None else fee_rate
    return float(shares * r * price * (1.0 - price))


def run_scan(verbose=True, dry=False):
    """主扫描：抓 → 筛 → 写观测 → 建纸面下注。返回统计 dict。"""
    conn = D.connect()
    ts = D.now_iso()
    today = D.today()

    markets = fetch_all_markets(verbose=verbose)
    df = to_frame(markets)
    if verbose:
        print("  [scan] 抓到 %d 个市场" % len(df), flush=True)
    df, cold = mark_cold(df)
    # ★ 观测口径【宽收】：记录所有价格档（日后任何假设都能在同一份数据上检验）
    obs_cand = filter_candidates(df, verbose=verbose, wide=True)
    # 下注口径【窄】：只对明确规则的子集真下注
    bet_cand = filter_candidates(df, verbose=verbose, wide=False)
    cand = obs_cand
    if verbose:
        print("  [scan] 观测候选 %d 个 / 下注候选 %d 个（冷门联赛 %d 个）"
              % (len(obs_cand), len(bet_cand), len(cold)), flush=True)

    n_obs = 0
    for _, r in cand.iterrows():
        D.upsert_observation(conn, {
            "snap_date": today, "snap_ts": ts,
            "market_id": r["market_id"], "slug": r["slug"], "question": r["question"],
            "league": r["league"], "market_type": r["market_type"],
            "group_title": r["group_title"], "bid": r["bid"], "ask": r["ask"],
            "mid": r["mid"], "spread": r["spread"], "days_to_end": r["days_to_end"],
            "end_date": r["end_date"], "vol24": r["vol24"], "volnum": r["volnum"],
            "liq": r["liq"], "is_cold": int(r["is_cold"]),
            "ref_fair": None, "ref_source": None,
            "neg_risk": int(r["neg_risk"]), "fee_type": r["fee_type"],
        })
        n_obs += 1
    conn.commit()

    # ---- 纸面下注 ----
    n_bets = 0
    if not dry:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM bets WHERE status='open'").fetchone()["c"]
        room = max(0, C.MAX_OPEN_BETS - open_n)
        # 优先冷门联赛；同联赛内按 24h 成交额降序（先做流动性好的）
        pick = bet_cand[bet_cand["is_cold"] == 1].sort_values("vol24", ascending=False)
        pick = pick.head(min(C.MAX_NEW_BETS_PER_RUN, room))
        for _, r in pick.iterrows():
            ex = conn.execute("SELECT 1 FROM bets WHERE market_id=?",
                              (r["market_id"],)).fetchone()
            if ex:
                continue
            side = C.DEFAULT_SIDE
            px = entry_price(r, side)
            if not (0.0 < px < 1.0):
                continue
            shares = C.STAKE / px
            fee = calc_fee(px, shares)
            conn.execute(
                "INSERT INTO bets(market_id,league,question,side,entry_price,entry_mid,"
                "shares,stake,entry_ts,entry_date,days_to_end,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'open')",
                (r["market_id"], r["league"], r["question"], side, px, r["mid"],
                 shares, C.STAKE, ts, today, r["days_to_end"]))
            n_bets += 1
        conn.commit()

    D.log_run(conn, "scan", n_scanned=len(df), n_new_obs=n_obs, n_new_bets=n_bets)
    res = {"scanned": len(df), "obs_candidates": len(obs_cand),
           "bet_candidates": len(bet_cand), "n_cold_leagues": len(cold),
           "n_obs": n_obs, "n_bets": n_bets}
    conn.close()
    return res


if __name__ == "__main__":
    print(run_scan())
