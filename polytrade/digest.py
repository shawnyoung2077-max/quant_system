"""
digest.py - 看门狗 + 每日摘要
============================================================================
用户要的"全自动化"里，AI 真正该待的位置是这个：

  执行层（扫描/下注/结算）→ 确定性代码，已经自动了
  监控层（系统坏了吗？）  → 本模块。这是 AI 不必常驻但必须有覆盖的地方
  研究层（规则对吗？）    → 需要人+AI 一起看，本模块给材料

看门狗检查的是「静默失败」—— 这类故障不会报错，只会让系统假装在工作：
  · 任务没在跑（最后一次运行太久以前）
  · 连着好几次没下注（规则或筛选悄悄挂了）
  · 观测数暴涨/暴跌（API 改结构了）
  · 结算一直没进展（比赛都结束了还是 open）
  · 额度用尽（参照价停了但没人知道）
"""

import datetime as dt
import os

import numpy as np
import pandas as pd

from . import config as C
from . import db as D

# 阈值（超出即告警）
STALE_HOURS = 6.0          # 距上次运行超过这么久 = 任务可能停了
NO_BET_RUNS = 6            # 连续这么多次运行没下注 = 可疑
OBS_CHANGE_PCT = 0.5       # 观测数相对上次变动超过 50% = 可疑
SETTLE_STUCK_DAYS = 4.0    # 有注到期这么久了还是 open = 可疑
# The 2026-10-10 pagination fix expanded coverage; don't compare its first
# complete-catalog snapshot with the known truncated snapshot from the day before.
OBS_COVERAGE_BASELINE_DATE = "2026-10-10"


