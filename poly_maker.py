"""
poly_maker.py - 纯体育市场：做市奖励(liquidity rewards)经济性 + 修正后的价差
============================================================================
修正前一版的错误：poly_live_spread.py 用「按成交额排序的全市种」抓实时市场，
前几名全是政治/地缘政治，导致价差表被污染。本脚本**只取体育**。

回答两个问题：
  Q1 体育 0.10-0.15 档的【真实】价差是多少？（修正污染）
  Q2 提供流动性（做市）拿官方奖励，需要多少启动资金？收益率多少？

做市奖励机制（Polymarket Liquidity Rewards Program）：
  · 市场有 rewardsDailyRate（每天发放的奖励池，USD）
  · 要在 mid 的 rewardsMaxSpread 美分以内挂单，且单边至少 rewardsMinSize 股
  · 奖励按【合格挂单量 × 在线时长】占比分配
  ⇒ 你的日收益 = rewardsDailyRate × V / (V + D)
     其中 V = 你的合格挂单额，D = 其他做市商的合格挂单额
  ⇒ 日收益率 = rewardsDailyRate / (V + D)
     **注意：收益率随 V 增大而下降** —— 这是关键的反直觉点：
     投入越多，你自己把自己的收益率稀释了。
"""

import io
import json
import os
import sys
import time

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
CAT = r"D:\26050\Documents\polymarket_sports\data\sports_catalog.json"


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def get(url, params=None, tries=3, timeout=40):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(2 + 2 * i)
                continue
            return {"__err__": r.status_code}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": str(e)[:100]}
    return None


def fetch_sports_markets(max_pages=14, limit=100):
    """按 sports catalog 的 primaryTagId 逐联赛抓未结算市场。"""
    cat = json.load(io.open(CAT, encoding="utf-8"))
    tags = []
    for c in cat:
        t = c.get("primaryTagId")
        if t and t not in tags:
            tags.append(t)
    log("[sports] catalog 联赛数=%d, 唯一 primaryTagId=%d" % (len(cat), len(tags)))

    seen = {}
    for i, t in enumerate(tags):
        j = get(GAMMA + "/markets", {"limit": limit, "closed": "false", "tag_id": t})
        if isinstance(j, list):
            for m in j:
                mid = m.get("id") or m.get("conditionId")
                if mid:
                    seen[mid] = m
        if (i + 1) % 60 == 0:
            log("  ... tag %d/%d, 累计 %d 个市场" % (i + 1, len(tags), len(seen)))
            if i + 1 >= max_pages * 60:
                break
    log("[sports] 抓到未结算体育市场 %d 个" % len(seen))
    return list(seen.values())


