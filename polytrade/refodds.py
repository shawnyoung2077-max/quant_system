"""
refodds.py - 外部参照赔率（Pinnacle de-vig）—— 系统的"技术分析"地基
============================================================================
为什么需要它：用户的核心要求是"至少比大众判断准"。
没有外部参照价，Polymarket 的价格就是唯一信息源，你无法判断它是贵还是便宜。
有了 Pinnacle 的三向赔率并 de-vig 掉抽水，你才有一个**独立于大众情绪的公允概率**。

═══════════════════════════════════════════════════════════════════════════
★ 额度是硬约束（免费档 500 次/月），本模块围绕它设计
═══════════════════════════════════════════════════════════════════════════
实测：1 次 odds 请求 = 1 个 sport_key（返回该联赛全部赛程）
      10 个联赛 x 每 2 小时一次 = 120 次/天 => 4 天烧完 500 次
所以必须：
  · 联赛白名单（只抓"Polymarket 有 x odds-api 也有 x 测出偏差大"的交集）
  · 降频（2 次/天，不是 12 次）
  · 额度预算器（DB 里记账，超预算就不发请求）
  · 缓存（同一联赛短时间内不重复抓）

═══════════════════════════════════════════════════════════════════════════
★ de-vig：三种方法，结果不同，冷门档差异最大
═══════════════════════════════════════════════════════════════════════════
博彩公司的赔率含抽水（overround）。实测本项目相关联赛约 3.9%~4.1%。
去掉抽水有三种主流方法，**在冷门档给出显著不同的公允概率**：

  1. 比例法 (multiplicative)  : p_i / Σp        —— 最简单，假设抽水按比例分摊
  2. 幂法 (power / logarithmic): p_i^k 归一化    —— 对冷门修正更强
  3. Shin 法                    : 反解内幕交易比例 z —— 理论上最合理

我历史研究用的是什么没记录 —— 这意味着**我之前的"错价"结论可能有一部分
只是 de-vig 方法选得不同造成的**。所以本模块三种都算，并显式报告差异。
"""

import io
import json
import os
import sys
import time
import datetime as dt

import requests
import numpy as np

from . import config as C
from . import db as D

ODDS_BASE = "https://api.the-odds-api.com/v4"
KEY_FILE = os.path.join(C.ROOT, "configs", "odds_api.json")

# ---------------------------------------------------------------------------
# 联赛白名单：只在"Polymarket 有 x odds-api 也有"的交集里抓
# ---------------------------------------------------------------------------
# polymarket 名称 -> the-odds-api sport_key
# 选入标准（按优先级）：
#   1. 我历史测出的偏差大小（J1 League t=4.21 最大）
#   2. Polymarket 侧的活跃度（Veikkausliiga 24h $16,323 是最活跃的冷门联赛）
#   3. odds-api 是否有该联赛（很多冷门联赛没有，如拉脱维亚/爱沙尼亚/法罗群岛）
LEAGUE_MAP = {
    "Veikkausliiga":           "soccer_finland_veikkausliiga",
    "J1 League":               "soccer_japan_j_league",
    "League of Ireland Premier": "soccer_league_of_ireland",
    "K League 1":              "soccer_korea_kleague1",
    "Superettan":              "soccer_sweden_superettan",
    "Danish Superliga":        "soccer_denmark_superliga",
    "Austria Bundesliga":      "soccer_austria_bundesliga",
    "Eliteserien":             "soccer_norway_eliteserien",
}

# 抓取节奏：每天抓几次（决定额度消耗）
# 额度账（免费档 500/月）：
#   朴素做法 8 联赛 x 每 2 小时 = 2880 次/月  -> 5 天烧完 500 次
#   本系统   5 个有效联赛 x 1 次/天 = 150 次/月 -> 只用 30% 预算
#   （有效联赛数动态变化：只在 Polymarket 有对应市场时才抓）
FETCHES_PER_DAY = 1
# 每月额度预算（留 100 次缓冲）
MONTHLY_BUDGET = 400


def load_key():
    if not os.path.exists(KEY_FILE):
        return None
    try:
        return json.load(io.open(KEY_FILE, encoding="utf-8")).get("api_key")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# de-vig
