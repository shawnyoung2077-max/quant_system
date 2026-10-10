"""
equity.py —— 净值曲线与真实回撤
============================================================================
为什么需要它：

2026-10-10 用户问「怎么第一天就回撤 25%？」。查下来发现两件事：

  1) 那个 25% 根本不是回撤，是「总盈亏 ÷ 本金」被当成了回撤。
     同一笔 $253.62 的亏损：本金填 $1000 就是 -25.36%，
     填 $3000 就是 -8.45%，填 $560（实际投入）就是 -45.29%。
     三个数，同样的钱 —— 说明当时的指标在数学上就是空的。

  2) 更根本的是：**当时系统里没有任何净值记录**，
     所以「回撤」在技术上根本无法测量。

本模块补上这一环：
  · 每个快照日记录 已实现盈亏 / 未实现盈亏 / 在场敞口 / 净值
  · 维护峰值与回撤，给出真正的「最大回撤」定义

定义（写清楚，避免又出现口径漂移）：
    cash          = 本金 + 已实现盈亏 - 在场敞口        （没在场上的钱）
    open_stake    = 所有 open 注的投入合计              （在场上的钱，也是最大损失）
    unrealized    = Σ 每笔 open 注 (shares x 当前中间价 - 投入)
    equity        = cash + open_stake + unrealized
                  = 本金 + 已实现盈亏 + 未实现盈亏
    peak          = equity 的历史最高
    drawdown      = equity / peak - 1                   （<= 0）

注意：未实现盈亏需要该市场的**当前中间价**。已关闭市场不再出现在扫描里，
所以取该市场最后一次观测的 mid；完全没有观测的用入场中间价（即视为无变动），
并在返回里报告有多少笔是"用入场价顶替"的，避免悄悄乐观或悲观。
"""

import sys

from . import config as C
from . import db as D

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def compute(conn=None):
    """按当前数据库状态算一次净值快照，返回 dict（不写库）。"""
    own = conn is None
    conn = conn or D.connect()

    realized = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) s, COUNT(*) n FROM bets WHERE status='settled'"
    ).fetchone()

    open_rows = conn.execute(
        "SELECT bet_id, market_id, shares, stake, entry_mid, entry_price "
        "FROM bets WHERE status='open'").fetchall()

    # 每个 open 市场的最后一次观测中间价
    last_mid = {}
    for r in conn.execute(
            "SELECT market_id, mid FROM observations o WHERE snap_ts = "
            "(SELECT MAX(snap_ts) FROM observations x WHERE x.market_id=o.market_id)"):
        last_mid[r["market_id"]] = r["mid"]

    open_stake = 0.0
    unreal = 0.0
    n_priced = 0
    n_fallback = 0
    for r in open_rows:
        st = float(r["stake"] or 0.0)
        sh = float(r["shares"] or 0.0)
        open_stake += st
        mid = last_mid.get(r["market_id"])
        if mid is None:
            mid = r["entry_mid"] if r["entry_mid"] is not None else r["entry_price"]
            n_fallback += 1
        else:
            n_priced += 1
        if mid is not None:
            unreal += sh * float(mid) - st
        else:
            n_fallback += 1

    realized_pnl = float(realized["s"] or 0.0)
    equity = C.PAPER_BANKROLL + realized_pnl + unreal
    cash = C.PAPER_BANKROLL + realized_pnl - open_stake

    prev = conn.execute(
        "SELECT peak FROM equity ORDER BY snap_date DESC LIMIT 1").fetchone()
    prev_peak = float(prev["peak"]) if prev and prev["peak"] is not None else C.PAPER_BANKROLL
    peak = max(prev_peak, equity)

    out = {
        "n_open": len(open_rows),
        "open_stake": round(open_stake, 4),
        "exposure_pct": round(open_stake / C.PAPER_BANKROLL, 6) if C.PAPER_BANKROLL else None,
        "cash": round(cash, 4),
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unreal, 4),
        "equity": round(equity, 4),
        "peak": round(peak, 4),
        "drawdown": round(equity / peak - 1.0, 6) if peak else None,
        "n_unreal_priced": n_priced,
        "n_unreal_fallback": n_fallback,
        "n_settled": int(realized["n"] or 0),
    }
    if own:
        conn.close()
    return out


