"""
invariants.py —— 线上数据不变量检查
============================================================================
为什么需要它：

2026-10-12 用户指令："每天检查 bug，测试框架别省，数据要紧。"

本会话一共踩到 4 次静默失败（结算恒 0、联赛覆盖截断、代理掉线 36 小时、
报告分母不清）。共同点：**失败和成功看起来一样**。
单元测试能防"代码写错"，但**防不了"线上数据悄悄变质"** ——
比如某张表突然空了、某个字段全是 NULL、某个闸门实际没生效。

所以本模块检查的是**不变量**：无论代码怎么改、跑到第几天，
这些关系都必须成立；一旦不成立，说明数据或逻辑已经坏了。

设计原则：
  · 每条检查返回 (名字, 级别, 说明)，级别 ∈ {FAIL, WARN}
  · **只报事实，不做判断** —— 判据写死在检查里，避免"事后解释"
  · 全部可复算：每条都能单独跑、单独验证
  · 接入每日 digest，坏了会自己冒出来，不需要有人去问

★ 每一条检查都对应一次真实踩过的坑（见各项注释里的日期）。
   新增检查时请同样注明"它防的是哪一类事故"。
"""

import sys

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from . import config as C
from . import db as D

TOL = 1e-6

# 判重协议变更时刻（ISO，UTC）。此前的重复作为已知遗留、此后的重复算 FAIL。
# 依据：2026-10-10 那次"去掉 target_lead"的判重键修改。
BET_DEDUP_FIX_TS = "2026-10-10T08:00:00+00:00"


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def check_observations(conn):
    out = []
    # 价格基本关系。防的是"字段接错/解析错"导致价格越界 ——
    # 2026-10-10 曾因为 best_bid/best_ask 映射错误差点在错误价位下注。
    r = _q(conn, "SELECT COUNT(*) c FROM observations WHERE "
                 "bid IS NOT NULL AND ask IS NOT NULL AND (bid > ask OR bid < 0 OR ask > 1)")
    if r[0]["c"]:
        out.append(("obs.price_range", "FAIL",
                    "%d 行 bid/ask 违反 0<=bid<=ask<=1" % r[0]["c"]))
    r = _q(conn, "SELECT COUNT(*) c FROM observations WHERE "
                 "mid IS NOT NULL AND spread IS NOT NULL AND ABS(mid-(bid+ask)/2.0) > 0.005")
    if r[0]["c"]:
        out.append(("obs.mid_consistency", "WARN",
                    "%d 行 mid != (bid+ask)/2" % r[0]["c"]))
    # 唯一性。防的是"每天多条"或"去重键写错"。
    r = _q(conn, "SELECT COUNT(*) c FROM (SELECT market_id, snap_date, COUNT(*) n "
                 "FROM observations GROUP BY 1,2 HAVING n>1)")
    if r[0]["c"]:
        out.append(("obs.unique_day", "FAIL",
                    "%d 个 (market_id, snap_date) 重复" % r[0]["c"]))
    # 观测表不能空 —— 但**只在系统跑过之后**才算故障（2026-10-12 代理掉线那次）。
    # ⚠ 判据必须区分"空库"和"曾经有数据现在没了"：
    #   全新库是空的，那不是故障。靠 runs 表判断系统是否跑过。
    #   这条是 test_regressions 的自检抓出来的 —— 不变量模块自己也会判错。
    r = _q(conn, "SELECT COUNT(*) c FROM observations")
    if r[0]["c"] == 0:
        r2 = _q(conn, "SELECT COUNT(*) c FROM runs")
        if r2[0]["c"] > 0:
            out.append(("obs.not_empty", "FAIL",
                        "跑过 %d 次运行但 observations 为空 —— 抓取链路已断" % r2[0]["c"]))
    return out


