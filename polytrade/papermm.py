"""
papermm.py —— 纸上做市（提供流动性拿官方奖励）的可行性评估
============================================================================
为什么单独做这个模块：

  用户提的方向性策略（买冷门合约）**依赖 edge 为真**，而 edge 目前尚未验证。
  做市赚的是**奖励 + 价差**，不依赖"我们比市场准" —— 这是唯一一条
  不依赖 edge 验证的收益路径。从风险角度，反而是最该先估的。

奖励规则（从 clobRewards 实测，字段含义确定）：
    rewardsDailyRate    该市场每天发多少奖励（美元）
    rewardsMinSize      挂单至少多少【股】才计入
    rewardsMaxSpread    距中间价多少【分】以内才计入

  ⚠ 注意单位：rewardsMaxSpread 是**分**（0.01 元），不是价格。
    实测 Valorant 某市场 rewardsMaxSpread=4.5 而真实盘口是 0.01/0.368
    —— 即盘口宽 36 分，但奖励要求你挂在中间价 ±4.5 分内。
    **这个落差就是逆向选择本身**：想拿奖励，就得挂在真实买盘上方十几分的位置，
    而那个位置只有在别人愿意卖给你的时候才成交 —— 也就是价格在往下走的时候。

本模块量化三件事：
  ① 奖励池有多大（客观，来自 clobRewards）
  ② 要拿到奖励得挂多激进（与真实盘口的距离）
  ③ 被成交后的盈亏（逆向选择）—— 用 2 小时快照序列做保守模拟

③ 的模拟口径（**故意取最坏情况**）：
    在快照 t 以 q = mid_t - d 挂买单（d = rewardsMaxSpread/100）
    若快照 t+1 的 mid <= q，视为成交（价格穿过我们的买单 = 我们被逆向选中）
    成交后一直持有到结算，按最终结果算盈亏
    奖励按挂单时长按比例计入

为什么这个口径是"最坏情况"：真实市场里，价格**在两次快照之间**触到我们的价位
也会成交，而我们看不到。所以这里必然低估成交量 —— 但也因此不会自我美化。

无法估计的部分（必须说清楚）：
    **奖励分成比例。** 官方按"报价分/全场总分"分配奖励池，而总分取决于
    其他做市商挂了多少。我们看不到别人的挂单，所以算不出份额。
    本模块因此只报**池子大小**和"假设拿到 X% 时的收益"，不假装知道份额。
"""

import sys

import numpy as np
import pandas as pd

from . import config as C
from . import db as D

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def load(conn=None):
    """
    取带奖励市场的快照序列。

    ★ 优先用 obs_intraday（小时级）—— 因为 observations 是 (market_id, snap_date)
      唯一，**同一个市场每天只留一条**，日粒度下一个只活 2 天的市场只有
      1 个转换点，做市模拟样本根本不够。
      没有 obs_intraday 时退回 observations，并在报告里注明粒度。
    """
    own = conn is None
    conn = conn or D.connect()
    src = "obs_intraday"
    try:
        n = conn.execute("SELECT COUNT(*) c FROM obs_intraday").fetchone()["c"]
        if n == 0:
            src = "observations"
    except Exception:
        src = "observations"
    if src == "obs_intraday":
        df = pd.read_sql_query(
            "SELECT market_id, snap_ts, mid, bid, ask, spread, days_to_start, game_start,"
            " league, question, rewards_daily, rewards_min_size, rewards_max_spread "
            "FROM obs_intraday WHERE rewards_daily > 0 AND mid IS NOT NULL "
            "ORDER BY market_id, snap_ts", conn)
    else:
        df = pd.read_sql_query(
            "SELECT market_id, snap_ts, mid, bid, ask, spread, days_to_start, game_start,"
            " league, question, rewards_daily, rewards_min_size, rewards_max_spread "
            "FROM observations WHERE rewards_daily IS NOT NULL AND rewards_daily > 0 "
            "AND mid IS NOT NULL ORDER BY market_id, snap_ts", conn)
    if own:
        conn.close()
    df.attrs["src"] = src
    return df