# ---------------------------------------------------------------------------
def devig(odds, method="multiplicative"):
    """
    odds: 小数赔率列表（如 [1.57, 5.63, 4.45]，对应 [home, away, draw]）
    返回去掉抽水后的概率列表，和 odds 同序、和为 1。
    """
    o = np.asarray(odds, dtype=float)
    if np.any(o <= 1.0):
        return None
    p = 1.0 / o
    s = p.sum()
    if method == "multiplicative":
        return list(p / s)

    if method == "power":
        # 求 k 使 Σ p_i^k = 1。
        # 因为 p_i ∈ (0,1)，p_i^k 随 k 增大而【减小】；
        # 而 k=1 时 Σp = s = overround > 1，所以根在 k > 1 一侧。
        #   v > 1  ⇒ k 太小 ⇒ 增大 k ⇒ lo = mid
        #   v < 1  ⇒ k 太大 ⇒ 减小 k ⇒ hi = mid
        # ⚠️ 这里方向写反过一次，导致 fair_power 算出 0.99 这种荒谬值
        #    （热门队 odds=1.57 的概率被算成 99%）。已修正。
        lo, hi = 1.0, 10.0
        for _ in range(200):
            mid = (lo + hi) / 2
            v = float(np.sum(p ** mid))
            if v > 1.0:
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2
        q = p ** k
        return list(q / q.sum())

    if method == "shin":
        # Shin (1993)：假设有比例 z 的内幕交易者。
        # Σp 在 z=0 时 = sqrt(s) > 1，随 z 增大单调递减，根在小 z 一侧。
        #   v > 1 ⇒ z 太小 ⇒ 增大 z ⇒ lo = mid
        #   v < 1 ⇒ z 太大 ⇒ 减小 z ⇒ hi = mid
        def probs(z):
            return (np.sqrt(z ** 2 + 4 * (1 - z) * p ** 2 / s) - z) / (2 * (1 - z))
        lo, hi = 0.0, 0.5
        for _ in range(200):
            mid = (lo + hi) / 2
            v = float(probs(mid).sum())
            if v > 1.0:
                lo = mid
            else:
                hi = mid
        q = probs((lo + hi) / 2)
        return list(q / q.sum())

    raise ValueError("method 必须是 multiplicative / power / shin")


def overround(odds):
    return float(np.sum(1.0 / np.asarray(odds, dtype=float)))


