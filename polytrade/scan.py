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
import os
import sys
import time
import datetime as dt

import requests
import pandas as pd

from . import config as C
from . import db as D

FETCH_COVERAGE_ISSUES = []


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


def fetch_all_markets(verbose=True, workers=8):
    """
    按 sports catalog 的 primaryTagId 抓所有未结算体育市场。

    ★ 性能：实测瓶颈 100% 在这里 —— 200 个 tag 串行请求要 482 秒
      （总运行 494 秒）。改成 8 线程并发后约 60 秒。
      其余步骤（转 DataFrame / 筛选）总共只有 13 秒，不值得优化。
    """
    global FETCH_COVERAGE_ISSUES
    FETCH_COVERAGE_ISSUES = []
    cat = json.load(io.open(C.SPORTS_CATALOG, encoding="utf-8"))
    tags = []
    for c in cat:
        t = c.get("primaryTagId")
        if t:
            tags.append((t, c.get("name") or c.get("sport") or str(t)))
    # ★ MAX_LEAGUE_TAGS = 0 表示「全部」。原先写死 200，
    #   而 catalog 的顺序没有优先级含义，结果把 Premier League /
    #   Europa League / MLS / Serie A 等全截掉了（详见 config.py 注释）。
    n_all = len(tags)
    if C.MAX_LEAGUE_TAGS:
        tags = tags[:C.MAX_LEAGUE_TAGS]
    if verbose:
        print("  [scan] 联赛 tag: 抓 %d / 共 %d" % (len(tags), n_all), flush=True)

    from concurrent.futures import ThreadPoolExecutor
    import time as _time
    _t_start = _time.time()
    _budget_hit = {"v": False}

    def one(item):
        t, nm = item
        out = []
        # ★ 分页：单次请求最多只返回 100 条（limit=500 与 100 结果相同），
        #   而一个热门联赛可能有 600+ 个未结算市场。某页不足满页即到底，提前停。
        hit_page_cap = False
        failed_page = False
        skipped_by_budget = False
        for pg in range(max(1, C.MAX_PAGES_PER_TAG)):
            # ★ 墙钟预算（2026-10-10）：定时任务的 ExecutionTimeLimit 是 30 分钟，
            #   而 473 个 tag × 10 页最坏情况是 4,730 次请求。一旦代理抽风，
            #   单次请求要重试 5 次 × 20s 超时 —— 那会把任务拖到被杀。
            #   所以按墙钟截断，并且**必须上报**（写进 runs.note 交给看门狗）：
            #   静默截断正是最初那个"联赛覆盖缺陷"能藏两天的原因。
            if _time.time() - _t_start > C.FETCH_TIME_BUDGET_SEC:
                _budget_hit["v"] = True
                skipped_by_budget = True
                break
            j = _get(C.GAMMA + "/markets",
                     {"limit": C.MARKETS_PER_TAG,
                      "offset": pg * C.MARKETS_PER_TAG,
                      "closed": "false", "tag_id": t})
            if not isinstance(j, list):
                failed_page = True
                break
            if not j:
                break
            out += [(m.get("id") or m.get("conditionId"), m, nm) for m in j]
            if len(j) < C.MARKETS_PER_TAG:
                break
            if pg == max(1, C.MAX_PAGES_PER_TAG) - 1:
                hit_page_cap = True
        return out, hit_page_cap, failed_page, skipped_by_budget

    seen = {}
    done = 0
    failed_tags = []
    capped_tags = []
    skipped_tags = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for item, res in zip(tags, ex.map(one, tags)):
            markets_for_tag, hit_cap, failed_page, skipped = res
            if hit_cap:
                capped_tags.append(item[1])
            if failed_page:
                failed_tags.append(item[1])
            if skipped:
                skipped_tags.append(item[1])
            for k, m, nm in markets_for_tag:
                if k and k not in seen:
                    m["__league_name"] = nm
                    seen[k] = m
            done += 1
            if verbose and done % 50 == 0:
                print("  [scan] tag %d/%d, 累计 %d 个市场"
                      % (done, len(tags), len(seen)), flush=True)
    if skipped_tags:
        print("  [scan] ⚠ 墙钟预算 %ds 用尽，%d 个联赛被跳过（覆盖不完整）：%s"
              % (C.FETCH_TIME_BUDGET_SEC, len(skipped_tags),
                 ", ".join(skipped_tags[:12])), flush=True)
        FETCH_COVERAGE_ISSUES.append("time_budget tags_skipped=%d (%s)" %
                                     (len(skipped_tags), ", ".join(skipped_tags[:12])))
    if capped_tags and verbose:
        print("  [scan] ⚠ %d 个联赛达到 %d 页上限，仍可能有漏采：%s"
              % (len(capped_tags), C.MAX_PAGES_PER_TAG,
                 ", ".join(capped_tags[:12])), flush=True)
    if capped_tags:
        FETCH_COVERAGE_ISSUES.append("page_cap tags=%d (%s)" %
                                     (len(capped_tags), ", ".join(capped_tags[:12])))
    if failed_tags and verbose:
        print("  [scan] ⚠ %d 个联赛抓取中途遇到 API 错误，覆盖不完整：%s"
              % (len(failed_tags), ", ".join(failed_tags[:12])), flush=True)
    if failed_tags:
        FETCH_COVERAGE_ISSUES.append("api_errors tags=%d (%s)" %
                                     (len(failed_tags), ", ".join(failed_tags[:12])))
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
            # ★ 开赛时间：判"是否赛前"必须用它，不能用 end_date（结算日常滞后）
            gs = m.get("gameStartTime")
            gstart = pd.to_datetime(gs, errors="coerce", utc=True) if gs else pd.NaT
            d2e = (end - now).total_seconds() / 86400 if pd.notna(end) else None
            d2s = (gstart - now).total_seconds() / 86400 if pd.notna(gstart) else None
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
                "days_to_start": d2s,
                "end_date": end.isoformat() if pd.notna(end) else None,
                "game_start": gstart.isoformat() if pd.notna(gstart) else None,
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