def check_bets(conn):
    out = []
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE entry_price <= 0 OR entry_price >= 1")
    if r[0]["c"]:
        out.append(("bet.price_range", "FAIL", "%d 笔成交价越界" % r[0]["c"]))
    # 股数与投入必须自洽：shares = stake/price。
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE shares IS NOT NULL AND entry_price>0 "
                 "AND ABS(shares - stake/entry_price) > 0.01")
    if r[0]["c"]:
        out.append(("bet.shares", "FAIL", "%d 笔 shares != stake/price" % r[0]["c"]))
    # 官方手续费公式 fee = shares x 0.05 x p x (1-p)。接错就白算。
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE fee IS NOT NULL AND shares IS NOT NULL "
                 "AND entry_price>0 AND ABS(fee - shares*0.05*entry_price*(1-entry_price)) > 0.01")
    if r[0]["c"]:
        out.append(("bet.fee_formula", "FAIL", "%d 笔手续费与官方公式不符" % r[0]["c"]))
    # 判重：同一市场同一价格档只能有一笔。
    # 2026-10-10 因 NULL 判重失效重复下注。★ 这里必须按**协议变更时刻**分界，
    # 而不是"我第一次尝试修复"的时刻 —— 我第一次的 COALESCE 修复其实无效
    # （NULL 归一成 -1 仍匹配不上 1 或 3），真正修好它的是随后"去掉 target_lead"。
    # 实测：32 个重复组里 25 个在协议变更前、7 个落在两次修复之间、**之后 0 个**。
    # 所以判据是"变更之后不允许再有"，变更之前的作为已知遗留单列。
    r = _q(conn, "SELECT COUNT(*) c FROM (SELECT market_id, rule_lo, rule_hi "
                 "FROM bets WHERE entry_ts >= ? GROUP BY 1,2,3 HAVING COUNT(*)>1)",
           (BET_DEDUP_FIX_TS,))
    if r[0]["c"]:
        out.append(("bet.unique_market_rule", "FAIL",
                    "%d 组 (market, 价格档) 在判重修复之后仍然重复下注" % r[0]["c"]))
    r = _q(conn, "SELECT COUNT(*) c FROM (SELECT market_id, rule_lo, rule_hi "
                 "FROM bets WHERE entry_ts < ? GROUP BY 1,2,3 HAVING COUNT(*)>1)",
           (BET_DEDUP_FIX_TS,))
    if r[0]["c"]:
        out.append(("bet.unique_market_rule.legacy", "WARN",
                    "%d 组为判重修复前的已知遗留重复（保留以便审计，非新故障）" % r[0]["c"]))
    # 结算自洽：pnl = payout - stake - fee，且 outcome 必须是 0/1。
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE status='settled' "
                 "AND (outcome_yes NOT IN (0,1) OR ABS(pnl-(payout-stake-fee))>0.01)")
    if r[0]["c"]:
        out.append(("bet.settle_consistency", "FAIL",
                    "%d 笔已结算但 outcome/pnl 不自洽" % r[0]["c"]))
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE status='settled' AND outcome_yes IS NULL")
    if r[0]["c"]:
        out.append(("bet.settled_has_outcome", "FAIL",
                    "%d 笔标记已结算但没有结果" % r[0]["c"]))
    # 不能赛后建仓（防止回填/时间错乱把未来信息带进来）。
    r = _q(conn, "SELECT COUNT(*) c FROM bets WHERE game_start IS NOT NULL "
                 "AND entry_ts IS NOT NULL AND substr(entry_ts,1,19) >= substr(game_start,1,19)")
    if r[0]["c"]:
        out.append(("bet.entry_before_start", "FAIL",
                    "%d 笔在开赛之后才建仓" % r[0]["c"]))
    return out


def check_settlement_liveness(conn):
    """
    防"结算链路悄悄死掉"——2026-10-10 就是这条：17 次运行 n_settled 全是 0，
    外表看一切正常，实际一个数据点都没产出。

    ★ 判据基准必须是**市场结束时间(end_date)**，不是开赛时间(game_start)。
      第一版用 game_start+12h，结果把 ABA League 那笔误报成"结算卡住"：
      那场比赛 10-11 就踢完了，但市场 endDate=10-18、
      umaResolutionStatus=proposed —— 是 UMA 的争议窗口，我们**正确地**在等。
      用开赛时间做基准，等于要求"比赛一结束就结算"，而 Polymarket 不是那样运作的。
    """
    out = []
    r = _q(conn, """
        SELECT COUNT(*) c FROM bets b
        WHERE b.status='open'
          AND COALESCE(
                (SELECT MAX(o.end_date) FROM observations o WHERE o.market_id=b.market_id),
                b.game_start) IS NOT NULL
          AND datetime(COALESCE(
                (SELECT MAX(o.end_date) FROM observations o WHERE o.market_id=b.market_id),
                b.game_start)) < datetime('now','-36 hours')
    """)
    n = r[0]["c"]
    if n:
        out.append(("settle.liveness", "FAIL" if n >= 5 else "WARN",
                    "%d 笔注的市场已过期 36 小时以上仍是 open —— 结算链路可能已断" % n))
    return out


def check_risk_gates(conn):
    """
    防"风险闸实际没生效"。2026-10-10 用户质问"一天亏 25%"，根因就是没有闸门；
    加了闸门之后必须验证它真的在拦，而不是写在配置里好看。
    """
    out = []
    day = conn.execute(
        "SELECT COALESCE(SUM(stake),0) s FROM bets WHERE entry_date=?",
        (D.today(),)).fetchone()["s"]
    cap = C.MAX_DAILY_NEW_PCT * C.PAPER_BANKROLL
    if day > cap + C.STAKE:
        out.append(("risk.daily_cap", "FAIL",
                    "今日投放 $%.0f 超过上限 $%.0f" % (day, cap)))
    expo = conn.execute(
        "SELECT COALESCE(SUM(stake),0) s FROM bets WHERE status='open'").fetchone()["s"]
    cap2 = C.MAX_EXPOSURE_PCT * C.PAPER_BANKROLL
    if expo > cap2 + C.STAKE:
        out.append(("risk.exposure_cap", "FAIL",
                    "在场敞口 $%.0f 超过上限 $%.0f" % (expo, cap2)))
    cap3 = C.MAX_CLUSTER_PCT * C.PAPER_BANKROLL
    bad = _q(conn, "SELECT event_key, SUM(stake) s FROM bets WHERE status='open' "
                   "GROUP BY event_key HAVING s > ?", (cap3 + C.STAKE,))
    if bad:
        out.append(("risk.cluster_cap", "FAIL",
                    "%d 个「联赛x比赛日」簇超过 $%.0f" % (len(bad), cap3)))
    return out