# ---------------------------------------------------------------------------
# 额度与缓存
# ---------------------------------------------------------------------------
QUOTA_SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_fixtures (
    fid INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_ts TEXT, sport_key TEXT,
    commence_time TEXT, home TEXT, away TEXT,
    odds_home REAL, odds_away REAL, odds_draw REAL,
    fair_mult REAL, fair_power REAL, fair_shin REAL,   -- 主队胜的公允概率
    overround REAL, bookmaker TEXT,
    UNIQUE(sport_key, commence_time, home, away)
);
CREATE TABLE IF NOT EXISTS odds_quota (
    month TEXT PRIMARY KEY, used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS odds_fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, sport_key TEXT, status INTEGER, remaining_hdr TEXT
);
"""


def ensure_schema(conn):
    conn.executescript(QUOTA_SCHEMA)
    # ★ 三向公允概率必须都存下来。
    #   初版只存了主队（fair_mult = fm[0]）——那样给客队/平局市场填 ref_fair 时
    #   会把【主队】的公允价填到客队市场上，产生完全错误的参照价。
    #   用一列 JSON 存 [mult3, power3, shin3]，避免加 6 个列。
    cols = [r[1] for r in conn.execute("PRAGMA table_info(odds_fixtures)")]
    if "fair_json" not in cols:
        try:
            conn.execute("ALTER TABLE odds_fixtures ADD COLUMN fair_json TEXT")
        except Exception:
            pass
    conn.commit()


def month_key():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def quota_used(conn):
    r = conn.execute("SELECT used FROM odds_quota WHERE month=?",
                     (month_key(),)).fetchone()
    return int(r["used"]) if r else 0


def quota_add(conn, n=1):
    conn.execute("INSERT INTO odds_quota(month,used) VALUES(?,?) "
                 "ON CONFLICT(month) DO UPDATE SET used=used+excluded.used",
                 (month_key(), n))
    conn.commit()


def fetch_league(conn, sport_key, regions="eu", markets="h2h",
                 bookmaker="pinnacle", force=False, verbose=True):
    """
    抓一个联赛的赛程与赔率。返回 (新增行数, 说明)。
    额度不足或最近已抓过则跳过。
    """
    ensure_schema(conn)
    key = load_key()
    if not key:
        return 0, "无 API key（configs/odds_api.json 缺失）"

    # 额度检查
    used = quota_used(conn)
    if not force and used >= MONTHLY_BUDGET:
        return 0, "本月额度已用尽 (%d/%d)，跳过" % (used, MONTHLY_BUDGET)

    # 缓存检查：同一联赛 FETCHES_PER_DAY 次/天，间隔 < 12/FETCHES_PER_DAY 小时则跳过
    if not force:
        r = conn.execute(
            "SELECT ts FROM odds_fetch_log WHERE sport_key=? ORDER BY id DESC LIMIT 1",
            (sport_key,)).fetchone()
        if r:
            last = dt.datetime.fromisoformat(r["ts"])
            gap = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600
            min_gap = 24.0 / FETCHES_PER_DAY
            if gap < min_gap:
                return 0, "距上次抓取仅 %.1f 小时 (< %.1f)，跳过" % (gap, min_gap)

    url = "%s/sports/%s/odds" % (ODDS_BASE, sport_key)
    try:
        r = requests.get(url, params={"apiKey": key, "regions": regions,
                                      "markets": markets, "oddsFormat": "decimal"},
                         timeout=C.HTTP_TIMEOUT)
    except Exception as e:
        conn.execute("INSERT INTO odds_fetch_log(ts,sport_key,status,remaining_hdr)"
                     " VALUES(?,?,?,?)", (D.now_iso(), sport_key, -1, str(e)[:80]))
        conn.commit()
        return 0, "请求异常: %s" % str(e)[:80]

    conn.execute("INSERT INTO odds_fetch_log(ts,sport_key,status,remaining_hdr)"
                 " VALUES(?,?,?,?)",
                 (D.now_iso(), sport_key, r.status_code,
                  r.headers.get("x-requests-remaining")))
    conn.commit()
    if r.status_code != 200:
        return 0, "HTTP %d: %s" % (r.status_code, r.text[:100])

    quota_add(conn, 1)
    data = r.json()
    n = 0
    for ev in data:
        home, away = ev.get("home_team"), ev.get("away_team")
        ct = ev.get("commence_time")
        bk = None
        for b in ev.get("bookmakers", []):
            if b["key"] == bookmaker:
                bk = b
                break
        if not bk:
            continue
        for mk in bk.get("markets", []):
            if mk["key"] != "h2h":
                continue
            od = {o["name"]: o["price"] for o in mk["outcomes"]}
            oh, oa = od.get(home), od.get(away)
            odr = od.get("Draw")
            if not (oh and oa and odr):
                continue
            order = [oh, oa, odr]
            fm = devig(order, "multiplicative")
            fp = devig(order, "power")
            fs = devig(order, "shin")
            conn.execute(
                "INSERT INTO odds_fixtures(fetched_ts,sport_key,commence_time,home,away,"
                "odds_home,odds_away,odds_draw,fair_mult,fair_power,fair_shin,"
                "overround,bookmaker,fair_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sport_key,commence_time,home,away) DO UPDATE SET "
                "fetched_ts=excluded.fetched_ts, odds_home=excluded.odds_home, "
                "odds_away=excluded.odds_away, odds_draw=excluded.odds_draw, "
                "fair_mult=excluded.fair_mult, fair_power=excluded.fair_power, "
                "fair_shin=excluded.fair_shin, overround=excluded.overround, "
                "fair_json=excluded.fair_json",
                (D.now_iso(), sport_key, ct, home, away, oh, oa, odr,
                 fm[0] if fm else None, fp[0] if fp else None, fs[0] if fs else None,
                 overround(order), bookmaker,
                 json.dumps([fm, fp, fs]) if fm else None))
            n += 1
    conn.commit()
    return n, "OK (HTTP 200, 剩余额度 %s)" % r.headers.get("x-requests-remaining")


import re as _re

# 归一化时忽略的通用词。
# ⚠️ 注意不要把 "1"/"2"/"a"/"b" 放进来 —— 它们区分级别！
#    K League 1 与 K League 2 是不同联赛；
#    剥掉数字后两者都变成 {"k"}，就会互相误匹配。
_STOP = {"division", "div", "league", "liga", "ligue", "premier", "premiership",
         "super", "superleague", "the", "de", "of",
         "championship", "cup", "serie", "primera", "allsvenskan"}


def _norm(s):
    """把联赛名归一化成 token 集合。"""
    s = (s or "").lower()
    s = _re.sub(r"[^a-z0-9\s]", " ", s)
    return {t for t in s.split() if t and t not in _STOP}


def league_matches(name_a, name_b):
    """
    联赛名是否指同一个联赛。

    ⚠️ 这里必须用【子集包含】而不是【有交集】。
       实测反例：
         "K League 1"        vs "K League 2"         → 不同级别
         "Austria Bundesliga" vs "Basketball Bundesliga" → 足球 vs 篮球
       两者的归一化集合各有交集（{"k"} 或 {"bundesliga"}），
       用"有交集"会全部误匹配。

    ★ 为什么宁可漏也不能错：
       漏掉一个联赛 = 少一个机会（可接受）；
       误匹配 = 拿【别的联赛】的赔率去算公允价（产生完全错误的数据，
                下游所有结论都脏）。**假匹配的危害远大**。

    判定：短名（token 更少的那个）的所有 token 必须都出现在长名里。
         两边都归一化成空集时，退回原串精确比较。
    """
    a, b = _norm(name_a), _norm(name_b)
    if not a or not b:
        return (name_a or "").lower().strip() == (name_b or "").lower().strip()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return short.issubset(long_)


def leagues_with_pm_markets(conn):
    """
    返回【Polymarket 侧确实有开放市场】的联赛集合（原始名）。
    额度优化的核心：没人交易对应市场的联赛，抓它的赔率纯属浪费额度。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT league FROM observations "
            "WHERE snap_date >= date('now','-7 day')").fetchall()
        return {r["league"] for r in rows if r["league"]}
    except Exception:
        return set()