def _norm_league(s):
    """联赛名归一化：实盘名来自 Polymarket tag，历史名来自数据集，写法有差异。"""
    s = str(s or "").lower()
    for ch in " .-_'":
        s = s.replace(ch, "")
    return s


_WL_CACHE = {}


def load_whitelist():
    """
    读 modeling/league_support.csv，返回 {归一化联赛名: dict}。

    字段：
        supported  统计显著（聚类 bootstrap 区间下界 > 0 且 BH q < 0.05）
        n_band     该联赛在 [0.15,0.30) 档、lead 1/3 天的独立市场数
        net_bias   费用调整后的偏差（未扣实测价差成本）

    ★ 2026-10-10：这里新增 net_bias —— 因为用户要求「限制在偏差较大的时候才交易」，
      而下注门槛必须是**偏差的量**，不能只用一个 supported 布尔。
      偏差的分辨率上限是「联赛层」：试过「联赛 x 0.03 价格细档」，
      356 个单元格里 n>=40 的只有 28 个，BH 校正后显著的一个都没有
      （见 build_edge_cells.py 的诊断输出）。
    """
    if "wl" in _WL_CACHE:
        return _WL_CACHE["wl"]
    wl = {}
    try:
        import csv as _csv
        if os.path.exists(C.WHITELIST_PATH):
            with io.open(C.WHITELIST_PATH, encoding="utf-8") as fh:
                for row in _csv.DictReader(fh):
                    def _f(key):
                        try:
                            v = row.get(key)
                            return float(v) if v not in (None, "") else None
                        except Exception:
                            return None
                    try:
                        nb = int(row.get("n_band") or 0)
                    except Exception:
                        nb = 0
                    wl[_norm_league(row.get("league_name"))] = {
                        "supported": int(row.get("supported") or 0),
                        "n_band": nb,
                        "net_bias": _f("bias"),
                        "q_bh": _f("q_bh"),
                    }
    except Exception as e:
        print("  [scan] 白名单读取失败（全部按 explore 处理）：%r" % e, flush=True)
    _WL_CACHE["wl"] = wl
    return wl