def health_check(conn=None, verbose=True):
    """
    返回 (issues:list[str], stats:dict)。
    issues 为空 = 一切正常。
    """
    own = conn is None
    conn = conn or D.connect()
    issues = []
    stats = {}

    # ---- 1. 最近是否有运行 ----
    runs = pd.read_sql_query(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT 20", conn)
    if runs.empty:
        issues.append("[严重] 没有任何运行记录 —— 任务从未成功执行过")
        stats["n_runs"] = 0
    else:
        last = pd.to_datetime(runs.iloc[0]["run_ts"], errors="coerce", utc=True)
        now = dt.datetime.now(dt.timezone.utc)
        hours = (now - last).total_seconds() / 3600 if pd.notna(last) else np.nan
        stats["last_run_hours_ago"] = round(float(hours), 2)
        stats["n_runs"] = int(pd.read_sql_query(
            "SELECT COUNT(*) c FROM runs", conn).iloc[0, 0])
        if hours > STALE_HOURS:
            issues.append("[严重] 距上次运行 %.1f 小时（阈值 %.0f）—— 任务可能已停止"
                          % (hours, STALE_HOURS))

    # ---- 2. 连续多次没下注 ----
    scans = runs[runs["kind"] == "scan"] if not runs.empty else runs
    if len(scans) >= NO_BET_RUNS:
        recent = scans.head(NO_BET_RUNS)
        if (recent["n_new_bets"].fillna(0) == 0).all():
            # ★ 2026-10-10 修正：先分清"额度满了"还是"筛不出标的"，
            #   否则会把正常的满仓误报成策略失效。
            n_open = int(pd.read_sql_query(
                "SELECT COUNT(*) c FROM bets WHERE status='open'", conn).iloc[0, 0])
            stats["n_open_bets"] = n_open
            if n_open >= C.MAX_OPEN_BETS:
                issues.append(
                    "[提示] 最近 %d 次扫描没有新下注，因为持仓已满"
                    "（open=%d / 上限 %d）—— 结算后会自动恢复；"
                    "若长期不结算请查 settle"
                    % (NO_BET_RUNS, n_open, C.MAX_OPEN_BETS))
            else:
                issues.append("[警告] 最近 %d 次扫描都没有新下注（持仓未满 open=%d/%d）—— "
                              "可能是筛选条件太严、或规则区间已无市场"
                              % (NO_BET_RUNS, n_open, C.MAX_OPEN_BETS))
    stats["recent_new_bets"] = (int(scans.head(6)["n_new_bets"].fillna(0).sum())
                                if not scans.empty else 0)

    if not scans.empty:
        last_note = str(scans.iloc[0].get("note") or "")
        if last_note:
            # ★ 2026-10-10：这里给 [提示] 而不是 [警告]。
            #   MAX_PAGES_PER_TAG=10 是**已知且接受的取舍** ——
            #   实测 53 个联赛会稳定打满（Bundesliga/LaLiga/CS2 ...）。
            #   如果每轮都报"异常"，告警就变成噪音，人会开始无视它，
            #   而那正是最初那个"静默少采"能藏两天的土壤。
            #   真正要报警的是下面第 3 节的观测数突变。
            issues.append("[提示] 本轮扫描有联赛打满页上限（已知取舍，非故障）：%s"
                          % last_note)

    # ---- 3. 观测数是否异常波动 ----
    obs_today = pd.read_sql_query(
        "SELECT snap_date, COUNT(DISTINCT market_id) n FROM observations "
        "GROUP BY snap_date ORDER BY snap_date DESC LIMIT 7", conn)
    stats["obs_days"] = len(obs_today)
    if len(obs_today) >= 2:
        cur, prev = int(obs_today.iloc[0]["n"]), int(obs_today.iloc[1]["n"])
        stats["obs_cur"], stats["obs_prev"] = cur, prev
        prev_day = str(obs_today.iloc[1]["snap_date"])
        if prev > 0 and prev_day >= OBS_COVERAGE_BASELINE_DATE:
            chg = abs(cur - prev) / prev
            if chg > OBS_CHANGE_PCT:
                issues.append("[警告] 观测数从 %d 变到 %d（%.0f%%）—— "
                              "可能是 API 结构变化或筛选失效" % (prev, cur, chg * 100))
    stats["obs_unique_markets"] = int(
        pd.read_sql_query("SELECT COUNT(DISTINCT market_id) c FROM observations",
                          conn).iloc[0, 0])

    # ---- 4. 结算是否卡住 ----
    b = pd.read_sql_query(
        "SELECT status, COUNT(*) n FROM bets GROUP BY status", conn)
    stats["bets"] = dict(zip(b["status"], b["n"])) if not b.empty else {}
    stats["n_bets_total"] = int(b["n"].sum()) if not b.empty else 0

    # ⚠️ 这里曾写成 `days_to_start < -?` 且传 -SETTLE_STUCK_DAYS，
    #    双重负号使条件变成 days_to_start < 4.0 —— 把刚下的新注全误报成"卡住"。
    stuck = pd.read_sql_query(
        "SELECT COUNT(*) c FROM bets WHERE status='open' "
        "AND days_to_start IS NOT NULL AND days_to_start < ?",
        conn, params=(-SETTLE_STUCK_DAYS,)).iloc[0, 0]
    stats["stuck_open"] = int(stuck)
    if stuck > 0:
        issues.append("[警告] 有 %d 笔注在开赛 %.0f 天后仍未结算 —— "
                      "可能是结算 API 或结果解析有问题" % (stuck, SETTLE_STUCK_DAYS))

    # ---- 5. 参照价额度 ----
    try:
        q = pd.read_sql_query("SELECT month, used FROM odds_quota", conn)
        if not q.empty:
            used = int(q.iloc[-1]["used"])
            stats["odds_quota_used"] = used
            if used > 350:
                issues.append("[警告] the-odds-api 本月已用 %d/500 —— 接近上限"
                              % used)
    except Exception:
        pass

    # ---- 6. 参照价覆盖率 ----
    nref = pd.read_sql_query(
        "SELECT COUNT(*) c FROM observations WHERE ref_fair IS NOT NULL",
        conn).iloc[0, 0]
    stats["ref_fair_rows"] = int(nref)

    if own:
        conn.close()
    if verbose:
        icon = "OK" if not issues else "有问题"
        print("  [watchdog] %s（%d 项检查）" % (icon, 6))
        for i in issues:
            print("      %s" % i)
    return issues, stats


def daily_digest(conn=None):
    """生成一份人可读的每日简报（纯文本）。"""
    own = conn is None
    conn = conn or D.connect()
    L = []
    now = dt.datetime.now()

    def w(s=""):
        L.append(str(s))

    w("=" * 76)
    w("  POLYTRADE 每日简报   %s" % now.strftime("%Y-%m-%d %H:%M"))
    w("=" * 76)
    w("")

    issues, st = health_check(conn, verbose=False)
    w("【系统状态】")
    if issues:
        for i in issues:
            w("  X  %s" % i)
    else:
        w("  正常 —— 无异常")
    w("  上次运行    : %.1f 小时前（共 %d 次）"
      % (st.get("last_run_hours_ago", float("nan")), st.get("n_runs", 0)))
    w("  观测市场数  : %d" % st.get("obs_unique_markets", 0))
    w("  纸面注      : %d 笔 %s" % (st.get("n_bets_total", 0), st.get("bets", {})))
    b = pd.read_sql_query("SELECT * FROM bets", conn)
    if not b.empty and "track" in b:
        w("  双线账本    : " + "  ".join(
            "%s %d 笔（open %d / settled %d，独立已结算市场 %d）" % (
                name, len(g), int((g.status == "open").sum()),
                int((g.status == "settled").sum()),
                g.loc[g.status == "settled", "market_id"].nunique())
            for name, g in b.groupby("track", dropna=False)))
    w("  参照价覆盖  : %d 行有 ref_fair" % st.get("ref_fair_rows", 0))
    w("  API 额度    : %s / 500" % st.get("odds_quota_used", "?"))
    w("")

    # ---- 纸面盈亏 ----
    stt = b[b["status"] == "settled"] if not b.empty else pd.DataFrame()
    w("【纸面盈亏】")
    if len(stt):
        tot = float(stt["pnl"].sum())
        stake_settled = float(stt["stake"].sum())
        # ★ 2026-10-10 口径修正：原文案写「本金的 x%」，而本金恰好等于全部计划敞口，
        #   读起来像"回撤"，实际不是。同一笔 $253.62 在本金 1000 / 3000 /
        #   实际投入 560 下分别是 -25.36% / -8.45% / -45.29%。
        #   简报里只留**可比的**那个，其余交给详细报告。
        w("  已结算投入 $%.0f  盈亏 $%+.2f  对已投入资金的收益率 %+.2f%%"
          % (stake_settled, tot, tot / stake_settled * 100 if stake_settled else 0.0))
        w("  胜率 %.1f%%  平均每注 $%+.4f"
          % ((stt["payout"] > 0).mean() * 100, tot / len(stt)))
        # 净值与真实回撤（equity 快照）
        try:
            from . import equity as EQ
            s = EQ.compute()
            md = EQ.max_drawdown()
            w("  净值 $%.2f（已实现 $%+.2f / 未实现 $%+.2f）  在场敞口 $%.0f（%.1f%% 本金，上限 %.0f%%）"
              % (s["equity"], s["realized_pnl"], s["unrealized_pnl"],
                 s["open_stake"], (s["exposure_pct"] or 0) * 100,
                 C.MAX_EXPOSURE_PCT * 100))
            if md and md["n_snapshots"] >= 3:
                w("  最大回撤 %.2f%%（%s → %s）"
                  % (md["max_drawdown"] * 100, md["peak_date"], md["trough_date"]))
            else:
                w("  最大回撤：暂不可测（只有 %d 个快照日，需 >=3）"
                  % (md["n_snapshots"] if md else 0))
        except Exception as e:
            w("  [净值] 计算失败：%r" % e)
        market = stt.groupby("market_id").agg(pnl=("pnl", "sum"), stake=("stake", "sum"))
        base = market["pnl"] / market["stake"]
        se = base.std(ddof=1) / np.sqrt(len(base)) if len(base) > 1 else float("nan")
        w("  市场级平均净收益率（%d 个独立市场）= %+.4f%%   SE=%.4f%%"
          % (len(base), base.mean() * 100, se * 100))
    else:
        w("  尚无已结算的注 —— 比赛还没踢完")
        if not b.empty:
            d = b[b["status"] == "open"]["days_to_start"]
            d = pd.to_numeric(d, errors="coerce").dropna()
            if len(d):
                w("  %d 笔持有中，最近的 %.2f 天后开赛"
                  % (len(d), max(d.min(), 0)))
    w("")

    # ---- 按规则 / 时点 ----
    if not b.empty:
        w("【按规则】")
        g = b.groupby(["rule_lo", "rule_hi", "side"], dropna=False).agg(
            n=("bet_id", "size"),
            settled=("status", lambda s: (s == "settled").sum()),
            pnl=("pnl", "sum"))
        for (lo, hi, sd), r in g.iterrows():
            w("  %.2f-%.2f 买 %-3s : %d 笔（已结算 %d）盈亏 $%+.2f"
              % (lo, hi, sd, r["n"], r["settled"], r["pnl"]))
        w("")
        w("【按入场时点】（对方要求分别评估 1/3/7 天）")
        g2 = b.groupby("target_lead", dropna=False).agg(
            n=("bet_id", "size"),
            settled=("status", lambda s: (s == "settled").sum()),
            pnl=("pnl", "sum"))
        for k, r in g2.iterrows():
            w("  %s 天前入场 : %d 笔（已结算 %d）盈亏 $%+.2f"
              % ("?" if pd.isna(k) else "%d" % k, r["n"], r["settled"], r["pnl"]))
        w("")

    # ---- 距目标还有多远 ----
    n_set = int(stt["market_id"].nunique()) if len(stt) else 0
    target = 100  # initial review checkpoint, not the open-position cap
    w("【进度】")
    w("  已结算独立市场 %d / 初步复核点 %d（%.0f%%）"
      % (n_set, target, min(n_set / target * 100, 100)))
    if n_set == 0:
        w("  说明：100 个独立市场只是初步复核点；统计结论仍需看区间、")
        w("        联赛/赛事聚类和 valid 线的独立样本量。")
    w("")
    w("  参照价对照（Pinnacle de-vig vs Polymarket）:")
    m = pd.read_sql_query(
        "SELECT league, question, mid, ref_fair FROM observations "
        "WHERE ref_fair IS NOT NULL ORDER BY mid LIMIT 10", conn)
    if len(m):
        for _, r in m.iterrows():
            dev = r["mid"] - r["ref_fair"]
            w("    pm %.3f  fair %.3f  偏离 %+.3f  %s"
              % (r["mid"], r["ref_fair"], dev, str(r["question"])[:44]))
        dd = m["mid"] - m["ref_fair"]
        w("    n=%d  平均偏离 %+.4f" % (len(m), dd.mean()))
    else:
        w("    还没有匹配上的参照价")

    if own:
        conn.close()
    return "\n".join(L)


def save_digest(text=None, name="digest_latest.txt"):
    if text is None:
        text = daily_digest()
    p = os.path.join(C.OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    t = daily_digest()
    print(t)
    print()
    print("已写出:", save_digest(t))
