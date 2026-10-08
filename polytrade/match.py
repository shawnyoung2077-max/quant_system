"""
match.py - 把 Polymarket 市场匹配到 Pinnacle 赛程（风险最高的一环）
============================================================================
为什么这是最高风险的一环：
  匹配错了 ⇒ 拿【别的比赛】的公允价去算 edge ⇒ 下游所有结论都是垃圾，
  而且**不会报错**。所以本模块的原则是：**宁可漏，绝不能错。**

匹配要做两件事（缺一不可）：
  1. 队名对应：Polymarket 的 "Will HJK win?" / groupItemTitle="HJK" ↔ Pinnacle 的 "HJK Helsinki"
  2. 日期对应：两家给出的比赛时间必须足够接近

★ 为什么队名匹配不能用"有交集"
  反例（和联赛名的坑同源）：
    "FC Seoul"     vs "FC Tokyo"        → 交集 {"fc"}          ✗ 不同队
    "Manchester City" vs "Manchester United" → 交集 {"manchester"} ✗ 同城死敌
  所以：
    · 剔掉通用词（fc/fk/sc/cf/united/city/rovers/...）
    · 要求【较短名】的 token 全部出现在较长名里（子集包含）
    · 子集包含后仍要求共享 token 里至少有一个"具辨识度"的词
    · 再叠加日期接近度
  任一条不满足 → 不匹配（宁可漏）

输出写进 market_fixture 表，并记录 confidence 与 method 以便审计。
"""

import re
import datetime as dt

import numpy as np
import pandas as pd

from . import config as C
from . import db as D

# 队名里到处出现、不承载辨识度的词
TEAM_STOP = {
    "fc", "fk", "sc", "cf", "ac", "sk", "bk", "if", "ff", "cd", "ca", "cs", "csd",
    "afc", "cfc", "sfc", "kfc", "us", "ss", "as", "sv", "tsv", "vfl", "vfb",
    "united", "utd", "city", "town", "rovers", "wanderers", "athletic", "sporting",
    "club", "de", "futbol", "futebol", "calcio", "team", "the", "and",
    "football", "soccer", "sport", "sports",
}

MATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_fixture (
    mf_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id  TEXT NOT NULL,
    fid        INTEGER NOT NULL,
    market_team TEXT,          -- Polymarket 侧指向的队伍名
    fixture_side TEXT,         -- 'home' / 'away' / 'draw'
    confidence REAL,
    day_gap    REAL,
    method     TEXT,
    matched_ts TEXT,
    UNIQUE(market_id, fid)
);
CREATE INDEX IF NOT EXISTS idx_mf_market ON market_fixture(market_id);
"""

# ★ 只有这些市场类型，"Pinnacle h2h 公允概率"才是可比的。
#   其它类型（正确比分/总进球/角球/两队都进球/半场结果…）虽然队名能对上，
#   但赌的是完全不同的事件 —— 必须排除，否则信号是纯粹的垃圾。
MONEYLINE_TYPES = {
    "moneyline", "child_moneyline", "first_half_moneyline",
    "",                       # 勘察发现 938 个空类型，多为对阵式胜负市场
}


def ensure_schema(conn):
    conn.executescript(MATCH_SCHEMA)
    conn.commit()


def _tokens(name):
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return {t for t in s.split() if t and t not in TEAM_STOP and len(t) > 1}


def team_matches(a, b):
    """
    队名是否指同一支球队。返回 (是否匹配, 共享的具辨识度 token 数)。

    规则：短名的 token 必须是长名 token 的子集，且共享 token 数 >= 1。
    （两个条件在剔除通用词后其实是等价的，但显式写出来便于审计。）
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False, 0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short.issubset(long_):
        return False, 0
    return True, len(short)


def extract_market_team(question, group_title):
    """
    从 Polymarket 市场里提取"这个市场在赌哪支队伍"。
    常见形态：
      groupItemTitle = "HJK Helsinki"          ← 最可靠
      question       = "Will HJK win on ..."
      question       = "HJK Helsinki vs. VPS Vaasa"（对阵式，取两边都试）
    平局市场（Draw (A vs. B)）在 match_market 里单独处理，这里返回 None。
    """
    gt = (group_title or "").strip()
    if gt and gt.lower() not in ("draw", "yes", "no"):
        return gt, "group_title"
    q = (question or "").strip()
    m = re.search(r"will\s+(.+?)\s+win", q, re.I)
    if m:
        return m.group(1).strip(), "question_will_win"
    m = re.search(r"^(.+?)\s+vs\.?\s+(.+?)(?:\s*[:\?]|$)", q, re.I)
    if m:
        return m.group(1).strip(), "question_vs_home"
    return None, None