def summarize(df):
    """
    只看奖励池与"挂单激进程度"，不做盈亏模拟。
    这是纯客观的部分，先给出规模感。
    """
    if df.empty:
        return None
    g = df.groupby("market_id").agg(
        league=("league", "first"), question=("question", "first"),
        rewards_daily=("rewards_daily", "max"),
        min_size=("rewards_min_size", "max"),
        max_spread=("rewards_max_spread", "max"),
        mid=("mid", "last"), bid=("bid", "last"), ask=("ask", "last"),
        spread=("spread", "last"), liq=("liq", "last"),
        n_snap=("mid", "size"))
    g["pool_annual"] = g["rewards_daily"] * 365
    # 要拿到奖励，买单价最多能挂到 mid - maxSpread/100；
    # 与"真实最优买价(bid)"的距离 > 0 表示奖励价高于真实买盘 —— 越正越危险
    g["quote_bid"] = g["mid"] - g["max_spread"] / 100.0
    g["vs_real_bid"] = g["quote_bid"] - g["bid"]
    return g


def simulate(df, horizons=(1, 3), d_ticks=None):
    """
    逆向选择模拟。返回 (逐笔明细 DataFrame, 汇总 dict)。

    horizons: 用"下一个快照"还是"下下个"判断成交（间隔 ~2 小时/次）
    d_ticks:  挂单偏离中间价的距离（价格单位）。None = 用该市场的 rewardsMaxSpread/100
    """
    rows = []
    for mid_, sub in df.groupby("market_id"):
        sub = sub.sort_values("snap_ts").reset_index(drop=True)
        if len(sub) < 2:
            continue
        r0 = sub.iloc[0]
        d = (r0["rewards_max_spread"] / 100.0) if d_ticks is None else d_ticks
        if not d or d <= 0 or d >= 0.5:
            continue
        q = float(r0["mid"]) - d          # 我们的买单报价
        if q <= 0.001 or q >= 0.999:
            continue
        size = float(r0["rewards_min_size"] or 20)
        pool = float(r0["rewards_daily"] or 0) / 86400.0   # 每秒奖励

        filled = False
        fill_i = None
        for h in horizons:
            if len(sub) > h:
                if float(sub.iloc[h]["mid"]) <= q:
                    filled = True
                    fill_i = h
                    break
        # 挂单时长（秒）：没成交就一直挂到最后一个快照
        end_i = fill_i if filled else len(sub) - 1
        t0 = pd.to_datetime(sub.iloc[0]["snap_ts"], errors="coerce", utc=True)
        t1 = pd.to_datetime(sub.iloc[end_i]["snap_ts"], errors="coerce", utc=True)
        rest_sec = max(0.0, (t1 - t0).total_seconds()) if pd.notna(t0) and pd.notna(t1) else 0.0

        rows.append({
            "market_id": mid_, "league": r0["league"],
            "question": str(r0["question"])[:60],
            "mid0": float(r0["mid"]), "bid0": float(r0["bid"]),
            "quote": q, "dist": d, "size": size,
            "vs_real_bid": q - float(r0["bid"]),
            "rewards_daily": float(r0["rewards_daily"] or 0),
            "filled": int(filled), "rest_sec": rest_sec,
            "reward_if_full": pool * rest_sec,       # 假设独占整个池子
            "n_snap": len(sub),
            # ★ 逆向选择的直接度量：成交后价格又跌了多少。
            #   买入 q，最后快照的中间价 mid_last —— 差额就是我们的浮亏。
            #   这一项比"成交率"有用得多：做市亏钱不是因为成交多，是因为
            #   **成交之后价格继续往不利方向走**。
            "mid_last": float(sub.iloc[-1]["mid"]),
            "mtm_per_share": (float(sub.iloc[-1]["mid"]) - q) if filled else np.nan,
        })
    det = pd.DataFrame(rows)
    if det.empty:
        return det, {}

    fl = det[det["filled"] == 1]
    # 奖励按挂单时长计；独占是上界，所以同时给几个份额假设
    agg = {
        "n_market": len(det),
        "n_filled": int(len(fl)),
        "pool_daily_all": float(det["rewards_daily"].sum()),
        "pool_daily_median": float(det["rewards_daily"].median()),
        "fill_rate": float(det["filled"].mean()),
        # 挂单报价相对真实买盘的溢价（越正 = 越容易被动成交 = 逆向选择越强）
        "vs_real_bid_median": float(det["vs_real_bid"].median()),
        "rest_hours_median": float(det["rest_sec"].median() / 3600.0),
        "rewards_daily_captured_if_solo": float(det["reward_if_full"].sum() / max(1e-9, 1)),
    }
    if len(fl):
        # 每股浮亏 × 股数 = 一次成交的库存损失
        agg["mtm_per_share_median"] = float(fl["mtm_per_share"].median())
        agg["mtm_loss_sum"] = float(
            (fl["mtm_per_share"] * fl["size"]).sum())
    for share in (0.05, 0.10, 0.25, 1.00):
        agg["reward_at_%.0f%%_share" % (share * 100)] = \
            float(det["rewards_daily"].sum() * share * 365)   # 年化美元
    return det, agg


