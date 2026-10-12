"""
aiforecast.py —— 赛前 AI 概率预测：落库 + 自动评估
============================================================================
用户的设想："每次比赛前调用 AI 分析胜率，发现偏差再下注。"

本模块**不实现下注**，只实现两件必须先做成的事：
    1. 赛前把 AI 的概率**带时间戳、不可改地**落库
    2. 赛后用**固定公式**自动评估

为什么必须先做这两件：

  ★ 不落库就等于没有。
    如果赛前不把 AI 概率存下来，事后永远可以说"它其实说对了" ——
    这是自欺的标准路径，而且**做评估的也是 AI**，更容易自我美化。
    所以制度上必须：赛前写死、赛后用固定公式算、人不参与"哪些算数"的判断。

  ★ 已知的三个风险（用数据回答，不靠猜）：
    (1) AI 的概率可能只是市场价的影子 —— LLM 训练数据含赛前共识赔率。
        → 用 corr(ai_prob, market_mid) 回答。>0.95 就说明没有独立信息。
        → 所以 prompt **故意不告诉它市场价格**，否则测的是"它会不会复述"。
    (2) 校准是 LLM 的已知弱项，对极端概率尤其差 ——
        而我们买的恰恰是 0.15~0.30 这种极端档。
        → 用 k_AI = mean(outcome) - mean(ai_prob) 回答。
    (3) AI 的"偏离"可能纯属噪声。
        → 用条件期望回答：mean(outcome - mid | ai_prob - mid > τ)

只有 (3) 显著为正，才说明 AI 有增量价值。三个指标全部自动算，不给人留操作空间。

数据库表 ai_forecasts：
    UNIQUE(market_id)  —— 一个市场只能有一条赛前预测，写进去就改不了。
    另加硬约束：落库时间必须早于 game_start，否则拒收。
"""

import io
import json
import os
import sys
import time

import numpy as np

