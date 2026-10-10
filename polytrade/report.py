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

    k 的定义：k = 实际胜率 - 市场隐含概率（即每股的期望收益，单位：元/股）。
    返回 dict，含 naive / 去重 / 聚类 三套 (n, k, se, t)。

    注意：本函数只报「有没有信号」，不代表已经确认 edge。样本量远不够。
    """
    if st is None or len(st) == 0:
        return None
    d = st.dropna(subset=["entry_price", "outcome_yes"]).copy()
    if len(d) == 0:
        return None
    d["outcome_yes"] = d["outcome_yes"].astype(float)
    out = {}

    def _k(gr):
        p = float(gr["entry_price"].mean())
        o = float(gr["outcome_yes"].mean())
        return o - p

    # (a) 按笔（错的，仅作对照）
    n = len(d)
    out["naive"] = {"n": n, "k": _k(d),
                    "se": float(d["outcome_yes"].std(ddof=1) / np.sqrt(n))}

    # (b) 按市场去重：同一市场只保留第一笔
    u = d.sort_values("entry_ts").groupby("market_id", as_index=False).first()
    nu = len(u)
    out["per_market"] = {"n": nu, "k": _k(u),
                         "se": float(u["outcome_yes"].std(ddof=1) / np.sqrt(nu))}

    # (c) 按 联赛 x 比赛日 聚类（簇内先平均，再对簇做 t 检验）
    uu = u.copy()
    uu["_day"] = uu["game_start"].astype(str).str[:10]
    uu["_dev"] = uu["outcome_yes"] - uu["entry_price"]
    cl = uu.groupby(["league", "_day"])["_dev"].mean()
    nc = len(cl)
    if nc >= 2:
        out["cluster"] = {"n": nc, "k": float(cl.mean()),
                          "se": float(cl.std(ddof=1) / np.sqrt(nc))}

    for v in out.values():
        v["t"] = v["k"] / v["se"] if v["se"] and v["se"] > 0 else float("nan")
        if v["se"] and v["se"] > 0:
            v["ci"] = (v["k"] - 1.96 * v["se"], v["k"] + 1.96 * v["se"])
        else:
            v["ci"] = (float("nan"), float("nan"))
    out["dup_ratio"] = 1 - nu / n if n else 0.0
    return out


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

    # ---------------- 纸面 P&L ----------------
    st = bets[bets["status"] == "settled"] if len(bets) else pd.DataFrame()
    if len(st):
        tot = float(st["pnl"].sum())
        log("")
        log("=" * 100)
        log("1. 纸面盈亏")
        log("=" * 100)
        log("  本金基准 = $%.0f（单注 $%.0f）" % (C.PAPER_BANKROLL, C.STAKE))
        log("  已结算 %d 笔，总盈亏 = $%+.2f  (%.2f%% of 本金)"
            % (len(st), tot, tot / C.PAPER_BANKROLL * 100))
        log("  胜率 = %.1f%%   平均每注 = $%+.4f"
            % (float((st["payout"] > 0).mean()) * 100, tot / len(st)))
        log("  平均投入 = $%.3f   平均回款 = $%.3f"
            % (float(st["stake"].mean()), float(st["payout"].mean())))
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
            log("  ★ 核心：k = 实际胜率 - 市场隐含概率   （正 = 买 YES 有 edge）")
            log("  " + "-" * 92)
            log("     口径            有效n    k          SE       t      95%CI")
            for key, lab in (("naive", "按笔(错)"),
                             ("per_market", "按市场去重"),
                             ("cluster", "联赛x比赛日聚类")):
                v = ea.get(key)
                if not v:
                    continue
                log("     %-14s %5d  %+7.4f  %6.4f  %+6.2f  [%+.4f, %+.4f]"
                    % (lab, v["n"], v["k"], v["se"], v["t"], v["ci"][0], v["ci"][1]))
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
        st["ret"] = st["pnl"] / st["stake"]
        k = float(st["ret"].mean())
        se_k = float(st["ret"].std(ddof=1) / np.sqrt(len(st)))
        # ★ 2026-10-10 修正：原用 days_to_end（市场到期日）当持有期，
        #   得到 "n=360 次/年"，把 k*n 放大成天文数字（-16311%），完全没有意义。
        #   持有期应当 = 结算时刻 - 入场时刻。
        hold = None
        try:
            t0 = pd.to_datetime(st["entry_ts"], errors="coerce", utc=True)
            t1 = pd.to_datetime(st["settled_ts"], errors="coerce", utc=True)
            hh = (t1 - t0).dt.total_seconds() / 86400.0
            hh = hh[(hh > 0) & (hh < 30)]
            if len(hh):
                hold = float(hh.mean())
        except Exception:
            hold = None
        if hold is None:
            hold = float(st["days_to_start"].fillna(1.0).mean()) + 0.25
        n_turn = 365.0 / max(hold, 0.25)
        log("  ★ 实测（来自 %d 笔已结算纸面交易，**未去重，仅供参考**）:" % len(st))
        log("     k = 每注净收益率 = %+.4f%%  (SE=%.4f%%, t=%.2f)"
            % (k * 100, se_k * 100, k / se_k if se_k else float("nan")))
        log("     平均持有期 = %.2f 天  ⇒ n = %.1f 次/年" % (hold, n_turn))
        log("     k*n = %.1f%%" % (k * n_turn * 100))
        log("     ⚠ 这里按笔平均且未去重；同一市场重复下注会放大 |k|。")
        log("       判定 edge 请以上面第 1 节的【按市场去重 / 聚类】口径为准。")
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
