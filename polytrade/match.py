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


def _team_matches_tokens(ta, tb):
    """
    队名 token 集合是否指同一支球队。返回 (是否匹配, 共享的具辨识度 token 数)。

    ★ 2026-10-15：从 team_matches 里抽出来，供索引化后的匹配循环直接用
      预先算好的 token 集合，避免在 48 万次比较里反复做正则和建集合。
      语义与 team_matches 完全一致（team_matches 现在就是它的薄封装）。
    """
    if not ta or not tb:
        return False, 0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short.issubset(long_):
        return False, 0
    return True, len(short)


def team_matches(a, b):
    """
    队名是否指同一支球队。返回 (是否匹配, 共享的具辨识度 token 数)。

    规则：短名的 token 必须是长名 token 的子集，且共享 token 数 >= 1。
    （两个条件在剔除通用词后其实是等价的，但显式写出来便于审计。）
    """
    return _team_matches_tokens(_tokens(a), _tokens(b))


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


def prepare_fixture_index(fixtures, max_day_gap=2.0):
    """
    把 fixtures 预处理成可复用的索引（**只做一次，然后给所有市场共用**）。

    ★★★ 2026-10-15：这是 run_match 的性能修复 ★★★
      实测（14:05 那轮的真实日志）：
          [match] 本次代价 = 6087 个市场 x 79 条赛程 = 最多 480,873 次比较
          [match] 进度 1000/6087，已匹配 23，已用 139s
        => 全量约需 845 秒，而这一步的预算是 417 秒 ⇒ **必然被截断，
           匹配覆盖率实际只有约一半**。

      原实现每一轮市场循环里都在重复做三件昂贵的、与市场无关的事：
        (a) pd.to_datetime(f["commence_time"])  —— 48 万次日期解析
        (b) _tokens(f["home"]) / _tokens(f["away"]) —— 正则 + 建集合
        (c) 遍历**全部** 79 条赛程，而其中绝大多数连日期都对不上
            （max_day_gap 只有 2 天）

      这里把 (a)(b) 预计算一次，并按**日期分桶**解决 (c)：
      每个市场只看 end_date 前后几天的桶，而不是全部赛程。

      语义保证：分桶用的是一个**超集**（±ceil(max_day_gap)+1 天），
      之后仍然用原来的精确 gap 判定 —— 所以结果与优化前逐字节相同，
      这一点由 tests/test_regressions.py 里的等价性测试锁住。
    """
    span = int(max_day_gap) + 1
    all_fx = []
    by_date = {}
    for f in fixtures:
        try:
            ct = pd.to_datetime(f["commence_time"], utc=True)
        except Exception:
            # 与原逻辑一致：解析不出的赛程永远匹配不上，直接排除
            continue
        rec = {"fid": f["fid"], "home": f["home"], "away": f["away"],
               "_ct": ct, "_th": _tokens(f["home"]), "_ta": _tokens(f["away"])}
        all_fx.append(rec)
        try:
            by_date.setdefault(ct.date(), []).append(rec)
        except Exception:
            pass
    return {"all": all_fx, "by_date": by_date, "span": span}


