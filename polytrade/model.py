"""
model.py - 定价模型 + 信号 + 仓位 + 逐日盯市
============================================================================
用户要的："假装下注了，然后每天计算价格" —— 所以必须逐日盯市，
不能只等结算。本模块提供完整的三层：

  L1 公允价    Pinnacle 三向赔率 de-vig（三种方法）
  L2 信号      公允价 vs Polymarket 报价 → edge / 方向 / 一致性
  L3 仓位      分数 Kelly + 上限
  L4 盯市      每日记录价格，算浮动盈亏

═══════════════════════════════════════════════════════════════════════════
★ 模型的关键约束：为什么必须要求"三方法一致"
═══════════════════════════════════════════════════════════════════════════
实测（本项目的 de-vig 验算）：
     势均力敌 (抽水 0.6%)  三方法分歧 0.0001
     绝大热门 (抽水 8.7%)  三方法分歧 0.055   ← 比我们要追的 edge(2~3pp) 还大

⇒ 如果只用一种 de-vig 方法，你测到的"edge"可能完全是方法选择造成的。
  所以本模块默认要求 **三种方法对方向一致** 才出信号。
  这不是保守，而是：在分歧比信号还大的地方，那个信号不可信。

★ 为什么用分数 Kelly 而不是满 Kelly
  我们的 p（公允概率）本身是估计值，有误差。满 Kelly 假设 p 精确已知，
  在 p 有误差时会系统性超配。用 1/4 Kelly + 硬上限是标准做法。
"""

import numpy as np
import datetime as dt

from . import config as C
from . import db as D

# ---- 模型参数（集中在此，便于审计）----
MIN_NET_EDGE = 0.020      # 扣完成本后的最小 edge（2 个百分点）
KELLY_FRACTION = 0.25     # 分数 Kelly
MAX_STAKE_FRAC = 0.02     # 单注不超过本金的 2%
REQUIRE_CONSENSUS = True  # 要求三种 de-vig 方法方向一致


# ===========================================================================
# L2 信号
# ===========================================================================
def evaluate(pm_bid, pm_ask, fair_mult, fair_power, fair_shin,
             fee_rate=None, min_edge=None, require_consensus=None):
    """
    给定 Polymarket 报价与三种 de-vig 公允概率，判断是否有可下注的 edge。

    pm_bid / pm_ask : Polymarket 的 YES 买一 / 卖一
    fair_*          : 三种方法给出的【YES 公允概率】

    返回 dict：
      side          'YES' / 'NO' / None
      entry_price   实际成交价（含跨价差）
      fair         用哪一方的公允概率
      raw_edge     成交价口径的毛 edge
      net_edge     扣掉平台费后的净 edge
      methods_agree 三种方法是否方向一致
      detail       每种方法各自的 edge（用于审计）
    """
    fr = C.SPORTS_FEE_RATE if fee_rate is None else fee_rate
    me = MIN_NET_EDGE if min_edge is None else min_edge
    rc = REQUIRE_CONSENSUS if require_consensus is None else require_consensus

    if not all(np.isfinite([pm_bid, pm_ask, fair_mult, fair_power, fair_shin])):
        return {"side": None, "reason": "输入含 NaN"}
    if not (0 < pm_bid < pm_ask < 1):
        return {"side": None, "reason": "报价异常"}

    fairs = {"mult": fair_mult, "power": fair_power, "shin": fair_shin}

    # --- 买 YES：付 ask，公允 = fair ---
    yes_detail = {k: v - pm_ask for k, v in fairs.items()}
    # --- 买 NO：付 1-bid，公允 = 1-fair ---
    no_detail = {k: pm_bid - v for k, v in fairs.items()}

    # 方向一致性：三种方法是否都指向同一侧且为正
    def agree(d):
        vals = list(d.values())
        return all(v > 0 for v in vals)

    yes_ok = agree(yes_detail)
    no_ok = agree(no_detail)

    if yes_ok and no_ok:
        return {"side": None, "reason": "两方向同时为正（不应发生）", "detail": {
            "yes": yes_detail, "no": no_detail}}
    if not yes_ok and not no_ok and rc:
        # 找一个"最不坏"的用于报告，但不出信号
        best_yes = min(yes_detail.values())
        best_no = min(no_detail.values())
        return {"side": None, "reason": "三方法方向不一致",
                "best_yes_min_edge": best_yes, "best_no_min_edge": best_no,
                "detail": {"yes": yes_detail, "no": no_detail}}

    if yes_ok:
        side = "YES"
        entry = pm_ask
        fair = float(np.median(list(fairs.values())))   # 三方法取中位
        detail = yes_detail
    else:
        side = "NO"
        entry = 1.0 - pm_bid
        fair_no = 1.0 - float(np.median(list(fairs.values())))
        fair = fair_no
        detail = no_detail

    raw_edge = fair - entry
    # 官方费率：fee = C * rate * p * (1-p)，按每股即 rate*entry*(1-entry)
    fee_ps = fr * entry * (1.0 - entry)
    net_edge = raw_edge - fee_ps

    if net_edge < me:
        return {"side": None, "reason": "净 edge %.4f < 门槛 %.4f" % (net_edge, me),
                "side_would_be": side, "net_edge": net_edge, "detail": detail}

    return {"side": side, "entry_price": float(entry), "fair": float(fair),
            "raw_edge": float(raw_edge), "fee_per_share": float(fee_ps),
            "net_edge": float(net_edge), "methods_agree": True, "detail": detail}