def league_edge(league, wl=None):
    """
    返回该联赛的**净偏差**（已扣手续费与实测价差成本）；
    **若该联赛没有统计显著的历史证据，返回 None** —— 意思是"估不出来"。

    ★ 2026-10-10 修的一个自己写的坑：
      第一版只比较 `net_bias >= MIN_EDGE_PP`，忘了同时要求 supported=1。
      结果 n_band=1 的联赛（只有一个历史市场）会被判成 valid ——
      比如 "法国超级杯" 单个市场算出 +79.7pp 的"偏差"，
      那是噪声，不是证据。**在不支持的分辨率上设门槛，等于编一个数字。**
      所以这里把 supported 当作**存在性**条件，而不是额外的过滤器：
      偏差只有在能显著估计的地方才存在。
    """
    wl = load_whitelist() if wl is None else wl
    rec = wl.get(_norm_league(league))
    if not rec or rec.get("net_bias") is None:
        return None
    if rec.get("supported") != 1:
        return None
    return float(rec["net_bias"]) - C.SPREAD_COST


def track_of(league, wl=None):
    """
    决定一个市场属于哪条线。

    ★ 2026-10-10 改：判据从「supported 布尔」改成「净偏差是否过门槛」。
      用户要求「限制在偏差较大的时候才交易」，所以：
        valid   = 净偏差 >= MIN_EDGE_PP（可估计且够大）-> 这才是"投资"
        explore = 偏差估不出来或不够大 -> 只作证据积累，额度极小
    """
    e = league_edge(league, wl)
    return "valid" if (e is not None and e >= C.MIN_EDGE_PP) else "explore"


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
            "days_to_start": r["days_to_start"], "game_start": r.get("game_start"),
            "end_date": r["end_date"], "vol24": r["vol24"], "volnum": r["volnum"],
            "liq": r["liq"], "is_cold": int(r["is_cold"]),
            "ref_fair": None, "ref_source": None,
            "neg_risk": int(r["neg_risk"]), "fee_type": r["fee_type"],
        })
        n_obs += 1
    conn.commit()

    # ---- 纸面下注（每市场只下注一次，按首次跨越的 lead 档归类）----
    n_bets = 0
    if not dry:
        # ★ 双线并行：按联赛分区（见 config.TRACKS 注释）
        wl = load_whitelist()
        room = {}
        for tk, cfg in C.TRACKS.items():
            n = conn.execute(
                "SELECT COUNT(*) c FROM bets WHERE status='open' AND track=?",
                (tk,)).fetchone()["c"]
            room[tk] = max(0, cfg["max_open"] - n)

        # ★ 每日投放上限（2026-10-10 用户明确要求）
        #   "一天的投资占比不能超过本金的一定比例"
        #   日盈亏标准差 = 2.24 x sqrt(stake x 日投放)，所以日投放直接决定日波动。
        #   两条线按 share 切分，避免 explore 吃掉投资线的风险预算。
        daily_cap = C.MAX_DAILY_NEW_PCT * C.PAPER_BANKROLL
        daily_used = float(conn.execute(
            "SELECT COALESCE(SUM(stake),0) s FROM bets WHERE entry_date=?",
            (today,)).fetchone()["s"] or 0.0)
        daily_used_track = {}
        for _r in conn.execute(
                "SELECT track, COALESCE(SUM(stake),0) s FROM bets "
                "WHERE entry_date=? GROUP BY track", (today,)):
            daily_used_track[_r["track"]] = float(_r["s"] or 0.0)
        daily_room = {}
        for tk, cfg in C.TRACKS.items():
            cap = daily_cap * float(cfg.get("share", 1.0))
            daily_room[tk] = max(0.0, cap - daily_used_track.get(tk, 0.0))
        daily_room["_all"] = max(0.0, daily_cap - daily_used)
        if verbose:
            print("  [scan] 日投放 $%.0f/$%.0f（上限 %.0f%% 本金）  双线: %s"
                  % (daily_used, daily_cap, C.MAX_DAILY_NEW_PCT * 100,
                     "  ".join("%s=%d/%d $%.0f"
                               % (k, room[k], C.TRACKS[k]["max_open"],
                                  daily_room[k]) for k in C.TRACKS)), flush=True)

        # ★ 风险闸：敞口上限 + 单簇上限
        #   第一轮 100 笔 42 分钟内开完、占用本金 100%，一次坏周末就是 -45%。
        #   56 笔里 22 笔还是荷乙同一轮 —— 那不是 100 个独立头寸。
        #   这两道闸不提高收益，只保证不会被单个周末打穿。
        max_expo = C.MAX_EXPOSURE_PCT * C.PAPER_BANKROLL
        max_clu = C.MAX_CLUSTER_PCT * C.PAPER_BANKROLL
        expo_used = float(conn.execute(
            "SELECT COALESCE(SUM(stake),0) s FROM bets WHERE status='open'"
        ).fetchone()["s"] or 0.0)
        expo_room = max(0.0, max_expo - expo_used)
        clu_used = {}
        for _r in conn.execute(
                "SELECT event_key, COALESCE(SUM(stake),0) s FROM bets "
                "WHERE status='open' GROUP BY event_key"):
            clu_used[_r["event_key"]] = float(_r["s"] or 0.0)
        if verbose:
            # ⚠ 这里必须打印**实际占用**，不能写 max_expo - expo_room ——
            #   超限时 expo_room 会被截到 0，于是永远显示成"刚好用满"，
            #   把它超了多少藏起来（第一版就犯了这个错）。
            print("  [scan] 风险闸: 敞口 $%.0f/$%.0f（上限 %.0f%% 本金）%s，单簇上限 $%.0f"
                  % (expo_used, max_expo, C.MAX_EXPOSURE_PCT * 100,
                     "  ⚠ 超限" if expo_used > max_expo else "", max_clu),
                  flush=True)
        n_block_expo = 0
        n_block_clu = 0
        n_block_daily = 0
        n_block_edge = 0

        # 赛前过滤：必须有开赛时间且在赛前
        cand = bet_cand[bet_cand["days_to_start"].notna()
                        & (bet_cand["days_to_start"] > 0)
                        & (bet_cand["days_to_start"] <= max(C.ENTRY_LEAD_DAYS) + 0.5)]
        cand = cand.sort_values("vol24", ascending=False)
        picked = 0
        n_by_track = {}
        # ★ 偏差门槛统计（用户要求"限制在偏差较大的时候才交易"）
        edge_seen = []
        for _, r in cand.iterrows():
            if picked >= C.MAX_NEW_BETS_PER_RUN:
                break
            ed = league_edge(r["league"], wl)
            edge_seen.append(ed)
            track = track_of(r["league"], wl)
            if track == "explore" and ed is not None:
                n_block_edge += 1      # 估得出偏差但没到门槛
            if room.get(track, 0) <= 0:
                continue
            d2s = float(r["days_to_start"])
            # ★ 入场时点归属（2026-10-10 起）：每个市场**只在首次跨过阈值时下一笔**，
            #   该市场就唯一归入 1/3/7 天中的某一档。这样 no-look-ahead：
            #   分档由预先固定的规则决定，而不是事后从同一批注里挑时点。
            #   （旧协议会对同一市场在 1/3/7 各下一笔；分析端按 market_id 去重，
            #     所以两个协议的样本可以合并比较。）
            target = None
            for L in sorted(C.ENTRY_LEAD_DAYS, reverse=True):
                if d2s <= L and d2s > L - 0.5:
                    target = L
                    break
            if target is None:
                continue
            # 按价格档决定方向
            rule_hit = None
            for lo, hi, sdir, note in C.BET_RULES:
                if lo <= r["mid"] < hi:
                    rule_hit = (sdir, note, lo, hi)
                    break
            if rule_hit is None:
                continue
            side, note, lo, hi = rule_hit
            # ★ BUG 修复 2026-10-10：原判重条件带 target_lead=?，
            #   而 SQL 里 NULL = 1 结果是 NULL（不为真），于是历史遗留的
            #   target_lead=NULL 的注永远匹配不上，同一市场被重复下注。
            #   实测：100 笔里有 25 笔是重复市场，其中 24 笔价位完全相同 —— 纯浪费额度。
            #
            # ★ 协议变更 2026-10-10（用户批准）：判重键**去掉 target_lead**，
            #   即每个市场只下一笔。原因：我们缺的是独立市场数（决定统计功效），
            #   同一市场按 1/3/7 天下三笔只是把有效样本摊薄成 1/3，并让 t 值虚高。
            #   target_lead 仍然记录 —— 它由「首次跨过哪个阈值」唯一决定，
            #   事后仍可按 1/3/7 天分档比较（分析端按 market_id 去重，口径兼容）。
            ex = conn.execute(
                "SELECT 1 FROM bets WHERE market_id=? AND rule_lo=? AND rule_hi=?",
                (r["market_id"], lo, hi)).fetchone()
            if ex:
                continue
            # ★ 风险闸检查（放在真正插入之前，保证不会下超）
            if C.STAKE > daily_room.get(track, 0.0) or C.STAKE > daily_room["_all"]:
                n_block_daily += 1
                continue
            if C.STAKE > expo_room * C.TRACKS[track].get("share", 1.0):
                n_block_expo += 1
                continue
            ekey = "%s|%s" % (r["league"], str(r.get("game_start"))[:10])
            if clu_used.get(ekey, 0.0) + C.STAKE > max_clu:
                n_block_clu += 1
                continue
            px = entry_price(r, side)
            if not (0.0 < px < 1.0):
                continue
            shares = C.STAKE / px
            fee = calc_fee(px, shares)
            conn.execute(
                "INSERT INTO bets(market_id,league,question,side,entry_price,entry_mid,"
                "shares,stake,fee,entry_ts,entry_date,days_to_end,days_to_start,"
                "game_start,rule_lo,rule_hi,rule_note,event_key,target_lead,status,track) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (r["market_id"], r["league"], r["question"], side, px, r["mid"],
                 shares, C.STAKE, fee, ts, today, r["days_to_end"], d2s,
                 r.get("game_start"), lo, hi, note,
                 "%s|%s" % (r["league"], str(r.get("game_start"))[:10]), target, track))
            n_bets += 1
            picked += 1
            room[track] -= 1
            expo_room -= C.STAKE
            daily_room[track] -= C.STAKE
            daily_room["_all"] -= C.STAKE
            clu_used[ekey] = clu_used.get(ekey, 0.0) + C.STAKE
            n_by_track[track] = n_by_track.get(track, 0) + 1
        conn.commit()
        if verbose and n_bets:
            print("  [scan] 新增 %d 笔（%s）"
                  % (n_bets, "  ".join("%s=%d" % kv for kv in sorted(n_by_track.items()))),
                  flush=True)
        if verbose and (n_block_daily or n_block_expo or n_block_clu):
            print("  [scan] 风险闸拦下: 日投放不足 %d / 敞口不足 %d / 单簇超限 %d 笔"
                  % (n_block_daily, n_block_expo, n_block_clu), flush=True)
        if verbose:
            known = [e for e in edge_seen if e is not None]
            print("  [scan] 偏差门槛: 候选 %d 个，其中 %d 个能估出联赛偏差"
                  "（%d 个未过 %.0fpp 门槛），%d 个联赛无历史数据"
                  % (len(edge_seen), len(known), n_block_edge, C.MIN_EDGE_PP * 100,
                     len(edge_seen) - len(known)), flush=True)

    if verbose and not dry:
        # ★ 队列诊断：区分"额度满"和"根本没有标的"，
        #   否则实验静默归零时会看起来一切正常（2026-10-10 踩过）。
        n_inwin = 0
        try:
            dd = bet_cand["days_to_start"]
            for L in sorted(C.ENTRY_LEAD_DAYS, reverse=True):
                n_inwin += int(((dd <= L) & (dd > L - 0.5)).sum())
        except Exception:
            pass
        print("  [scan] 队列：候选 %d 个，落在 1/3/7 天窗口内的 %d 个，本次新增 %d 笔"
              % (len(bet_cand), n_inwin, n_bets), flush=True)
        if n_inwin == 0:
            print("  [scan] ⚠ 窗口内没有任何候选 —— 不是额度问题，是**没有标的**。"
                  "请查联赛覆盖（MAX_LEAGUE_TAGS / MAX_PAGES_PER_TAG）", flush=True)

    D.log_run(conn, "scan", n_scanned=len(df), n_new_obs=n_obs, n_new_bets=n_bets,
              note="; ".join(FETCH_COVERAGE_ISSUES)[:500])
    res = {"scanned": len(df), "obs_candidates": len(obs_cand),
           "bet_candidates": len(bet_cand), "n_cold_leagues": len(cold),
           "n_obs": n_obs, "n_bets": n_bets}
    conn.close()
    return res


if __name__ == "__main__":
    print(run_scan())