def snapshot(conn=None):
    """把当前净值写入 equity 表（同一 snap_date 覆盖）。返回写入的 dict。"""
    own = conn is None
    conn = conn or D.connect()
    s = compute(conn)
    conn.execute(
        "INSERT INTO equity(snap_date,snap_ts,n_open,open_stake,cash,realized_pnl,"
        "unrealized_pnl,equity,peak,drawdown,n_unreal_priced,n_unreal_fallback,"
        "n_settled) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(snap_date) DO UPDATE SET "
        "snap_ts=excluded.snap_ts, n_open=excluded.n_open, open_stake=excluded.open_stake,"
        "cash=excluded.cash, realized_pnl=excluded.realized_pnl,"
        "unrealized_pnl=excluded.unrealized_pnl, equity=excluded.equity,"
        "peak=excluded.peak, drawdown=excluded.drawdown,"
        "n_unreal_priced=excluded.n_unreal_priced,"
        "n_unreal_fallback=excluded.n_unreal_fallback, n_settled=excluded.n_settled",
        (D.today(), D.now_iso(), s["n_open"], s["open_stake"], s["cash"],
         s["realized_pnl"], s["unrealized_pnl"], s["equity"], s["peak"],
         s["drawdown"], s["n_unreal_priced"], s["n_unreal_fallback"], s["n_settled"]))
    conn.commit()
    if own:
        conn.close()
    return s


def max_drawdown(conn=None):
    """
    返回 (最大回撤, 峰值日期, 谷值日期, 快照数)。
    只基于 equity 表的逐日快照 —— 快照太少时不要下结论。
    """
    own = conn is None
    conn = conn or D.connect()
    rows = conn.execute(
        "SELECT snap_date, equity, peak, drawdown FROM equity ORDER BY snap_date").fetchall()
    if own:
        conn.close()
    if not rows:
        return None
    mdd = min(float(r["drawdown"] or 0.0) for r in rows)
    trough = min(rows, key=lambda r: float(r["drawdown"] or 0.0))
    # 峰值日 = 谷值之前 equity 最高的那天
    best, best_d = None, None
    for r in rows:
        if r["snap_date"] > trough["snap_date"]:
            break
        e = float(r["equity"] or 0.0)
        if best is None or e > best:
            best, best_d = e, r["snap_date"]
    return {"max_drawdown": mdd, "peak_date": best_d,
            "trough_date": trough["snap_date"], "n_snapshots": len(rows)}


def risk_report():
    """给报告用的一段文字。"""
    s = compute()
    L = []
    L.append("  本金 $%.0f   在场敞口 $%.2f（%.1f%% 本金，上限 %.0f%%）%d 笔在场"
             % (C.PAPER_BANKROLL, s["open_stake"], (s["exposure_pct"] or 0) * 100,
                C.MAX_EXPOSURE_PCT * 100, s["n_open"]))
    L.append("  已实现盈亏 $%+.2f   未实现盈亏 $%+.2f   净值 $%.2f"
             % (s["realized_pnl"], s["unrealized_pnl"], s["equity"]))
    md = max_drawdown()
    if md and md["n_snapshots"] >= 3:
        L.append("  最大回撤 %.2f%%（峰值 %s → 谷值 %s，%d 个快照日）"
                 % (md["max_drawdown"] * 100, md["peak_date"], md["trough_date"],
                    md["n_snapshots"]))
    else:
        L.append("  最大回撤：**还测不了** —— 目前只有 %d 个快照日（至少需要 3 个）。"
                 % (md["n_snapshots"] if md else 0))
    if s["n_unreal_fallback"]:
        L.append("  （未实现盈亏中 %d 笔没有当前报价，用入场价顶替）"
                 % s["n_unreal_fallback"])
    return "\n".join(L)