def match_market(question, group_title, league, end_dt, fixtures=None,
                 max_day_gap=2.0, index=None):
    """
    把一个 Polymarket 市场匹配到 fixtures（同联赛）。
    fixtures: list of dict(fid, home, away, commence_time)
    返回最佳匹配 dict 或 None。

    ★ index：由 prepare_fixture_index() 预先算好的索引。
      **循环调用时一定要传 index**，否则每个市场都会重建一次索引（更慢）。
      index=None 时会用 fixtures 就地建一份 —— 保持旧的单次调用方式可用。
    """
    if index is None:
        index = prepare_fixture_index(fixtures or [], max_day_gap=max_day_gap)

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
    if not is_draw:
        tok_team = _tokens(team)

    end_ts = None
    if end_dt is not None:
        end_ts = pd.to_datetime(end_dt, utc=True)
    # 只取 end_date 前后几天的桶（超集），精确判定仍用原来的 gap 逻辑
    if end_ts is not None and pd.notna(end_ts):
        d0 = end_ts.date()
        cands = []
        for k in range(-index["span"], index["span"] + 1):
            cands.extend(index["by_date"].get(d0 + dt.timedelta(days=k), ()))
    else:
        cands = index["all"]

    best = None
    for f in cands:
        ct = f["_ct"]
        gap = abs((end_ts - ct).total_seconds()) / 86400.0 if end_ts is not None else 0.0
        if gap > max_day_gap:
            continue
        cand_side = None
        conf = 0.0
        if is_draw:
            a, b = draw_teams
            ok1 = _team_matches_tokens(_tokens(a), f["_th"])[0] and \
                _team_matches_tokens(_tokens(b), f["_ta"])[0]
            ok2 = _team_matches_tokens(_tokens(b), f["_th"])[0] and \
                _team_matches_tokens(_tokens(a), f["_ta"])[0]
            if ok1 or ok2:
                conf = 0.75
                cand_side = "draw"
            else:
                continue          # ★ 队名对不上 ⇒ 拒绝
        else:
            ok_h, nh = _team_matches_tokens(tok_team, f["_th"])
            ok_a, na = _team_matches_tokens(tok_team, f["_ta"])
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


def run_match(verbose=True, min_conf=0.45, budget_sec=None):
    """
    对 observations 里的市场做匹配，写入 market_fixture。
    只写入 confidence >= min_conf 的；其余丢弃（宁可漏）。

    ★ 2026-10-14：这一步是整轮运行被硬杀的真凶，改动三处 ——
      1) **打印代价**。这里是个 O(市场数 x 赛程数) 的纯 Python 双重循环，
         之前一句都不打印，所以外部看起来就是"卡死"。现在开头就报 N x M。
      2) **进度**。每 1000 个市场打一行，再也不会出现十几分钟毫无输出。
      3) **预算**。budget_sec 到点就在下一行停下，并报告还剩多少市场没匹配。
         之前它完全没有上限：实测 10:36 那次运行，扫描 646s 结束后，
         本步骤闷头跑了约 1000s，整轮 1765s，离 PT30M 只剩 36 秒。
    """
    import time as _time
    _t0 = _time.time()
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
        print("  [match] ★ 本次代价 = %d 个市场 x %d 条赛程 = 最多 %s 次比较"
              % (len(ms), len(fixtures), format(len(ms) * len(fixtures), ",")),
              flush=True)
        if budget_sec is not None:
            print("  [match] 预算 %.0fs" % budget_sec, flush=True)

    # ★ 2026-10-15：索引**只建一次**，所有市场共用。
    #   这正是把 480,873 次比较压下去的地方 —— 见 prepare_fixture_index。
    t_idx = _time.time()
    index = prepare_fixture_index(fixtures)
    if verbose:
        print("  [match] 索引已建：%d 条赛程 / %d 个日期桶，耗时 %.1fs"
              % (len(index["all"]), len(index["by_date"]), _time.time() - t_idx),
              flush=True)

    n_new, n_try = 0, 0
    truncated = False
    for _, r in ms.iterrows():
        if budget_sec is not None and (_time.time() - _t0) > budget_sec:
            truncated = True
            break
        n_try += 1
        if verbose and n_try % 1000 == 0:
            print("  [match] 进度 %d/%d，已匹配 %d，已用 %.0fs"
                  % (n_try, len(ms), n_new, _time.time() - _t0), flush=True)
        m = match_market(r["question"], r["group_title"], r["league"],
                         r["end_date"], index=index)
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
        print("  [match] 尝试 %d 个市场，成功匹配 %d 个（门槛 conf>=%.2f），耗时 %.0fs"
              % (n_try, n_new, min_conf, _time.time() - _t0), flush=True)
    if truncated:
        left = len(ms) - n_try
        print("  [match] ⚠ 预算 %.0fs 用尽，还剩 %d 个市场未匹配（匹配覆盖不完整）"
              % (budget_sec, left), flush=True)
        try:
            D.log_run(conn, "match", note="time_budget markets_unmatched=%d of %d"
                      % (left, len(ms)))
        except Exception as e:
            print("  [match] 预算截断记录写入失败: %r" % e, flush=True)
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
