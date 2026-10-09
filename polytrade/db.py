"""
db.py - 纸面交易的存储层（SQLite）
============================================================================
两张表，对应 __init__ 里说的两层设计：

  observations  观测日志：每个合格市场在每个快照日的价格。
                **不含任何方向决策** ⇒ 不会被我的偏见污染，
                可以事后对任意策略规则回测。
  bets          纸面账本：按显式规则模拟的下注，含结算与 P&L。

为什么要 observations 表（而不只记 bets）：
  如果只记 bets，那我就只观测到"我决定下注的那一小部分"。
  而没有下注的那些恰恰是判断"我的规则是否选对了"的对照组。
  记录全部合格市场 ⇒ 可以构造对照组 ⇒ 能真正检验规则。
"""

import os
import sqlite3
import datetime as dt

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    obs_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date     TEXT NOT NULL,          -- 快照日期 YYYY-MM-DD
    snap_ts       TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    slug          TEXT,
    question      TEXT,
    league        TEXT,
    market_type   TEXT,
    group_title   TEXT,
    bid           REAL,                   -- YES 买一
    ask           REAL,                   -- YES 卖一
    mid           REAL,
    spread        REAL,
    days_to_end   REAL,
    days_to_start REAL,                   -- 距【开赛】天数（判赛前必须用它）
    game_start    TEXT,
    end_date      TEXT,
    vol24         REAL, volnum REAL, liq REAL,
    is_cold       INTEGER,                -- 是否冷门联赛
    ref_fair      REAL,                   -- 外部参照的公允概率（可空）
    ref_source    TEXT,
    neg_risk      INTEGER,
    fee_type      TEXT,
    UNIQUE(market_id, snap_date)
);
CREATE INDEX IF NOT EXISTS idx_obs_market ON observations(market_id);
CREATE INDEX IF NOT EXISTS idx_obs_date   ON observations(snap_date);

CREATE TABLE IF NOT EXISTS bets (
    bet_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT,                   -- 同一市场的不同规则可各下一注
    league        TEXT,
    question      TEXT,
    side          TEXT,                   -- 'YES' / 'NO'
    entry_price   REAL,                   -- 实际成交价（含跨价差）
    entry_mid     REAL,                   -- 当时中间价（用于事后归因）
    shares        REAL,
    stake         REAL,
    entry_ts      TEXT,
    entry_date    TEXT,
    days_to_end   REAL,
    days_to_start REAL,                   -- 入场时距开赛天数（用于分时点评估）
    game_start    TEXT,
    rule_lo       REAL,                   -- 命中规则的区间（用于事后归因）
    rule_hi       REAL,
    rule_note     TEXT,
    event_key     TEXT,                   -- 赛事级聚类键（联赛|开赛日）
    ref_fair      REAL,
    edge_at_entry REAL,                   -- 参照公允 - 成交价（YES 口径）
    status        TEXT DEFAULT 'open',    -- open / settled
    outcome_yes   INTEGER,                -- Yes 的最终结果 1/0
    payout        REAL,
    fee           REAL,
    pnl           REAL,
    settled_ts    TEXT
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts      TEXT, kind TEXT,
    n_scanned   INTEGER, n_new_obs INTEGER, n_new_bets INTEGER,
    n_settled   INTEGER,
    note        TEXT
);
"""


def connect(path=None):
    p = path or C.DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def log_run(conn, kind, n_scanned=0, n_new_obs=0, n_new_bets=0, n_settled=0, note=""):
    conn.execute(
        "INSERT INTO runs(run_ts,kind,n_scanned,n_new_obs,n_new_bets,n_settled,note)"
        " VALUES(?,?,?,?,?,?,?)",
        (now_iso(), kind, n_scanned, n_new_obs, n_new_bets, n_settled, note))
    conn.commit()


def upsert_observation(conn, row):
    """插入观测；同 (market_id, snap_date) 已存在则更新价格（保留最新）。"""
    cols = ["snap_date", "snap_ts", "market_id", "slug", "question", "league",
            "market_type", "group_title", "bid", "ask", "mid", "spread",
            "days_to_end", "days_to_start", "game_start", "end_date",
            "vol24", "volnum", "liq", "is_cold",
            "ref_fair", "ref_source", "neg_risk", "fee_type"]
    vals = [row.get(c) for c in cols]
    ph = ",".join("?" * len(cols))
    conn.execute(
        "INSERT INTO observations(%s) VALUES(%s) "
        "ON CONFLICT(market_id, snap_date) DO UPDATE SET "
        "snap_ts=excluded.snap_ts, bid=excluded.bid, ask=excluded.ask, "
        "mid=excluded.mid, spread=excluded.spread, "
        "days_to_end=excluded.days_to_end, days_to_start=excluded.days_to_start, "
        "vol24=excluded.vol24, volnum=excluded.volnum, liq=excluded.liq, "
        "ref_fair=excluded.ref_fair, ref_source=excluded.ref_source"
        % (",".join(cols), ph), vals)