from . import config as C
from . import db as D

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_forecasts (
    forecast_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id    TEXT NOT NULL UNIQUE,   -- 唯一约束 = 不可事后修改
    made_ts      TEXT NOT NULL,          -- 落库时刻（UTC）
    game_start   TEXT,                   -- 开赛时刻，用于强制 made_ts < game_start
    league       TEXT,
    question     TEXT,
    market_mid   REAL,                   -- 落库时的市场中间价（AI 看不到这个）
    market_ask   REAL,
    ai_prob      REAL,                   -- AI 给出的 YES 概率
    conf         TEXT,
    why          TEXT,
    model        TEXT,
    prompt_ver   TEXT,
    outcome_yes  INTEGER,                -- 事后回填
    resolved_ts  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_fx_market ON ai_forecasts(market_id);
"""

PROMPT_VER = "v1"

SYSTEM_PROMPT = (
    "你是一名体育赛事概率分析师。你的唯一任务是估计某个二元合约成立的概率。"
    "只输出 JSON，不要输出任何其他文字。"
)

USER_TEMPLATE = """请估计下面这个二元合约成立（YES）的概率。

合约问题: {question}
所属联赛: {league}
开赛时间(UTC): {game_start}

要求:
1. 只输出一个 JSON 对象，格式:
   {{"prob": <0到1的小数>, "conf": "low"|"mid"|"high", "why": "<不超过40字>"}}
2. prob 必须是 YES 成立的概率估计。
3. **不要猜测、也不要参考当前市场上的赔率** —— 只依据你对这项赛事和这两支队伍的认知。
4. 若你对这项赛事几乎没有认知，把 conf 设为 "low"，
   并给出接近该类比赛常见基准率的估计（例如足球平局约 0.25）。
5. 若合约是关于具体球员/小分项/冷门统计量的，同样给出你的最佳估计并标 low。
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
def load_key():
    """
    按顺序找 API key：环境变量 -> DSH 凭据文件。
    找不到返回 None（此时只导出队列，不发请求）。
    """
    for n in ("DEEPSEEK_API_KEY", "AI_FORECAST_API_KEY"):
        v = os.environ.get(n)
        if v:
            return v.strip()
    p = C.AI_CREDENTIALS_PATH
    try:
        if p and os.path.exists(p):
            with io.open(p, encoding="utf-8") as fh:
                for line in fh:
                    m = line.strip()
                    if m.startswith("DEEPSEEK_API_KEY:"):
                        return m.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------
def ask_once(question, league, game_start, key=None, timeout=40):
    """
    问一次 AI。返回 dict(prob, conf, why, model, raw) 或 None。
    任何异常都返回 None —— 预测是增强项，绝不能让主流程挂掉。
    """
    key = key or load_key()
    if not key:
        return None
    import requests
    body = {
        "model": C.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(
                question=question or "", league=league or "未知",
                game_start=game_start or "未知")},
        ],
        "temperature": 0,
        "max_tokens": C.AI_MAX_TOKENS,
        "stream": False,
    }
    try:
        r = requests.post(C.AI_ENDPOINT, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + key,
                                   "Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        j = r.json()
        txt = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
        s = txt.strip()
        if s.startswith("```"):
            s = s.strip("`")
            s = s[s.find("{"):]
        lo, hi = s.find("{"), s.rfind("}")
        if lo < 0 or hi < 0:
            return None
        d = json.loads(s[lo:hi + 1])
        p = float(d.get("prob"))
        if not (0.0 <= p <= 1.0):
            return None
        return {"prob": p, "conf": str(d.get("conf", ""))[:8],
                "why": str(d.get("why", ""))[:120],
                "model": C.AI_MODEL, "raw": txt[:400]}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------
def export_queue(conn, limit=None):
    """
    导出「该问 AI」的候选，返回 DataFrame。

    ★ 2026-10-12 修的两个坑（都来自"用了过期字段"）：
      (a) 价格必须是**某条 BET_RULES 档**，不是 [BET_PRICE_MIN, MAX)。
          粗预筛区间 [0.15,0.99) 里 0.5 附近的市场两条规则都不管 ——
          第一版把 mid=0.615 的也导进来了，那是无意义的调用。
      (b) 距开赛**必须用 game_start 现算**，不能用 observations 里的 days_to_start。
          那个字段是**快照时刻**算的，而最后一条快照可能是两天前的
          （代理掉线期间就发生过）。第一版因此把一个 15 分钟后开赛的比赛
          也导了进来，被赛前时间戳约束拒绝 —— 表现为"12 条失败"，
          其实那 12 条是比赛已经开始了。
    """
    import pandas as pd
    import datetime as dt
    limit = limit or C.AI_MAX_CALLS_PER_RUN

    bands = " OR ".join(["(l.mid >= ? AND l.mid < ?)"] * len(C.BET_RULES))
    params = []
    for lo, hi, _sd, _nt in C.BET_RULES:
        params += [lo, hi]
    q = """
    WITH lastobs AS (
      SELECT o.*, ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY snap_ts DESC) rn
      FROM observations o
    ), asked AS (SELECT market_id FROM ai_forecasts)
    SELECT l.market_id, l.league, l.question, l.mid, l.ask, l.spread,
           l.days_to_start, l.game_start, l.snap_ts
    FROM lastobs l
    WHERE l.rn = 1
      AND l.market_id NOT IN (SELECT market_id FROM asked)
      AND l.mid IS NOT NULL AND l.ask IS NOT NULL
      AND l.spread <= ?
      AND l.game_start IS NOT NULL
      AND (%s)
    ORDER BY l.vol24 DESC
    LIMIT ?
    """ % bands
    rows = conn.execute(q, [C.BET_MAX_SPREAD] + params + [max(limit * 8, 200)]).fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    now = dt.datetime.now(dt.timezone.utc)
    gs = pd.to_datetime(df["game_start"], errors="coerce", utc=True)
    df["lead_now"] = (gs - now).dt.total_seconds() / 86400.0
    df = df[(df["lead_now"] >= C.AI_MIN_LEAD_DAYS)
            & (df["lead_now"] <= max(C.ENTRY_LEAD_DAYS) + 0.5)]
    df = df.head(limit)
    return df


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(verbose=True, limit=None):
    """
    导出队列 -> 逐个问 AI -> 落库。返回统计 dict。

    硬约束：
      · made_ts 必须早于 game_start，否则拒收（防止事后补录）
      · UNIQUE(market_id)：同一市场只记第一条
    """
    conn = D.connect()
    ensure_schema(conn)
    key = load_key()
    if not key:
        if verbose:
            print("  [ai] 没找到 API key —— 只导出队列，不发请求", flush=True)
        return {"asked": 0, "stored": 0, "failed": 0, "no_key": True}

    used_today = conn.execute(
        "SELECT COUNT(*) c FROM ai_forecasts WHERE substr(made_ts,1,10)=?",
        (D.today(),)).fetchone()["c"]
    room = max(0, C.AI_MAX_CALLS_PER_DAY - int(used_today))
    if room <= 0:
        if verbose:
            print("  [ai] 今日额度已用尽（%d/%d）" % (used_today, C.AI_MAX_CALLS_PER_DAY),
                  flush=True)
        return {"asked": 0, "stored": 0, "failed": 0, "quota_hit": True}

    df = export_queue(conn, limit=min(limit or C.AI_MAX_CALLS_PER_RUN, room))
    if len(df) == 0:
        if verbose:
            print("  [ai] 队列为空（没有新的候选需要问）", flush=True)
        return {"asked": 0, "stored": 0, "failed": 0}

    if verbose:
        print("  [ai] 队列 %d 个市场，今日已用 %d/%d 额度"
              % (len(df), used_today, C.AI_MAX_CALLS_PER_DAY), flush=True)

    now = D.now_iso()
    n_ok = n_fail = 0
    for _, r in df.iterrows():
        gs = r.get("game_start")
        # ★ 硬约束：只在赛前落库
        if gs and str(gs) <= now:
            n_fail += 1
            continue
        res = ask_once(r["question"], r["league"], gs, key=key)
        if res is None:
            n_fail += 1
            time.sleep(C.AI_SLEEP_SEC)
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO ai_forecasts(market_id,made_ts,game_start,league,"
                "question,market_mid,market_ask,ai_prob,conf,why,model,prompt_ver) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["market_id"], now, gs, r["league"], r["question"],
                 float(r["mid"]), float(r["ask"]), res["prob"], res["conf"],
                 res["why"], res["model"], PROMPT_VER))
            n_ok += 1
        except Exception:
            n_fail += 1
        time.sleep(C.AI_SLEEP_SEC)
    conn.commit()
    if verbose:
        print("  [ai] 落库 %d 条，失败 %d 条" % (n_ok, n_fail), flush=True)
    conn.close()
    return {"asked": len(df), "stored": n_ok, "failed": n_fail}


