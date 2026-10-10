"""
report.py - 报告：用观测数据回答核心问题
============================================================================
这份报告要回答的不是"我赚了多少"（纸面赚亏只是症状），而是三件事：

  Q1 【校准】市场价准不准？—— 按价格档、按冷门/热门分别看
     若某个档的实际胜率系统性偏离价格 ⇒ 存在可交易的错价
  Q2 【方向】偏离朝哪边？—— 决定该买 YES 还是买 NO
  Q3 【资金】按实测 k 反推需要的启动资金

第三问用用户给的模型：
     C_min = F / (k*n - r_f)
     k = 每次交易的净收益率, n = 年周转次数, F = 年固定成本, r_f = 无风险利率
"""

import os
import sys
import numpy as np
import pandas as pd
from scipy import stats

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from . import config as C
from . import db as D


def _logits(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def load(conn=None):
    own = conn is None
    conn = conn or D.connect()
    obs = pd.read_sql_query("SELECT * FROM observations", conn)
    bets = pd.read_sql_query("SELECT * FROM bets", conn)
    if own:
        conn.close()
    return obs, bets


def calibration_table(df, by, label):
    """按 by 列分组，比较 平均价格 vs 实际胜率。"""
    rows = []
    for k, g in df.groupby(by, dropna=True):
        n = len(g)
        if n < 5:
            continue
        p = float(g["mid"].mean())
        o = float(g["outcome_yes"].mean())
        se = float(np.sqrt(o * (1 - o) / n)) if 0 < o < 1 else np.nan
        # edge = 买 NO 的每股期望 = 价格 - 实际胜率
        e = p - o
        t = e / se if se and se > 0 else np.nan
        rows.append({label: k, "n": n, "均价p": round(p, 4), "实际胜率r": round(o, 4),
                     "edge(买NO)": round(e, 4), "SE": round(se, 4) if se == se else None,
                     "t值": round(t, 2) if t == t else None,
                     "显著": ("是" if abs(t) > 2 else "否") if t == t else "-"})
    return pd.DataFrame(rows)


def settled_edge_analysis(st):
    """
    ★ 2026-10-10 新增：对已结算样本做【去重 + 聚类】的 edge 检验。

    为什么必须做 —— 这是本次最容易骗自己的地方：
      1) 100 笔纸面注里有 25 笔是**同一市场的重复下注**，其中 24 笔价位完全相同。
         按笔数直接算 t 值会把有效样本量虚增 ~70%，t 值跟着虚高。
      2) 同一联赛同一天的比赛高度相关（实测 22/56 笔来自荷乙同一轮）。
         按笔独立假设算出的标准误是错的，必须按「联赛 x 比赛日」聚类。

    k 是买入合约的结果减去实际入场价；YES/NO 按各自合约方向计算。
    k_net 再扣除实际交易费用。返回 naive / 去重 / 聚类三套统计量。

    注意：本函数只报「有没有信号」，不代表已经确认 edge。样本量远不够。
    """
    if st is None or len(st) == 0:
        return None
    d = st.dropna(subset=["entry_price", "outcome_yes"]).copy()
    if len(d) == 0:
        return None
    d["outcome_yes"] = d["outcome_yes"].astype(float)
    out = {}

    d["contract_outcome"] = np.where(
        d["side"].eq("NO"), 1.0 - d["outcome_yes"], d["outcome_yes"])
    d["gross_edge"] = d["contract_outcome"] - d["entry_price"]
    d["net_edge"] = np.where(
        d["shares"].gt(0) & d["pnl"].notna(), d["pnl"] / d["shares"],
        d["gross_edge"])

    def _k(gr, col="gross_edge"):
        return float(gr[col].mean())

    # (a) 按笔（错的，仅作对照）
    n = len(d)
    out["naive"] = {"n": n, "k": _k(d), "k_net": _k(d, "net_edge"),
                    "se": float(d["gross_edge"].std(ddof=1) / np.sqrt(n))}

    # (b) 按市场去重：同一市场只保留第一笔
    u = d.sort_values("entry_ts").groupby("market_id", as_index=False).first()
    nu = len(u)
    out["per_market"] = {"n": nu, "k": _k(u), "k_net": _k(u, "net_edge"),
                         "se": float(u["gross_edge"].std(ddof=1) / np.sqrt(nu))}

    # (c) 按 联赛 x 比赛日 聚类（簇内先平均，再对簇做 t 检验）
    uu = u.copy()
    uu["_day"] = uu["game_start"].astype(str).str[:10]
    cl = uu.groupby(["league", "_day"])[["gross_edge", "net_edge"]].mean()
    nc = len(cl)
    if nc >= 2:
        out["cluster"] = {"n": nc, "k": float(cl["gross_edge"].mean()),
                          "k_net": float(cl["net_edge"].mean()),
                          "se": float(cl["gross_edge"].std(ddof=1) / np.sqrt(nc))}

    for v in out.values():
        v["t"] = v["k"] / v["se"] if v["se"] and v["se"] > 0 else float("nan")
        if v["se"] and v["se"] > 0:
            v["ci"] = (v["k"] - 1.96 * v["se"], v["k"] + 1.96 * v["se"])
        else:
            v["ci"] = (float("nan"), float("nan"))
    out["dup_ratio"] = 1 - nu / n if n else 0.0
    return out


def league_transport_check(st, verbose=True):
    """
    ★ 2026-10-10 新增：把实盘结果按「该联赛有没有历史证据支撑」分层。

    起因：实盘第一轮 k = -11.5pp，而当初支撑策略的历史结论是同档 +7.8pp。
    追下去发现——**证据基础和实盘部署几乎不重叠**：

      历史数据集 74,242 行 / 179 联赛 / 本档 11,239 行，
      主力是 World Cup(19k行)、LaLiga、NFL、Premier League、Bundesliga；
      而实盘 32/56 笔押在 Eerste Divisie(历史 8 行, 本档 0 行) 和 Ligue 2(本档 3 行)。

    且历史效应本身高度异质：同档同 lead，联赛间 bias 从 -10.0pp 到 +36.3pp。
    所以"这一档 +8pp"是对**大联赛/世界杯**成立的平均结论，不能直接搬到荷乙/法乙。

    本函数输出分层后的 k，避免再用一个聚合数字代表所有联赛。
    """
    out = None
    try:
        sup_path = os.path.join(C.ROOT, "modeling", "league_support.csv")
        if not os.path.exists(sup_path):
            if verbose:
                print("  [分层] 找不到 %s，先跑 build_league_support.py" % sup_path)
            return None
        sup = pd.read_csv(sup_path)
        d = st.dropna(subset=["entry_price", "outcome_yes"]).copy()
        d["outcome_yes"] = d["outcome_yes"].astype(float)
        d = d.sort_values("entry_ts").groupby("market_id", as_index=False).first()

        # 联赛名归一化匹配（实盘名来自 Polymarket tag，历史名来自数据集）
        def norm(s):
            s = str(s).lower()
            for ch in " .-_'":
                s = s.replace(ch, "")
            return s

        key = {norm(r["league_name"]): r for _, r in sup.iterrows()}
        d["_n"] = d["league"].map(norm)
        d["_sup"] = d["_n"].map(lambda x: int(key[x]["supported"]) if x in key else 0)
        d["_nband"] = d["_n"].map(lambda x: int(key[x]["n_band"]) if x in key else 0)
        # 若 bets 表已有 track 列（双线并行），以它为准；否则按联赛现算。
        if "track" in d.columns:
            d["_sup"] = d["track"].map(lambda x: 1 if x == "valid" else 0).fillna(d["_sup"])

        out = {}
        for flag, lab in ((1, "有历史支撑"), (0, "无历史支撑")):
            g = d[d["_sup"] == flag]
            if len(g) == 0:
                continue
            k = float(g["outcome_yes"].mean() - g["entry_price"].mean())
            se = float(g["outcome_yes"].std(ddof=1) / np.sqrt(len(g))) if len(g) > 1 else float("nan")
            out[lab] = {"n": len(g), "k": k, "se": se,
                        "t": k / se if se and se > 0 else float("nan")}
        out["_detail"] = d
        return out
    except Exception as e:
        # ★ 不要静默吞掉：这一段跳过会正好藏起最该看的信息
        #   （2026-10-10 就踩过：os 未导入 -> NameError -> 整段无声消失）
        print("  [分层] 计算失败（不是'没有数据'，请修）：%r" % e, flush=True)
        try:
            import traceback
            traceback.print_exc()
        except Exception:
            pass
        return None


def report(tag="report", verbose=True):
    conn = D.connect()
    obs, bets = load(conn)
    L = []

    def log(s=""):
        L.append(str(s))
        print(str(s), flush=True)

    log("=" * 100)
    log("Polytrade 纸面交易报告  (%s)" % tag)
    log("=" * 100)
    log("")

    # ---------------- 运行概况 ----------------
    runs = pd.read_sql_query("SELECT * FROM runs ORDER BY run_id", conn)
    log("运行记录: %d 次" % len(runs))
    if len(runs):
        log("  最近 5 次:")
        for _, r in runs.tail(5).iterrows():
            log("    %s %-8s scanned=%-5s obs=%-5s bets=%-4s settled=%s"
                % (r["run_ts"][:19], r["kind"], r["n_scanned"], r["n_new_obs"],
                   r["n_new_bets"], r["n_settled"]))

    log("")
    log("观测总数 %d 行，覆盖 %d 个市场，%d 个快照日"
        % (len(obs), obs["market_id"].nunique() if len(obs) else 0,
           obs["snap_date"].nunique() if len(obs) else 0))
    log("纸面下注 %d 笔（open=%d, settled=%d）"
        % (len(bets),
           int((bets["status"] == "open").sum()) if len(bets) else 0,
           int((bets["status"] == "settled").sum()) if len(bets) else 0))
    if len(bets) and "track" in bets:
        for name, g in bets.groupby("track", dropna=False):
            settled_g = g[g["status"] == "settled"]
            log("  %-7s open=%d settled=%d 独立已结算市场=%d" % (
                str(name), int((g["status"] == "open").sum()), len(settled_g),
                settled_g["market_id"].nunique()))

    # ---------------- 纸面 P&L ----------------
    st = bets[bets["status"] == "settled"] if len(bets) else pd.DataFrame()
    if len(st):
        tot = float(st["pnl"].sum())
        stake_settled = float(st["stake"].sum())
        log("")
        log("=" * 100)
        log("1. 纸面盈亏")
        log("=" * 100)
        # ★ 2026-10-10 口径修正：原先只印「总盈亏 / 本金」，而本金恰好等于
        #   全部计划敞口，于是任何亏损都被报成一个吓人的百分比，被误读成"回撤"。
        #   同一笔 $253.62：分母填 1000 = -25.36%，填 3000 = -8.45%，
        #   填实际投入 560 = -45.29% —— 三个数，同样的钱。
        #   这里把口径并列出来，并指明只有哪个可以和 edge 讨论挂钩。
        log("  本金基准 = $%.0f（单注 $%.0f）" % (C.PAPER_BANKROLL, C.STAKE))
        log("")
        log("  【口径必须分清】同一笔亏损在不同分母下差很多：")
        log("     已结算投入              $%9.2f" % stake_settled)
        log("     已结算盈亏              $%+9.2f" % tot)
        log("     ★ 对已投入资金的收益率   %+8.2f%%   <- 只有这个能和 edge 挂钩"
            % (tot / stake_settled * 100 if stake_settled else 0.0))
        log("     占名义本金               %+8.2f%%   <- 仅供参考，**不是回撤**"
            % (tot / C.PAPER_BANKROLL * 100))
        log("")
        log("  胜率 = %.1f%%   平均每注 = $%+.4f   平均投入 = $%.3f   平均回款 = $%.3f"
            % (float((st["payout"] > 0).mean()) * 100, tot / len(st),
               float(st["stake"].mean()), float(st["payout"].mean())))

        # ---------- ★ 风险与真实回撤（2026-10-10 新增）----------
        try:
            from . import equity as EQ
            log("")
            log("  " + "-" * 92)
            log("  ★ 风险与回撤（真实口径，来自 equity 逐日快照）")
            log("  " + "-" * 92)
            for _ln in EQ.risk_report().splitlines():
                log(_ln)
        except Exception as e:
            log("  [风险] 计算失败：%r" % e)
        log("")
        log("  按联赛:")
        g = st.groupby("league").agg(n=("pnl", "size"), pnl=("pnl", "sum"),
                                     win=("payout", lambda s: (s > 0).mean()))
        g = g.sort_values("pnl")
        for k, r in g.iterrows():
            log("    %-24s n=%-4d 盈亏=$%+8.3f  胜率=%.0f%%"
                % (str(k)[:24], r["n"], r["pnl"], r["win"] * 100))

        # ---------- ★ 核心：edge 检验（去重 + 聚类） ----------
        ea = settled_edge_analysis(st)
        if ea:
            log("")
            log("  " + "-" * 92)
            log("  ★ 核心：方向收益 = 该合约实际结果 - 入场价（正 = 买入该方向有 edge）")
            log("  " + "-" * 92)
            log("     口径            有效n    k(gross)  k(net/share)   SE       t      95%CI")
            for key, lab in (("naive", "按笔(错)"),
                             ("per_market", "按市场去重"),
                             ("cluster", "联赛x比赛日聚类")):
                v = ea.get(key)
                if not v:
                    continue
                log("     %-14s %5d  %+7.4f   %+7.4f    %6.4f  %+6.2f  [%+.4f, %+.4f]"
                    % (lab, v["n"], v["k"], v["k_net"], v["se"], v["t"],
                       v["ci"][0], v["ci"][1]))
            log("")
            log("     重复下注占比 = %.1f%%（同一市场重复下注，不计入有效样本）"
                % (ea["dup_ratio"] * 100))
            pm = ea.get("per_market")
            if pm and abs(pm["t"]) < 2:
                log("     ⇒ |t| < 2：**样本量还不够判定 edge 是否存在**，不要据此改策略。")
            elif pm:
                log("     ⇒ 注意：这是单一样本、单一周末、少数几个联赛的结果；")
                log("       若要改策略，先确认它在不同联赛/时段上可复现。")
            log("")
            log("     交易成本实测：成交价 - 当时中间价 = 均值 %+.4f 元/股"
                % float((st["entry_price"] - st["entry_mid"]).mean()))
            log("       （这是每笔必然付出的成本，必须从 k 里扣掉才算净 edge）")

        # ---------- ★ 证据可迁移性：按联赛历史支撑分层 ----------
        # ⚠ 注意：这里**不能**写成 `lt.get("有历史支撑")`——
        #   如果实盘全部押在"无历史支撑"的联赛上（这正是 2026-10-10 的真实情况），
        #   那个 key 根本不存在，整段会被静默跳过，恰好把最该看的信息藏起来。
        lt = league_transport_check(st, verbose=verbose)
        if lt and (lt.get("有历史支撑") or lt.get("无历史支撑")):
            log("")
            log("  " + "-" * 92)
            log("  ★ 证据可迁移性：实盘结果按「该联赛有没有历史证据」分层")
            log("  " + "-" * 92)
            for lab in ("有历史支撑", "无历史支撑"):
                v = lt.get(lab)
                if not v:
                    log("     %-10s  n=0" % lab)
                    continue
                log("     %-10s  n=%3d   k=%+.4f  SE=%.4f  t=%+.2f"
                    % (lab, v["n"], v["k"], v["se"], v["t"]))
            log("")
            log("     ⚠ 若实盘的钱主要花在「无历史支撑」的联赛上，那么")
            log("       历史那个 +7~9pp 从来就不是关于这些联赛的结论 ——")
            log("       这不是策略失效，是**分布外外推**。")
            log("     ⚠ 历史效应本身异质：同价格档同 lead，联赛间 bias 从 -10pp 到 +36pp，")
            log("       所以也不该拿一个聚合的 +8pp 当所有联赛的预期。")
            det = lt.get("_detail")
            if det is not None and len(det):
                g = det.groupby("league").agg(n=("market_id", "size"),
                                              sup=("_sup", "max"),
                                              nband=("_nband", "max")).sort_values("n", ascending=False)
                log("     实盘联赛分布（前 10）:")
                log("       %-34s %5s %10s %10s" % ("联赛", "下注", "有历史支撑", "历史本档n"))
                for lg, r in g.head(10).iterrows():
                    log("       %-34s %5d %10s %10d"
                        % (str(lg)[:32], r["n"], "是" if r["sup"] else "否", r["nband"]))

    # ---------------- 校准（核心） ----------------
    log("")
    log("=" * 100)
    log("2. 校准：市场价准不准？（需要已结算市场）")
    log("=" * 100)
    # 从观测里取每个市场的最后一次观测，并与结果合并
    obs_s = obs.sort_values("snap_ts").groupby("market_id").tail(1).copy() \
        if len(obs) else obs
    res_map = {}
    if len(st):
        for _, r in st.iterrows():
            res_map[r["market_id"]] = r["outcome_yes"]
    # 也用观测市场的结算结果（即便没下注）
    if len(obs_s):
        obs_s["outcome_yes"] = obs_s["market_id"].map(res_map)
        have = obs_s[obs_s["outcome_yes"].notna()]
    else:
        have = obs_s

    if len(have) < 5:
        log("  已结算的观测市场只有 %d 个 —— 还没有足够数据做校准。" % len(have))
        log("  这是正常的：市场需要先结算。等积累到期后本表会自动填充。")
        log("")
        log("  ★ 当前系统状态：**数据积累阶段**")
        log("     已记录 %d 个合格市场的价格快照，等待结算。" % len(obs))
        if len(obs):
            log("     距结算天数分布: 中位=%.1f 天"
                % float(obs["days_to_end"].median()))
            log("     涉及联赛 %d 个，其中冷门 %d 个"
                % (obs["league"].nunique(),
                   obs.loc[obs["is_cold"] == 1, "league"].nunique()))
    else:
        log("  可用于校准的已结算市场 = %d 个" % len(have))
        log("")
        log("  ⚠ 口径警告：下面用的价格是**该市场最后一次被观测到的中间价**，")
        log("    不是决策时刻的价格。已关闭市场不会再被扫描，所以它通常已经接近")
        log("    赛前最后时刻 —— 信息比我们真正下单时多。这张表只能当**方向性**参考，")
        log("    不能当成'当时就能赚到的 edge'。可交易口径请看第 1 节的 entry_price。")
        log("")
        log("  --- 按冷门/热门 ---")
        t = calibration_table(have, "is_cold", "is_cold")
        log(t.to_string(index=False))
        log("")
        log("  --- 按价格档 ---")
        have = have.copy()
        have["price_band"] = pd.cut(have["mid"], [0, .05, .10, .15, .20, .30, .50, 1.01])
        t2 = calibration_table(have, "price_band", "价格档")
        log(t2.to_string(index=False))
        log("")
        log("  --- 冷门 x 价格档 ---")
        t3 = calibration_table(have[have["is_cold"] == 1], "price_band", "冷门价格档")
        if len(t3):
            log(t3.to_string(index=False))

    # ---------------- 资金 ----------------
    log("")
    log("=" * 100)
    log("3. 需要多少启动资金？（用实测 k 反推）")
    log("=" * 100)
    log("")
    log("  模型：年利润 = C*k*n - C*r_f - F   ⇒   C_min = F / (k*n - r_f)")
    log("")
    if len(st):
        # k = 每次交易的净收益率（占投入资金）
        st = st.copy()
        # Old protocol could place several tickets on one market. Aggregate
        # market P&L and stake before estimating the mean return or its SE.
        st_market = st.groupby("market_id", as_index=False).agg(
            pnl=("pnl", "sum"), stake=("stake", "sum"),
            entry_ts=("entry_ts", "min"), settled_ts=("settled_ts", "max"),
            days_to_start=("days_to_start", "first"))
        st_market["ret"] = st_market["pnl"] / st_market["stake"]
        k = float(st_market["ret"].mean())
        se_k = float(st_market["ret"].std(ddof=1) / np.sqrt(len(st_market)))
        # ★ 2026-10-10 修正：原用 days_to_end（市场到期日）当持有期，
        #   得到 "n=360 次/年"，把 k*n 放大成天文数字（-16311%），完全没有意义。
        #   持有期应当 = 结算时刻 - 入场时刻。
        hold = None
        try:
            t0 = pd.to_datetime(st_market["entry_ts"], errors="coerce", utc=True)
            t1 = pd.to_datetime(st_market["settled_ts"], errors="coerce", utc=True)
            hh = (t1 - t0).dt.total_seconds() / 86400.0
            hh = hh[(hh > 0) & (hh < 30)]
            if len(hh):
                hold = float(hh.mean())
        except Exception:
            hold = None
        if hold is None:
            hold = float(st_market["days_to_start"].fillna(1.0).mean()) + 0.25
        n_turn = 365.0 / max(hold, 0.25)
        log("  ★ 实测（按 %d 个独立市场聚合，已结算票数 %d）:" %
            (len(st_market), len(st)))
        log("     k = 每注净收益率 = %+.4f%%  (SE=%.4f%%, t=%.2f)"
            % (k * 100, se_k * 100, k / se_k if se_k else float("nan")))
        log("     平均持有期 = %.2f 天  ⇒ n = %.1f 次/年" % (hold, n_turn))
        log("     k*n = %.1f%%" % (k * n_turn * 100))
        log("     ⚠ 该年化换算仍是小样本方向性展示；判断 edge 请结合聚类区间。")
        for rf in (0.04, 0.05):
            ex = k * n_turn - rf
            log("     r_f=%.1f%% ⇒ k*n - r_f = %.1f%%" % (rf * 100, ex * 100))
            if ex > 0:
                for F in (600, 1200, 3600):
                    log("        F=$%d/年 ⇒ C_min = $%.0f" % (F, F / ex))
            else:
                log("        **任何资金都不盈利**（k*n <= r_f）")
    else:
        log("  还没有已结算的纸面交易，无法实测 k。")
        log("")
        log("  先验（来自建模的干净对照，仅供规划，不可当真）：")
        log("     价格档校准（剔除已定型样本后）:")
        log("       .15-.20  市场价 0.1735  实际 0.2487  偏差 +7.5pp  t=3.47")
        log("       .20-.30  市场价 0.2522  实际 0.3418  偏差 +9.0pp  t=6.40")
        log("       .90-1.00 市场价 0.9544  实际 0.8333  偏差 -12.1pp t=-2.98")
        log("")
        log("     * 但历史 OOS 的 4.31 分/股 是在 longshot 偏差 11.7pp 的时期测到的，")
        log("       而全期均值只有 6.84pp。保守规划应用 ~1.9 分/股，不是 4.31 分。")
        log("       本模拟的目的就是验证这个数 —— 不是复述历史高位。")
        log("")
        log("     ! 成本：模型假设「费率 + 半价差 0.5 分」，")
        log("       而纸面第一轮实测半价差中位 1.0 分（价差 2 分，不是一个 tick）。")
        log("       实际成本约为模型假设的 1.34 倍，需据此下调净 edge 预期。")

    log("")
    log("=" * 100)
    log("4. 还差什么？")
    log("=" * 100)
    if len(obs):
        log("  · 观测市场数: %d（目标：几百个以上才能谈统计显著）" % obs["market_id"].nunique())
        need = 865
        log("  · 历史研究给出的目标样本量 ≈ %d 个已结算市场" % need)

    # 参照价接入状态（★ 2026-10-10 修正：原文案写死的"尚未接入"已经过期）
    try:
        n_ref = int(obs["ref_fair"].notna().sum()) if len(obs) else 0
    except Exception:
        n_ref = 0
    if n_ref > 0:
        log("  · 外部参照价（Pinnacle de-vig）**已接入** ——")
        log("    当前有 ref_fair 的观测行 = %d 行，可做'参照价 vs 市场价'检验。" % n_ref)
        log("    ⚠ 但注意：参照价只在已下注之后才回填，bets 表里 ref_fair 仍是空的，")
        log("      若要检验'入场时参照价是否也认为便宜'，需要把观测的 ref_fair 回填到 bets。")
    else:
        log("  · 外部参照价（Pinnacle de-vig）尚未接入。")

    # 结算健康度（★ 新增：结算失败会让整个实验静默归零）
    if len(st) == 0 and len(bets):
        n_started = int((bets["days_to_start"] <= 0).sum()) if "days_to_start" in bets else 0
        log("  · ⚠ 已结算 0 笔。若已有大量注过了开赛时间，请优先怀疑结算链路，")
        log("    而不是'比赛还没踢完'。（2026-10-10 就踩过：见 settle.fetch_resolution 注释）")

    conn.close()
    return "\n".join(L)


def save_report(text, name):
    import os
    p = os.path.join(C.OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


if __name__ == "__main__":
    t = report()
    print(save_report(t, "report_latest.txt"))