def resolve_league(whitelist_name, obs_leagues):
    """白名单名 → observations 里的实际名（模糊匹配）。找不到返回 None。"""
    for o in obs_leagues:
        if league_matches(whitelist_name, o):
            return o
    return None


def fetch_all(conn=None, verbose=True, only_active=True):
    """
    抓白名单联赛的赔率。
    only_active=True 时只抓【Polymarket 近期有市场】的联赛 —— 省额度。
    用模糊匹配避免联赛名拼写差异导致的静默跳过。
    """
    own = conn is None
    conn = conn or D.connect()
    ensure_schema(conn)
    obs = leagues_with_pm_markets(conn) if only_active else None
    tot = 0
    skipped = []
    for lg, sk in LEAGUE_MAP.items():
        if only_active and obs is not None:
            hit = resolve_league(lg, obs)
            if hit is None:
                skipped.append(lg)
                continue
            if hit != lg and verbose:
                print("  [refodds] 联赛名匹配: %r -> %r" % (lg, hit), flush=True)
        n, msg = fetch_league(conn, sk, verbose=verbose)
        if verbose:
            print("  [refodds] %-28s %-34s %s" % (lg, sk, msg), flush=True)
        tot += n
        time.sleep(0.3)
    if skipped and verbose:
        print("  [refodds] 跳过 %d 个无对应 Polymarket 市场的联赛（省额度）: %s"
              % (len(skipped), ", ".join(skipped[:8])), flush=True)
    if own:
        conn.close()
    return tot


def fill_ref_fair(conn, verbose=False):
    """
    把 odds_fixtures 的三向公允概率，按 market_fixture 的 fixture_side
    填进 observations.ref_fair（YES 口径，与 observations.mid 同口径）。

      fixture_side='home' -> 主队胜的公允概率
      fixture_side='away' -> 客队胜的公允概率
      fixture_side='draw' -> 平局的公允概率

    ★ 必须按 side 取对应的分量。初版只存了主队值，若不分 side 就会把
      主队的公允价填到客队市场上（错误数据）。已用 fair_json 修正。
    """
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT mf.market_id, mf.fixture_side, f.fair_json "
        "FROM market_fixture mf JOIN odds_fixtures f ON f.fid = mf.fid "
        "WHERE f.fair_json IS NOT NULL").fetchall()
    idx_map = {"home": 0, "away": 1, "draw": 2}
    n = 0
    for r in rows:
        idx = idx_map.get(r["fixture_side"])
        if idx is None:
            continue
        try:
            # fair_json = [mult3, power3, shin3]，每个是 [home, away, draw]
            vec = json.loads(r["fair_json"])
            mult = vec[0][idx] if vec[0] else None
        except Exception:
            continue
        if mult is None:
            continue
        conn.execute(
            "UPDATE observations SET ref_fair=?, ref_source=? WHERE market_id=?",
            (float(mult), "pinnacle_h2h_%s" % r["fixture_side"], r["market_id"]))
        n += 1
    conn.commit()
    if verbose:
        print("  [refodds] 填入 %d 行 ref_fair" % n, flush=True)
    return n


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    conn = D.connect()
    ensure_schema(conn)
    print("剩余额度: %d/%d" % (MONTHLY_BUDGET - quota_used(conn), MONTHLY_BUDGET))
    n = fetch_all(conn)
    print("新增/更新 %d 条赛程" % n)


if __name__ == "__main__":
    main()
