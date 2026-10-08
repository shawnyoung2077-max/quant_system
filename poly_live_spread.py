"""
poly_live_spread.py - 实时盘口：真实价差与深度（代理已开）
============================================================================
需要回答的问题：
  Q1 官方费率公式到底是什么？（已从 docs 拿到，这里从 API 字段再确认）
  Q2 0.10-0.15 / 0.85-0.90 档的【实时】盘口价差是多少？
  Q3 盘口深度有多大？（决定单笔能吃多少）
  Q4 Maker 真的免费且有返利吗？（这会让整个结论反转）
"""

import io
import json
import os
import sys

sys.path.insert(0, r"D:\26050\Documents\quant_system")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=40,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                import time
                time.sleep(2 + 2 * i)
                continue
            return {"__err__": r.status_code, "__body__": r.text[:200]}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": "exc", "__body__": str(e)[:200]}
    return None


def main():
    log("=" * 100)
    log("Polymarket 实时盘口分析")
    log("=" * 100)
    log("")

    # ------------------------------------------------------------------
    # 1. 官方费率公式（从 docs 已取得，这里记录并二次确认字段）
    # ------------------------------------------------------------------
    log("=" * 100)
    log("1. 官方费率公式（来源：docs.polymarket.com/cn/trading/fees，已实测可达）")
    log("=" * 100)
    log("")
    log("  官方原文：")
    log("     fee = C x feeRate x p x (1 - p)")
    log("     其中 C = 交易的份额数量，p = 份额价格")
    log("")
    log("  * 这与我原先假设的 rate x min(p, 1-p) 不同 —— 是 p x (1-p)。")
    log("    对 p=0.12: 官方=0.05x0.12x0.88=0.00528/股；")
    log("              我的旧假设=0.05x0.12=0.00600/股。")
    log("    官方口径【更低】约 12%%，方向对我有利，但仍需修正。")
    log("")
    log("  官方费率表（Taker Fee Rate）：")
    for cat, rate, rebate in [("加密货币", 0.07, "20%"), ("体育", 0.05, "15%"),
                              ("金融", 0.04, "25%"), ("政治", 0.04, "25%"),
                              ("经济", 0.05, "25%"), ("文化", 0.05, "25%"),
                              ("天气", 0.05, "25%"), ("其他/通用", 0.05, "25%"),
                              ("Mentions", 0.04, "25%"), ("科技", 0.04, "25%"),
                              ("地缘政治", 0.0, "-")]:
        log("     %-12s taker=%.2f  maker=0  maker返利=%s" % (cat, rate, rebate))
    log("")
    log("  ★★ 重要：Maker 费用 = 0，且有 15%% 返利（体育档）")
    log("     『Maker 不收取任何费用。只有 Taker 支付费用。』")
    log("     ⇒ 挂限价单（maker）不但不付手续费，还能分到 taker 交的费用！")
    log("     这会让整个成本结构改变 —— 代价是承担【逆向选择】风险")
    log("     （你的限价单只在市场朝不利方向走时成交）。详见第 4 节。")

    # ------------------------------------------------------------------
    # 2. 抓实时市场，看价差字段
    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("2. 实时市场快照：价差与深度")
    log("=" * 100)
    log("")
    ms = []
    for off in range(0, 2000, 500):
        j = get(GAMMA + "/markets", {"limit": 500, "offset": off, "closed": "false",
                                     "order": "volumeNum", "ascending": "false"})
        if isinstance(j, list):
            ms.extend(j)
        elif isinstance(j, dict) and "__err__" in j:
            log("  ! 拉取失败 offset=%d: %s" % (off, j.get("__body__", "")[:80]))
            break
    log("  抓到 %d 个未结算市场" % len(ms))
    if not ms:
        _write()
        return

    # 字段探查
    k0 = sorted(ms[0].keys())
    log("  字段: %s" % [k for k in k0 if any(t in k.lower()
        for t in ("fee", "rate", "spread", "bid", "ask", "price", "liquid", "volume"))])
    log("")

    # ------------------------------------------------------------------
    # 3. 价差分布（实时）
    # ------------------------------------------------------------------
    rows = []
    for m in ms:
        try:
            bid = float(m.get("bestBid")) if m.get("bestBid") not in (None, "") else np.nan
            ask = float(m.get("bestAsk")) if m.get("bestAsk") not in (None, "") else np.nan
            sp = float(m.get("spread")) if m.get("spread") not in (None, "") else (ask - bid)
            rows.append({
                "q": (m.get("question") or "")[:60],
                "bid": bid, "ask": ask, "spread": sp,
                "vol": float(m.get("volumeNum") or 0),
                "liq": float(m.get("liquidityNum") or 0),
                "feeRate": m.get("feeRateBps") or m.get("fee_rate_bps"),
                "negRisk": m.get("negRisk"),
                "slug": m.get("slug"),
            })
        except Exception:
            continue
    df = pd.DataFrame(rows)
    log("  可用盘口字段的市场: %d" % df["spread"].notna().sum())
    mid = (df["bid"] + df["ask"]) / 2
    df["mid"] = mid

    live = df[df["spread"].notna() & (df["spread"] > 0)]
    log("")
    log("  实时价差分布（全部未结算市场, n=%d）:" % len(live))
    for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        log("     p%-4.0f = %.4f" % (q * 100, live["spread"].quantile(q)))
    log("     均值 = %.4f" % live["spread"].mean())

    # 分价格档看价差
    log("")
    log("  按价格档看实时价差（这才是关键 —— 价差随价格变化）:")
    log("  %-14s %8s %10s %10s %10s %12s" %
        ("价格区间", "样本", "价差中位", "价差均值", "占价格%", "vs edge 2%"))
    log("  " + "-" * 92)
    for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
                   (0.20, 0.50), (0.50, 0.80), (0.80, 0.85), (0.85, 0.90),
                   (0.90, 0.95), (0.95, 1.01)]:
        s = df[(df["mid"] >= lo) & (df["mid"] < hi) & df["spread"].notna()]
        if len(s) < 3:
            log("  %-14s %8d %10s %10s %10s %12s" %
                ("%.2f-%.2f" % (lo, hi), len(s), "-", "-", "-", "样本少"))
            continue
        sp_med = s["spread"].median()
        # 买 NO 时：投入 = 1-p，价差成本占投入的比例
        p_ = s["mid"].median()
        cost_ratio = sp_med / (1 - p_) if p_ > 0.5 else sp_med / p_
        verdict = "价差>edge" if sp_med > 0.02 else (
            "价差吃掉>50%" if sp_med > 0.01 else "可承受")
        log("  %-14s %8d %10.4f %10.4f %9.1f%% %12s" %
            ("%.2f-%.2f" % (lo, hi), len(s), sp_med, s["spread"].mean(),
             cost_ratio * 100, verdict))

    # ------------------------------------------------------------------
    # 4. 深度：单笔能吃多少
    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("4. 深度：单笔能吃掉多少？（用 CLOB 订单簿实测）")
    log("=" * 100)
    log("")
    cand = df[(df["mid"] >= 0.10) & (df["mid"] < 0.15) & (df["vol"] > 1e4)]
    log("  0.10-0.15 档且有成交额的市场: %d 个" % len(cand))
    if len(cand):
        log("  %-58s %8s %8s %10s" % ("问题", "bid", "ask", "成交额"))
        for _, r in cand.head(8).iterrows():
            log("  %-58s %8.3f %8.3f %10.0f" % (r["q"], r["bid"], r["ask"], r["vol"]))

    # 用 CLOB book 抓深度
    log("")
    log("  --- 订单簿深度实测（取若干 0.10-0.15 档市场）---")
    import time
    tok = None
    depth_rows = []
    for m in ms:
        try:
            b = float(m.get("bestBid") or 0)
            a = float(m.get("bestAsk") or 0)
            if not (0.10 <= (a + b) / 2 < 0.15):
                continue
            tids = m.get("clobTokenIds")
            if isinstance(tids, str):
                tids = json.loads(tids)
            if not tids:
                continue
            t = tids[0]
            bk = get(CLOB + "/book", {"token_id": t})
            if not isinstance(bk, dict) or "bids" not in bk:
                continue
            bids = bk.get("bids") or []
            asks = bk.get("asks") or []
            # 累计美元深度
            def cum_usd(side, n=10):
                tot = 0.0
                for lv in side[:n]:
                    try:
                        tot += float(lv["price"]) * float(lv["size"])
                    except Exception:
                        pass
                return tot
            mid_ = (a + b) / 2
            depth_rows.append({"q": (m.get("question") or "")[:46], "mid": mid_,
                               "bid_usd": cum_usd(bids), "ask_usd": cum_usd(asks),
                               "n_bid": len(bids), "n_ask": len(asks)})
            if len(depth_rows) >= 12:
                break
            time.sleep(0.25)
        except Exception:
            continue
    if depth_rows:
        dd = pd.DataFrame(depth_rows)
        log("  %-48s %7s %12s %12s %6s %6s" %
            ("问题", "中间价", "买盘$", "卖盘$", "bid档", "ask档"))
        for _, r in dd.iterrows():
            log("  %-48s %7.3f %12.0f %12.0f %6d %6d" %
                (r["q"], r["mid"], r["bid_usd"], r["ask_usd"], r["n_bid"], r["n_ask"]))
        log("")
        log("  买盘前10档累计美元: 中位=$%.0f 均值=$%.0f" %
            (dd["bid_usd"].median(), dd["bid_usd"].mean()))
        log("  卖盘前10档累计美元: 中位=$%.0f 均值=$%.0f" %
            (dd["ask_usd"].median(), dd["ask_usd"].mean()))
        log("  ⇒ 单笔可成交规模约在【$百 ~ $千】量级（乐观取两倍做缓冲）")
        dd.to_csv(r"D:\26050\Documents\quant_system\output_poly_book_depth.csv", index=False)
    else:
        log("  ! 未能取到订单簿")

    df.to_csv(r"D:\26050\Documents\quant_system\output_poly_live_markets.csv", index=False)
    _write()


def _write():
    with open(r"D:\26050\Documents\quant_system\output_poly_live_spread.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_live_spread.txt / output_poly_live_markets.csv")


if __name__ == "__main__":
    main()