def match_market(question, group_title, league, end_dt, fixtures,
                 max_day_gap=2.0):
    """
    把一个 Polymarket 市场匹配到 fixtures（同联赛）。
    fixtures: list of dict(fid, home, away, commence_time)
    返回最佳匹配 dict 或 None。
    """
    team, method = extract_market_team(question, group_title)
    is_draw = (group_title or "").strip().lower() == "draw" or \
              re.search(r"\bdraw\b", question or "", re.I) is not None

    # ★ 平局市场必须单独处理：它赌的不是某支队伍，而是"这场比赛的平局"。
    #   实测踩到的严重假匹配（只看了日期、没验证队名）：
    #     "Draw (Incheon United FC vs. Pohang Steelers FC)" -> Gangwon vs Bucheon  ✗
    #     "Draw (Al Sadd SC vs. Al Arabi Doha SC)"          -> Nordsjaelland vs Odense ✗
    #   所以必须从 "Draw (A vs. B)" 里解析出两队，并逐一对上。
    draw_teams = None
    if is_draw:
        m = re.search(r"\((.+?)\s+vs\.?\s+(.+?)\)", question or "", re.I)
        if not m:
            return None           # 解析不出对阵就不匹配（宁可漏）
        draw_teams = (m.group(1).strip(), m.group(2).strip())

    best = None
    for f in fixtures:
        try:
            ct = pd.to_datetime(f["commence_time"], utc=True)
        except Exception:
            continue
        gap = abs((pd.to_datetime(end_dt, utc=True) - ct).total_seconds()) / 86400.0 \
            if end_dt is not None else 0.0
        if gap > max_day_gap:
            continue
        cand_side = None
        conf = 0.0
        if is_draw:
            a, b = draw_teams
            ok1 = team_matches(a, f["home"])[0] and team_matches(b, f["away"])[0]
            ok2 = team_matches(b, f["home"])[0] and team_matches(a, f["away"])[0]
            if ok1 or ok2:
                conf = 0.75
                cand_side = "draw"
            else:
                continue          # ★ 队名对不上 ⇒ 拒绝
        else:
            ok_h, nh = team_matches(team, f["home"])
            ok_a, na = team_matches(team, f["away"])
            if ok_h and not ok_a:
                cand_side, conf = "home", 0.6 + 0.2 * nh
            elif ok_a and not ok_h:
                cand_side, conf = "away", 0.6 + 0.2 * na
            elif ok_h and ok_a:
                # 两队都匹配上 ⇒ 歧义 ⇒ 拒绝（宁可漏）
                continue
        if cand_side is None:
            continue
        # 日期越接近越可信
        conf *= max(0.5, 1.0 - gap / max(max_day_gap, 1e-6))
        conf = min(conf, 1.0)          # 截断到 1.0，便于当概率读
        if best is None or conf > best["confidence"]:
            best = {"fid": f["fid"], "fixture_side": cand_side,
                    "market_team": team, "confidence": round(conf, 4),
                    "day_gap": round(gap, 3), "method": method}
    return best


def run_match(verbose=True, min_conf=0.45):
    """
    对 observations 里的市场做匹配，写入 market_fixture。
    只写入 confidence >= min_conf 的；其余丢弃（宁可漏）。
    """
    conn = D.connect()
    ensure_schema(conn)
    fx = pd.read_sql_query(
        "SELECT fid,sport_key,commence_time,home,away,fair_mult,fair_power,fair_shin "
        "FROM odds_fixtures", conn)
    if fx.empty:
        if verbose:
            print("  [match] odds_fixtures 为空，先跑 refodds", flush=True)
        conn.close()
        return 0, 0
    # 按 sport_key 分组，但 Polymarket 的 league 名与 sport_key 不同，
    # 所以这里按【日期窗口 + 队名】全局匹配，不限联赛（更宽松但更可靠，
    # 因为同一天同名球队跨联赛撞车的概率极低）
    fixtures = fx.to_dict("records")

    # 只对近期有报价的市场做匹配
    # ⚠️ 必须限制市场类型！Pinnacle 的 h2h（1X2）公允概率【只适用于胜负类市场】。
    #    实测踩到：Grazer AK 1902 0 - 0 FC Salzburg 这类【正确比分】市场
    #    队名能匹配上，但它的价格是"这个确切比分"的概率，
    #    与 1X2 的公允概率完全不是一个东西 —— 拿来比较会产出纯垃圾。
    #    正确总进数、角球数、两队是否都进球等市场同理。
    ms = pd.read_sql_query(
        "SELECT DISTINCT market_id, question, group_title, league, end_date, market_type "
        "FROM observations WHERE snap_date >= date('now','-7 day')", conn)
    before = len(ms)
    ms = ms[ms["market_type"].isin(MONEYLINE_TYPES)]
    if verbose:
        print("  [match] 市场类型过滤: %d -> %d（只保留胜负类）"
              % (before, len(ms)), flush=True)

    n_new, n_try = 0, 0
    for _, r in ms.iterrows():
        n_try += 1
        m = match_market(r["question"], r["group_title"], r["league"],
                         r["end_date"], fixtures)
        if not m or m["confidence"] < min_conf:
            continue
        conn.execute(
            "INSERT INTO market_fixture(market_id,fid,market_team,fixture_side,"
            "confidence,day_gap,method,matched_ts) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(market_id,fid) DO UPDATE SET "
            "confidence=excluded.confidence, day_gap=excluded.day_gap, "
            "matched_ts=excluded.matched_ts",
            (r["market_id"], m["fid"], m["market_team"], m["fixture_side"],
             m["confidence"], m["day_gap"], m["method"], D.now_iso()))
        n_new += 1
    conn.commit()
    if verbose:
        print("  [match] 尝试 %d 个市场，成功匹配 %d 个（门槛 conf>=%.2f）"
              % (n_try, n_new, min_conf), flush=True)
    conn.close()
    return n_new, n_try


def main():
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    n, t = run_match()
    conn = D.connect()
    ensure_schema(conn)
    df = pd.read_sql_query(
        "SELECT mf.market_id, mf.market_team, mf.fixture_side, mf.confidence, "
        "mf.day_gap, f.home, f.away FROM market_fixture mf "
        "JOIN odds_fixtures f ON f.fid=mf.fid "
        "ORDER BY mf.confidence DESC LIMIT 20", conn)
    print()
    print(df.to_string(index=False))
    dfc = pd.read_sql_query("SELECT confidence FROM market_fixture", conn)
    if len(dfc):
        print()
        print("confidence 分布: 中位 %.3f  min %.3f  max %.3f"
              % (dfc.confidence.median(), dfc.confidence.min(), dfc.confidence.max()))
    conn.close()


if __name__ == "__main__":
    main()