def main():
    log("=" * 100)
    log("纯体育市场：做市奖励经济性 + 修正后的价差")
    log("=" * 100)
    log("")

    ms = fetch_sports_markets()
    if not ms:
        log("! 未抓到体育市场")
        _write()
        return

    rows = []
    for m in ms:
        try:
            bid = float(m.get("bestBid")) if m.get("bestBid") not in (None, "") else np.nan
            ask = float(m.get("bestAsk")) if m.get("bestAsk") not in (None, "") else np.nan
            if not (np.isfinite(bid) and np.isfinite(ask) and ask > bid):
                continue
            cr = m.get("clobRewards") or []
            rate = 0.0
            for c in cr:
                try:
                    rate += float(c.get("rewardsDailyRate") or 0)
                except Exception:
                    pass
            rows.append({
                "q": (m.get("question") or "")[:56],
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "spread": ask - bid,
                "tick": m.get("orderPriceMinTickSize"),
                "vol24": float(m.get("volume24hr") or 0),
                "volnum": float(m.get("volumeNum") or 0),
                "liq": float(m.get("liquidityNum") or 0),
                "rate": rate,
                "minSize": m.get("rewardsMinSize"),
                "maxSpread": m.get("rewardsMaxSpread"),
                "feeType": m.get("feeType"),
                "feesEnabled": m.get("feesEnabled"),
                "tok0": (json.loads(m["clobTokenIds"])[0]
                         if isinstance(m.get("clobTokenIds"), str) else None),
            })
        except Exception:
            continue
    df = pd.DataFrame(rows)
    log("[sports] 有有效盘口的市场 %d 个" % len(df))
    if not len(df):
        _write()
        return

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("1. 修正后的价差（纯体育）—— 对比上一版被污染的表")
    log("=" * 100)
    log("")
    log("  %-14s %8s %10s %10s %10s %10s" %
        ("价格区间", "样本", "价差中位", "价差均值", "tick中位", "占价格%"))
    log("  " + "-" * 92)
    for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20),
                   (0.20, 0.35), (0.35, 0.65), (0.65, 0.80), (0.80, 0.90),
                   (0.90, 1.01)]:
        s = df[(df["mid"] >= lo) & (df["mid"] < hi)]
        if len(s) < 3:
            log("  %-14s %8d %10s %10s %10s %10s" %
                ("%.2f-%.2f" % (lo, hi), len(s), "-", "-", "-", "-"))
            continue
        log("  %-14s %8d %10.4f %10.4f %10.4f %9.1f%%" %
            ("%.2f-%.2f" % (lo, hi), len(s), s["spread"].median(), s["spread"].mean(),
             float(pd.to_numeric(s["tick"], errors="coerce").median() or 0),
             (s["spread"].median() / max(s["mid"].median(), 0.02)) * 100))

    sub = df[(df["mid"] >= 0.10) & (df["mid"] < 0.15)]
    log("")
    log("  * 体育 0.10-0.15 档: n=%d  价差中位=%.4f  均值=%.4f"
        % (len(sub), sub["spread"].median() if len(sub) else np.nan,
           sub["spread"].mean() if len(sub) else np.nan))
    log("    （上一版被污染的表给出 0.0100 / 0.0242 —— 混合了政治和地缘政治）")

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("2. 做市奖励：体育市场里有多少在发奖励？发多少？")
    log("=" * 100)
    log("")
    rw = df[df["rate"] > 0]
    log("  有奖励的市场: %d / %d (%.0f%%)" % (len(rw), len(df), len(rw) / len(df) * 100))
    if len(rw):
        log("  rewardsDailyRate 分布: 中位=$%.1f 均值=$%.1f p90=$%.1f max=$%.1f"
            % (rw["rate"].median(), rw["rate"].mean(),
               rw["rate"].quantile(.9), rw["rate"].max()))
        log("  合计日奖励池 = $%.0f/天" % rw["rate"].sum())
        log("")
        log("  rewardsMaxSpread 分布: %s" % sorted(rw["maxSpread"].dropna().unique())[:12])
        log("  rewardsMinSize 分布:   中位=%s 取值=%s" %
            (rw["minSize"].median(), sorted(rw["minSize"].dropna().unique())[:12]))
        log("  分价格档看奖励市场数:")
        for lo, hi in [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25),
                       (0.25, 0.50), (0.50, 0.75), (0.75, 0.90), (0.90, 1.01)]:
            s = rw[(rw["mid"] >= lo) & (rw["mid"] < hi)]
            if len(s):
                log("     %.2f-%.2f : %3d 个, 日奖励中位 $%.1f" %
                    (lo, hi, len(s), s["rate"].median()))

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("3. 关键：要拿到奖励，最少要放多少钱？")
    log("=" * 100)
    log("")
    log("  规则：在 mid 的 rewardsMaxSpread 美分内挂单，单边至少 rewardsMinSize 股。")
    log("  最小合格挂单额 V_min = rewardsMinSize x 价格（每股按 1 美元结算）")
    log("")
    if len(rw):
        rw = rw.copy()
        rw["vmin"] = pd.to_numeric(rw["minSize"], errors="coerce") * rw["mid"]
        rw["vmin2"] = pd.to_numeric(rw["minSize"], errors="coerce") * (1 - rw["mid"])
        v = rw["vmin"].dropna()
        log("  V_min（按 YES 侧价格）: 中位=$%.0f 均值=$%.0f p90=$%.0f max=$%.0f"
            % (v.median(), v.mean(), v.quantile(.9), v.max()))
        log("  注：双边都要挂才拿满奖励 ⇒ 实际占用资金约 2 x V_min")
        log("  => 单市场最少占用资金: 中位 $%.0f" % (v.median() * 2))

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("4. 收益率：奖励 / 占用资金（含竞争者稀释）")
    log("=" * 100)
    log("")
    log("  公式: 日收益 = rate x V/(V+D)，  D = 其他做市商的合格挂单额")
    log("        日收益率 = rate / (V+D)")
    log("  ★ 反直觉点：收益率随 V 增大而【下降】—— 你自己稀释自己。")
    log("  => 最高收益率出现在【只有你一个人做市】的市场：V+D ≈ V")
    log("     此时日收益率 = rate/V —— 用小资金去吃大奖励池。")
    log("")
    log("  实测竞争者深度 D：用订单簿 maxSpread 范围内的挂单额近似。")
    log("  抓取若干有奖励的体育市场的订单簿 ...")

    if len(rw):
        cand = rw[(rw["vol24"] > 5000)].sort_values("rate", ascending=False).head(18)
        depth = []
        for _, r in cand.iterrows():
            if not r["tok0"]:
                continue
            bk = get(CLOB + "/book", {"token_id": r["tok0"]})
            if not isinstance(bk, dict) or "bids" not in bk:
                continue
            try:
                ms_ = float(r["maxSpread"]) / 100.0     # 美分 -> 价格单位
            except Exception:
                ms_ = 0.035
            mid = float(r["mid"])
            # 合格买盘 = 价格 >= mid - ms 的买单
            qb = sum(float(l["price"]) * float(l["size"]) for l in (bk.get("bids") or [])
                     if float(l["price"]) >= mid - ms_)
            qa = sum(float(l["price"]) * float(l["size"]) for l in (bk.get("asks") or [])
                     if float(l["price"]) <= mid + ms_)
            depth.append({"q": r["q"], "mid": mid, "rate": r["rate"],
                          "maxSpread": ms_, "minSize": r["minSize"],
                          "qual_bid": qb, "qual_ask": qa, "D": qb + qa,
                          "vmin": float(r["minSize"] or 0) * 0.5 * 2})
            time.sleep(0.2)
        dd = pd.DataFrame(depth)
        if len(dd):
            dd["vmin"] = dd["minSize"] * dd["mid"] * 2      # 双边
            dd["daily_yield_min"] = dd["rate"] / (dd["vmin"] + dd["D"])
            dd["daily_yield_10x"] = dd["rate"] / (dd["vmin"] * 10 + dd["D"])
            dd["ann_yield_min"] = dd["daily_yield_min"] * 365
            dd["ann_yield_10x"] = dd["daily_yield_10x"] * 365
            log("")
            log("  %-40s %7s %8s %10s %10s %10s %10s" %
                ("问题", "rate", "V_min", "竞争者D", "日收益%", "年化%", "年化%10x"))
            log("  " + "-" * 100)
            for _, r in dd.sort_values("ann_yield_min", ascending=False).head(14).iterrows():
                log("  %-40s %7.0f %8.0f %10.0f %9.3f%% %9.1f%% %9.1f%%" %
                    (r["q"], r["rate"], r["vmin"], r["D"],
                     r["daily_yield_min"] * 100, r["ann_yield_min"] * 100,
                     r["ann_yield_10x"] * 100))
            dd.to_csv(r"D:\26050\Documents\quant_system\output_poly_maker.csv", index=False)
            log("")
            log("  --- 统计 ---")
            log("  最小资金(V_min)下的年化收益率: 中位=%.1f%% 均值=%.1f%% max=%.1f%%" %
                (dd["ann_yield_min"].median() * 100, dd["ann_yield_min"].mean() * 100,
                 dd["ann_yield_min"].max() * 100))
            log("  放大到 10x 资金后年化:        中位=%.1f%%" % (dd["ann_yield_10x"].median() * 100))
            log("  竞争者合格挂单 D: 中位=$%.0f 均值=$%.0f" % (dd["D"].median(), dd["D"].mean()))

    df.to_csv(r"D:\26050\Documents\quant_system\output_poly_sports_live.csv", index=False)
    _write()


def _write():
    with open(r"D:\26050\Documents\quant_system\output_poly_maker.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_maker.txt / output_poly_maker.csv / output_poly_sports_live.csv")


if __name__ == "__main__":
    main()