def backfill_outcomes(conn):
    """把已结算市场的最终结果回填到 ai_forecasts（从 bets 或 observations 取）。"""
    n = conn.execute(
        "UPDATE ai_forecasts SET outcome_yes=("
        "  SELECT b.outcome_yes FROM bets b WHERE b.market_id=ai_forecasts.market_id "
        "  AND b.outcome_yes IS NOT NULL LIMIT 1), "
        "resolved_ts=? "
        "WHERE outcome_yes IS NULL AND EXISTS("
        "  SELECT 1 FROM bets b WHERE b.market_id=ai_forecasts.market_id "
        "  AND b.outcome_yes IS NOT NULL)", (D.now_iso(),)).rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 评估 —— 三个固定公式，不看人脸色
# ---------------------------------------------------------------------------
def evaluate(conn=None):
    own = conn is None
    conn = conn or D.connect()
    ensure_schema(conn)
    backfill_outcomes(conn)
    rows = conn.execute(
        "SELECT market_id, ai_prob, market_mid, outcome_yes, conf, league, made_ts, game_start "
        "FROM ai_forecasts WHERE outcome_yes IS NOT NULL").fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM ai_forecasts").fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM ai_forecasts WHERE outcome_yes IS NULL").fetchone()["c"]
    if own:
        conn.close()

    out = {"total": int(total), "pending": int(pending), "resolved": len(rows)}
    if len(rows) < 10:
        out["note"] = "已结算样本不足 10 条，先不下结论"
        return out

    ai = np.array([float(r["ai_prob"]) for r in rows])
    mk = np.array([float(r["market_mid"]) for r in rows])
    oc = np.array([float(r["outcome_yes"]) for r in rows])

    # ① AI 与市场有没有独立信息
    out["corr_ai_market"] = float(np.corrcoef(ai, mk)[0, 1]) if ai.std() > 0 and mk.std() > 0 else None
    # ② AI 自己准不准（相对市场）
    out["k_ai"] = float(oc.mean() - ai.mean())
    out["k_market"] = float(oc.mean() - mk.mean())
    se = float(oc.std(ddof=1) / np.sqrt(len(oc)))
    out["se"] = se
    # ③ ★ 关键：AI 相对市场的偏离，能不能预测结果相对市场的偏离
    dev_ai = ai - mk
    dev_oc = oc - mk
    out["corr_dev"] = float(np.corrcoef(dev_ai, dev_oc)[0, 1]) if dev_ai.std() > 0 else None
    # 分档：AI 说"比市场便宜/贵"的上下半，实际结果差多少
    med = np.median(dev_ai)
    hi = dev_oc[dev_ai > med]
    lo = dev_oc[dev_ai <= med]
    out["dev_hi_mean"] = float(hi.mean()) if len(hi) else None
    out["dev_lo_mean"] = float(lo.mean()) if len(lo) else None
    out["dev_spread"] = (float(hi.mean() - lo.mean())
                         if len(hi) and len(lo) else None)
    return out


def report_lines(conn=None):
    e = evaluate(conn)
    L = []
    L.append("  AI 预测（赛前落库，赛后固定公式评估）")
    L.append("     已落库 %d 条；已结算 %d；待结算 %d"
             % (e["total"], e["resolved"], e["pending"]))
    if e.get("note"):
        L.append("     %s" % e["note"])
        return L
    L.append("     ① corr(AI概率, 市场中间价) = %s"
             % ("%.3f" % e["corr_ai_market"] if e.get("corr_ai_market") is not None else "n/a"))
    L.append("        > 0.95 ⇒ AI 基本在复述市场，没有独立信息")
    L.append("     ② k_AI = 实际胜率 - AI概率 = %+.4f   （市场口径 k = %+.4f）"
             % (e["k_ai"], e["k_market"]))
    L.append("     ③ ★ 关键：corr(AI偏离, 结果偏离) = %s"
             % ("%.3f" % e["corr_dev"] if e.get("corr_dev") is not None else "n/a"))
    if e.get("dev_spread") is not None:
        L.append("        AI 说'更该买'的一半，实际偏离 %+.4f；另一半 %+.4f；差 %+.4f"
                 % (e["dev_hi_mean"], e["dev_lo_mean"], e["dev_spread"]))
        L.append("        差值显著 > 0 才说明 AI 有增量；接近 0 就是噪声")
    return L


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "eval":
        print("\n".join(report_lines()))
    else:
        print(run(verbose=True))