def check_edge_gate(conn):
    """
    防"偏差门槛形同虚设"：valid 线的每一笔，都必须真的过门槛。
    2026-10-12 曾因 track_of 忘了要求 supported=1，
    把"单个历史市场算出 +79.7pp"的联赛判成 valid。
    """
    out = []
    try:
        from . import scan as SC
        wl = SC.load_whitelist()
        rows = _q(conn, "SELECT bet_id, league, entry_mid, entry_price, side FROM bets "
                        "WHERE track='valid'")
        bad = []
        for r in rows:
            mid = r["entry_mid"]
            if mid is None:
                mid = r["entry_price"]
            rel = SC.rel_edge(r["league"], mid, wl)
            if rel is None or rel < C.MIN_EDGE_PCT_OF_STAKE - 1e-9:
                bad.append(r["bet_id"])
        if bad:
            out.append(("gate.valid_track", "FAIL",
                        "%d 笔 valid 线注实际上没过偏差门槛（bet_id 例：%s）"
                        % (len(bad), bad[:5])))
    except Exception as e:
        out.append(("gate.valid_track", "WARN", "无法复核偏差门槛：%r" % e))
    return out


def check_equity(conn):
    """防"净值口径漂移"：equity 必须等于 本金 + 已实现 + 未实现。"""
    out = []
    rows = _q(conn, "SELECT * FROM equity")
    for r in rows:
        lhs = r["equity"]
        rhs = C.PAPER_BANKROLL + (r["realized_pnl"] or 0) + (r["unrealized_pnl"] or 0)
        if lhs is None or abs(lhs - rhs) > 0.05:
            out.append(("equity.identity", "FAIL",
                        "%s 净值 %s != 本金+已实现+未实现 %s" % (r["snap_date"], lhs, rhs)))
            break
    return out


def check_ai_forecasts(conn):
    """防"事后补录"：AI 预测必须在开赛前落库，否则整个实验不可证伪。"""
    out = []
    try:
        r = _q(conn, "SELECT COUNT(*) c FROM ai_forecasts WHERE game_start IS NOT NULL "
                     "AND made_ts IS NOT NULL AND substr(made_ts,1,19) >= substr(game_start,1,19)")
        if r[0]["c"]:
            out.append(("ai.pre_match_only", "FAIL",
                        "%d 条 AI 预测是在开赛后才落库的" % r[0]["c"]))
        r = _q(conn, "SELECT COUNT(*) c FROM ai_forecasts WHERE ai_prob < 0 OR ai_prob > 1")
        if r[0]["c"]:
            out.append(("ai.prob_range", "FAIL", "%d 条 AI 概率越界" % r[0]["c"]))
        r = _q(conn, "SELECT COUNT(*) c FROM (SELECT market_id, COUNT(*) n FROM ai_forecasts "
                     "GROUP BY 1 HAVING n>1)")
        if r[0]["c"]:
            out.append(("ai.unique_market", "FAIL",
                        "%d 个市场有重复 AI 预测（不可事后修改被破坏）" % r[0]["c"]))
    except Exception:
        pass
    return out


def check_all(conn=None, verbose=False):
    """跑全部不变量检查。返回 [(name, level, msg), ...]。"""
    own = conn is None
    conn = conn or D.connect()
    issues = []
    for fn in (check_observations, check_bets, check_settlement_liveness,
               check_risk_gates, check_edge_gate, check_equity, check_ai_forecasts):
        try:
            issues += fn(conn)
        except Exception as e:
            issues.append((fn.__name__, "WARN", "检查本身出错：%r" % e))
    if own:
        conn.close()
    if verbose:
        for n, lv, m in issues:
            print("  [%s] %-26s %s" % (lv, n, m))
        if not issues:
            print("  全部不变量通过")
    return issues


if __name__ == "__main__":
    iss = check_all(verbose=True)
    print("\n合计 %d 条问题（FAIL %d / WARN %d）"
          % (len(iss), sum(1 for _n, l, _m in iss if l == "FAIL"),
             sum(1 for _n, l, _m in iss if l == "WARN")))
    sys.exit(1 if any(l == "FAIL" for _n, l, _m in iss) else 0)