# ===========================================================================
# L3 仓位
# ===========================================================================
def kelly_fraction(edge, price, fraction=None, cap=None):
    """
    二元合约的 Kelly 比例（占本金）。

    推导：以价格 q 买入、真实胜率 p、净赔率 b=(1-q)/q
         f* = (p(b+1) - 1)/b = (p - q)/(1 - q)

    edge 传【净 edge】(= p - q)，所以 f_full = edge/(1-q)。
    再做分数 Kelly 与上限裁剪。
    """
    fraction = KELLY_FRACTION if fraction is None else fraction
    cap = MAX_STAKE_FRAC if cap is None else cap
    if price <= 0 or price >= 1 or edge <= 0:
        return 0.0
    f_full = edge / (1.0 - price)
    return float(min(max(0.0, f_full * fraction), cap))


def size_position(bankroll, edge, price, **kw):
    f = kelly_fraction(edge, price, **kw)
    return float(bankroll * f)


# ===========================================================================
# L4 逐日盯市
# ===========================================================================
MARKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS marks (
    mark_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id    INTEGER NOT NULL,
    mark_date TEXT NOT NULL,
    mark_ts   TEXT NOT NULL,
    cur_mid   REAL,          -- 当日的 Polymarket 中间价（YES 口径）
    cur_side_price REAL,     -- 换算成我们所持方向的市价
    unrealized REAL,         -- 浮动盈亏（可为负）
    UNIQUE(bet_id, mark_date)
);
CREATE INDEX IF NOT EXISTS idx_marks_bet ON marks(bet_id);
"""


def ensure_marks_schema(conn):
    conn.executescript(MARKS_SCHEMA)
    conn.commit()


def side_price_from_mid(side, mid):
    """Polymarket 的 mid 是 YES 口径；换算成我们所持方向的市价。"""
    return float(mid) if side == "YES" else 1.0 - float(mid)


def mark_to_market(conn, verbose=True):
    """
    对所有 open 的注，用当日观测里的最新价格做盯市，写入 marks 表。

    浮动盈亏 = 份额 × (当前市价 - 入场价)
      （份额 = stake / 入场价，所以等价于 stake × (当前价/入场价 - 1)）
    """
    ensure_marks_schema(conn)
    today = D.today()
    ts = D.now_iso()
    rows = conn.execute(
        "SELECT bet_id, market_id, side, entry_price, shares, stake FROM bets "
        "WHERE status='open'").fetchall()
    n = 0
    for r in rows:
        o = conn.execute(
            "SELECT mid FROM observations WHERE market_id=? "
            "ORDER BY snap_ts DESC LIMIT 1", (r["market_id"],)).fetchone()
        if not o or o["mid"] is None:
            continue
        cur = side_price_from_mid(r["side"], o["mid"])
        unrl = float(r["shares"]) * (cur - float(r["entry_price"]))
        conn.execute(
            "INSERT INTO marks(bet_id,mark_date,mark_ts,cur_mid,cur_side_price,unrealized)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(bet_id,mark_date) DO UPDATE SET "
            "mark_ts=excluded.mark_ts, cur_mid=excluded.cur_mid, "
            "cur_side_price=excluded.cur_side_price, unrealized=excluded.unrealized",
            (r["bet_id"], today, ts, o["mid"], cur, unrl))
        n += 1
    conn.commit()
    if verbose:
        print("  [mtm] 盯市 %d 笔" % n, flush=True)
    return n


def portfolio_snapshot(conn, bankroll=None):
    """
    账户快照：已实现盈亏 + 浮动盈亏 + 净值。
    """
    bk = C.PAPER_BANKROLL if bankroll is None else bankroll
    ensure_marks_schema(conn)
    real = conn.execute("SELECT COALESCE(SUM(pnl),0) s FROM bets WHERE status='settled'"
                        ).fetchone()["s"]
    # 每笔 open 注取最新一次盯市
    unrl = conn.execute(
        "SELECT COALESCE(SUM(m.unrealized),0) s FROM marks m "
        "JOIN (SELECT bet_id, MAX(mark_date) md FROM marks GROUP BY bet_id) t "
        "  ON m.bet_id=t.bet_id AND m.mark_date=t.md "
        "JOIN bets b ON b.bet_id=m.bet_id AND b.status='open'").fetchone()["s"]
    n_open = conn.execute("SELECT COUNT(*) c FROM bets WHERE status='open'"
                          ).fetchone()["c"]
    n_set = conn.execute("SELECT COUNT(*) c FROM bets WHERE status='settled'"
                         ).fetchone()["c"]
    open_stake = conn.execute(
        "SELECT COALESCE(SUM(stake),0) s FROM bets WHERE status='open'").fetchone()["s"]
    return {"bankroll": bk, "realized": float(real), "unrealized": float(unrl),
            "equity": bk + float(real) + float(unrl),
            "n_open": n_open, "n_settled": n_set,
            "open_stake": float(open_stake)}


# ===========================================================================
# 组合信号（对接 observations 与 odds_fixtures）
# ===========================================================================
def build_signal_from_db(conn, market_id, verbose=False):
    """
    从 DB 里取一个市场的报价 + 匹配到的参照价，产出信号。
    匹配需要外部先把 odds_fixtures 与 market 关联（见 match.py），
    这里只做"给定参照价"的纯计算。
    """
    o = conn.execute(
        "SELECT bid, ask, mid FROM observations WHERE market_id=? "
        "ORDER BY snap_ts DESC LIMIT 1", (market_id,)).fetchone()
    f = conn.execute(
        "SELECT fair_mult, fair_power, fair_shin FROM odds_fixtures WHERE market_id=? "
        "ORDER BY fetched_ts DESC LIMIT 1", (market_id,)).fetchone()
    if not o or not f:
        return None
    return evaluate(o["bid"], o["ask"], f["fair_mult"], f["fair_power"], f["fair_shin"])