def report_lines(conn=None, verbose=False):
    conn = conn or D.connect()
    df = load(conn)
    src = df.attrs.get("src", "?")
    unit = "小时级" if src == "obs_intraday" else "日级(退回 observations)"
    L = ["  纸上做市（提供流动性拿官方奖励）—— 不依赖 edge 验证的独立路径"]
    if df.empty:
        L.append("     还没有带奖励字段的快照。奖励字段是 2026-10-12 才接进来的，")
        L.append("     需要等下一次扫描重新观测市场后才有数据。")
        return L

    g = summarize(df)
    L.append("     数据源 %s（%s），覆盖 %d 个市场 / %d 条快照"
             % (src, unit, len(g), len(df)))
    L.append("     ① 奖励池：合计 $%.2f/天（中位 $%.2f/市场/天）"
             % (g["rewards_daily"].sum(), g["rewards_daily"].median()))
    L.append("        最大的几个联赛：")
    bylg = g.groupby("league").agg(n=("rewards_daily", "size"),
                                   pool=("rewards_daily", "sum")).sort_values("pool", ascending=False)
    for lg, r in bylg.head(6).iterrows():
        L.append("          %-26s %3d 个市场  $%.2f/天" % (str(lg)[:24], r["n"], r["pool"]))

    L.append("     ② 挂单激进程度：奖励价 - 真实买价 的中位 = %+.1f 分"
             % (g["vs_real_bid"].median() * 100))
    L.append("        （正值越大，说明为了拿奖励必须挂得比真实买盘高越多，")
    L.append("          也就越容易被逆向选中 —— 这是做市的核心风险）")

    det, agg = simulate(df)
    if agg:
        L.append("     ③ 逆向选择模拟（保守：只用 2 小时快照，看不到盘中触发）")
        L.append("        被成交比例 = %.1f%%（挂单报价相对真实买盘中位 %+.1f 分）"
                 % (agg["fill_rate"] * 100, agg["vs_real_bid_median"] * 100))
        L.append("        挂单中位时长 = %.1f 小时" % agg["rest_hours_median"])
        if agg.get("mtm_per_share_median") is not None:
            L.append("        ★ 被成交后每股浮亏中位 = %+.4f 元（%.1f 分）"
                     % (agg["mtm_per_share_median"], agg["mtm_per_share_median"] * 100))
            L.append("          合计库存浮亏 = $%.2f（%d 次成交）"
                     % (agg["mtm_loss_sum"], agg["n_filled"]))
            L.append("          这一项才是做市的真实成本：不是成交多，而是")
            L.append("          **成交之后价格继续往不利方向走**。")
        L.append("        ⚠ 奖励**分成比例算不出来** —— 官方按「我方报价分/全场总分」")
        L.append("          分配，而总分取决于其他做市商挂了多少，我们看不到。")
        L.append("          所以只给池子规模与不同份额假设下的年化：")
        for share in (0.05, 0.10, 0.25):
            L.append("          假设拿到池子的 %2.0f%% -> 年化 $%,.0f"
                     % (share * 100, agg["reward_at_%.0f%%_share" % (share * 100)]))
        if verbose:
            L.append("        明细（前 10）:")
            for _, r in det.head(10).iterrows():
                L.append("          %s 池$%.1f/天 挂%.3f(d=%+.3f) vs买价%+.3f 成交=%d mtm=%s"
                         % (str(r["question"])[:30], r["rewards_daily"], r["quote"],
                            r["dist"], r["vs_real_bid"], r["filled"],
                            ("%+.3f" % r["mtm_per_share"]) if pd.notna(r["mtm_per_share"]) else "-"))
    else:
        L.append("     ③ 暂时算不了逆向选择：需要同一市场**至少 2 个快照**。")
        L.append("        obs_intraday 是 2026-10-12 才建的，等下一次扫描（每 2 小时）")
        L.append("        写入第二条后，本模块就能开始算。")
    L.append("     ⚠ 这只是可行性初筛。真正的做市需要实时挂撤单，")
    L.append("       我们每 2 小时扫一次，基础设施还差得远。")
    return L


if __name__ == "__main__":
    print("\n".join(report_lines(verbose=True)))
