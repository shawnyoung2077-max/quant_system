"""
settle.py - 结算：查已下注/已观测市场的最终结果，计算 P&L
============================================================================
结算逻辑：
  Polymarket 的 outcomePrices 形如 "[1, 0]"（YES 赢）或 "[0, 1]"（NO 赢）。
  也有 "[0.5, 0.5]" 之类（未结算或被否定），以及极少数会返回空。

  P&L（买 side、成交价 px、份额 shares = stake/px）：
    赢： gross = shares x 1
    输： gross = 0
    fee = shares x feeRate x px x (1-px)      （官方公式，Taker）
    pnl = gross - stake - fee

  ⚠️ 注意把"未结算"和"结算为 0"区分开 —— 两者 outcomePrices 都是 0 开头，
     但前者不该被当成亏损。判据用 closed 字段。
"""

import sys
import time
import json
import datetime as dt

from . import config as C
from . import db as D
from .scan import _get, calc_fee


def _pick_market(j):
    """从 gamma 返回值里取出一条 market dict（兼容 list / dict 两种返回）。"""
    if isinstance(j, list):
        return j[0] if j else None
    if isinstance(j, dict) and j.get("id"):
        return j
    return None


def fetch_resolution(market_id):
    """
    返回 (closed: bool, outcome_yes: int|None, raw)。

    ★★★ BUG 修复 2026-10-10 ★★★
      gamma-api 的 GET /markets?id=<id> **默认只返回未关闭的市场**
      （等价于隐式 closed=false）。市场一旦结算，这条查询恒返回 []，
      于是本函数返回 closed=None → settle_bets 把它当成"未到期"，
      **结算率永远是 0**。

      实测证据（2026-10-10 07:02 UTC，56 笔注的开赛时间已过）：
        GET /markets?id=5256984               -> []            （空）
        GET /markets?id=5256984&closed=true   -> 1 条, closed=True, ["0","1"]
        GET /markets/5256984                  -> 1 条, closed=True, ["0","1"]
      后果：17 次运行 n_settled 全部为 0，实验一个数据点都拿不到。

      修复：改用路径式 /markets/<id>（不依赖 closed 默认值），
      并保留两条退路。改完必须回归：settle 后 n_settled 应 ≈ 已开赛注数。
    """
    m = None
    for _url, _params in (
        (C.GAMMA + "/markets/" + str(market_id), None),          # 主路径（推荐）
        (C.GAMMA + "/markets", {"id": market_id, "closed": "true"}),  # 退路 1
        (C.GAMMA + "/markets", {"id": market_id}),               # 退路 2（仅未关闭市场）
    ):
        try:
            m = _pick_market(_get(_url, _params))
        except Exception:
            m = None
        if m:
            break
    if not m:
        return None, None, None

    closed = bool(m.get("closed"))
    op = m.get("outcomePrices")
    oy = None
    if closed and op:
        try:
            prices = json.loads(op) if isinstance(op, str) else op
            v = float(prices[0])          # 索引 0 = YES
            # 防误判：作废/取消的市场常返回 ["0.5","0.5"]，
            # 若按 v>0.5 判则会当成 YES 输 → 凭空造出一笔亏损。必须排除。
            if abs(v - 0.5) < 1e-6:
                return closed, None, m     # 已关闭但无明确结果 → 作废，不结算
            oy = 1 if v > 0.5 else 0
        except Exception:
            oy = None
    return closed, oy, m


def settle_bets(verbose=True, sleep=0.0, workers=8):
    """
    结算所有 open 的注。

    ★ 性能：原先逐笔串行 + 每笔 sleep 0.25s，100 笔要 25 秒以上的纯等待。
      改成 8 线程并发（默认 sleep=0）。结算幂等（只按 market_id 查询），
      并发不会造成竞态。
    """
    from concurrent.futures import ThreadPoolExecutor

    conn = D.connect()
    rows = conn.execute(
        "SELECT bet_id, market_id, side, entry_price, shares, stake FROM bets "
        "WHERE status='open'").fetchall()

    def one(r):
        closed, oy, _ = fetch_resolution(r["market_id"])
        return r, closed, oy

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, rows))

    n_settled = 0
    n_pending = 0
    for r, closed, oy in results:
        if closed is None or not closed or oy is None:
            n_pending += 1
            continue
        px = float(r["entry_price"])
        shares = float(r["shares"])
        stake = float(r["stake"])
        fee = calc_fee(px, shares)
        side = r["side"]
        won = (oy == 0) if side == "NO" else (oy == 1)
        gross = shares * 1.0 if won else 0.0
        pnl = gross - stake - fee
        conn.execute(
            "UPDATE bets SET status='settled', outcome_yes=?, payout=?, fee=?, pnl=?, "
            "settled_ts=? WHERE bet_id=?",
            (oy, gross, fee, pnl, D.now_iso(), r["bet_id"]))
        n_settled += 1
        if verbose:
            print("  [settle] %s side=%s px=%.3f yes=%d -> %s pnl=%+.4f"
                  % (r["market_id"], side, px, oy, "WIN" if won else "LOSS", pnl),
                  flush=True)
    conn.commit()
    if verbose:
        print("  [settle] 已结算 %d 笔，未到期 %d 笔" % (n_settled, n_pending),
              flush=True)
    if n_settled:
        D.log_run(conn, "settle", n_settled=n_settled)
    conn.close()
    return n_settled


if __name__ == "__main__":
    print("settled:", settle_bets())
