"""
poly_technical.py - 按「可否技术分析」给 Polymarket 市场分类，并算做市资金
============================================================================
用户的框架（本次要落实的判断标准）：
  「不一定是体育，只要能做技术分析就行 —— 至少比大众判断准。
    不想碰地缘政治，因为那基本靠猜靠运气。赚钱要靠技术、理性和建模。」

把这个直觉形式化成一个可检验的标准：

  ★ 一个市场"可以做技术分析" ⟺ 存在【独立于大众情绪的、可计算的参照价】
     · 有参照价 ⇒ 你能算出"应该是多少"，再与市场价比较 ⇒ 有客观的 edge
     · 没有参照价 ⇒ 你只能猜，而你和大众都在猜 ⇒ 没有可重复的优势

  按这个标准分类：
    A 竞技类      参照 = 博彩公司赔率(Pinnacle/Betfair) 或 Elo/Poisson 模型
    B 加密价格阈值 参照 = 期权隐含波动率(Deribit) —— 这本质是【数字期权定价】
    C 经济指标     参照 = 联邦基金期货 / CPI nowcast 模型
    D 天气         参照 = 气象预报模型(数值天气预报)
    E 选举         参照 = 民调均值 + 基本面模型（噪音大，但仍有参照）
    F 地缘政治     参照 = 无  ← 用户明确排除
    G 文化/名人/宗教 参照 = 无  ← 同样排除

  本脚本做两件事：
    1. 用实时数据给出各分类的市场数、奖励池、价差 ⇒ 看哪类【有肉】
    2. 算做市奖励需要的启动资金与收益率（限定在 A-D 类）
"""

import io
import json
import os
import sys
import time
import re
import collections

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
CAT = r"D:\26050\Documents\polymarket_sports\data\sports_catalog.json"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def get(url, params=None, tries=3, timeout=45):
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


# ===========================================================================
# 分类器：基于问题的关键词 + 是否来自 sports catalog
# ===========================================================================
GEO_KW = [r"\binvade\b", r"\bwar\b", r"\bceasefire\b", r"\bmilitary clash\b",
          r"\bstrike on\b", r"\bnuclear\b", r"\btroops\b", r"\bblockade\b",
          r"\bpeace deal\b", r"\btaiwan\b" , r"\biran\b", r"\brussia\b", r"\bukraine\b",
          r"\bvenezuela\b", r"\bnorth korea\b", r"\bgaza\b"]
CRYPTO_KW = [r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b", r"\bsolana\b",
             r"\bxrp\b", r"\bdoge\b", r"\bcrypto\b", r"\bfdv\b", r"\btoken\b",
             r"\bmarket cap\b", r"\ball[- ]time high\b", r"\bhalving\b"]
ECON_KW = [r"\binflation\b", r"\bcpi\b", r"\bfed\b", r"\binterest rate\b",
           r"\bgdp\b", r"\bunemployment\b", r"\brecession\b", r"\bjobless\b",
           r"\brate cut\b", r"\brate hike\b", r"\bfomc\b", r"\btariff\b"]
WEATHER_KW = [r"\btemperature\b", r"\bhurricane\b", r"\brainfall\b", r"\bsnow\b",
              r"\bweather\b", r"\btornado\b"]
ELEC_KW = [r"\belection\b", r"\bpresident\b", r"\bsenate\b", r"\bgovernor\b",
           r"\bprimary\b", r"\bnominee\b", r"\bvote\b", r"\bcongress\b", r"\bhouse\b",
           r"\bparliament\b", r"\bpoll\b", r"\bwin the\b"]
ENT_KW = [r"\bjesus\b", r"\bgod\b", r"\bgrammy\b", r"\boscar\b", r"\bmovie\b",
          r"\balbum\b", r"\bcelebrity\b", r"\bkardashian\b", r"\btaylor swift\b",
          r"\bseason finale\b", r"\brotten tomatoes\b"]


def classify(q, from_sports):
    t = (q or "").lower()
    if from_sports:
        return "A 竞技"
    for kw in GEO_KW:
        if re.search(kw, t):
            return "F 地缘政治"
    for kw in CRYPTO_KW:
        if re.search(kw, t):
            return "B 加密阈值"
    for kw in ECON_KW:
        if re.search(kw, t):
            return "C 经济指标"
    for kw in WEATHER_KW:
        if re.search(kw, t):
            return "D 天气"
    for kw in ENT_KW:
        if re.search(kw, t):
            return "G 文化/娱乐"
    for kw in ELEC_KW:
        if re.search(kw, t):
            return "E 选举/政治"
    return "H 未分类"


# 是否有独立的、可计算的参照价
HAS_REFERENCE = {
    "A 竞技": ("有", "博彩赔率(Pinnacle/Betfair) + Elo/Poisson 模型"),
    "B 加密阈值": ("有", "期权隐含波动率(Deribit) —— 本质是数字期权定价"),
    "C 经济指标": ("有", "联邦基金期货 / CPI nowcast"),
    "D 天气": ("有", "数值天气预报(NWP)"),
    "E 选举/政治": ("弱", "民调均值 + 基本面模型（噪音大）"),
    "F 地缘政治": ("无", "— 无法建模，只能猜  ← 用户排除"),
    "G 文化/娱乐": ("无", "— 无法建模  ← 应排除"),
    "H 未分类": ("?", "人工复核"),
}


def main():
    log("=" * 104)
    log("Polymarket：按「可否技术分析」分类 + 做市奖励资金测算")
    log("=" * 104)
    log("")
    log("  判断标准（形式化用户直觉）：")
    log("    可技术分析 ⟺ 存在【独立于大众情绪的、可计算的参照价】")
    log("")

    # ---- 抓数据 ----
    ms = {}
    # 体育（按 catalog tag）
    cat = json.load(io.open(CAT, encoding="utf-8"))
    tags = []
    for c in cat:
        t = c.get("primaryTagId")
        if t and t not in tags:
            tags.append(t)
    tags = tags[:130]
    for i, t in enumerate(tags):
        j = get(GAMMA + "/markets", {"limit": 100, "closed": "false", "tag_id": t})
        if isinstance(j, list):
            for m in j:
                k = m.get("id") or m.get("conditionId")
                if k:
                    m["__sports"] = True
                    ms[k] = m
        if (i + 1) % 40 == 0:
            log("  [sports] tag %d/%d, 累计 %d" % (i + 1, len(tags), len(ms)))
    log("[抓取] 体育市场 %d 个" % len(ms))

    n_before = len(ms)
    for off in range(0, 1600, 400):
        j = get(GAMMA + "/markets", {"limit": 400, "offset": off, "closed": "false",
                                     "order": "volumeNum", "ascending": "false"})
        if not isinstance(j, list) or not j:
            break
        for m in j:
            k = m.get("id") or m.get("conditionId")
            if k and k not in ms:
                m["__sports"] = False
                ms[k] = m
    log("[抓取] 全部未结算市场 %d 个（其中非体育 %d）" % (len(ms), len(ms) - n_before))

    rows = []
    for m in ms.values():
        try:
            bid = float(m.get("bestBid")) if m.get("bestBid") not in (None, "") else np.nan
            ask = float(m.get("bestAsk")) if m.get("bestAsk") not in (None, "") else np.nan
            cr = m.get("clobRewards") or []
            rate = 0.0
            for c in cr:
                try:
                    rate += float(c.get("rewardsDailyRate") or 0)
                except Exception:
                    pass
            q = m.get("question") or ""
            cls = classify(q, bool(m.get("__sports")))
            rows.append({
                "q": q[:70], "cls": cls,
                "bid": bid, "ask": ask,
                "mid": (bid + ask) / 2 if np.isfinite(bid) and np.isfinite(ask) else np.nan,
                "spread": (ask - bid) if np.isfinite(bid) and np.isfinite(ask) else np.nan,
                "vol24": float(m.get("volume24hr") or 0),
                "volnum": float(m.get("volumeNum") or 0),
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
    log("[数据] 有效市场 %d 个" % len(df))

    # ------------------------------------------------------------------
    log("")
    log("=" * 104)
    log("1. 分类总览：哪一类【有肉】，哪一类【不能碰】")
    log("=" * 104)
    log("")
    log("  %-12s %6s %12s %12s %10s %-34s" %
        ("分类", "市场数", "24h成交额", "合计日奖励", "价差中位", "独立参照价"))
    log("  " + "-" * 100)
    for c in sorted(df["cls"].unique()):
        s = df[df["cls"] == c]
        has, why = HAS_REFERENCE.get(c, ("?", ""))
        log("  %-12s %6d %12.0f %12.0f %10s  %s" %
            (c, len(s), s["vol24"].sum(), s["rate"].sum(),
             ("%.4f" % s["spread"].median()) if s["spread"].notna().any() else "-",
             "%s: %s" % (has, why)))

    log("")
    log("  ★ 按用户的标准（要能技术分析），可用类别：A 竞技 / B 加密阈值 / C 经济指标 / D 天气")
    log("    排除：F 地缘政治 / G 文化娱乐（无参照价，只能猜）")
    log("    弱：E 选举（有民调参照，但噪音大；且 feeType 常与政治绑定）")

    # ------------------------------------------------------------------
    log("")
    log("=" * 104)
    log("2. 做市奖励（Liquidity Rewards）：限定在【有参照价】的类别里")
    log("=" * 104)
    log("")
    USABLE = ["A 竞技", "B 加密阈值", "C 经济指标", "D 天气"]
    u = df[df["cls"].isin(USABLE)]
    log("  可用类别的市场数 = %d（占全部 %.0f%%）" % (len(u), len(u) / max(len(df), 1) * 100))

    rw = u[u["rate"] > 0].copy()
    log("  其中有做市奖励的: %d 个 (%.0f%%)" % (len(rw), len(rw) / max(len(u), 1) * 100))
    if not len(rw):
        log("  ! 可用类别里没有发奖励的市场")
        _write(df)
        return
    log("  rewardsDailyRate: 中位=$%.1f 均值=$%.1f max=$%.1f  合计=$%.0f/天" %
        (rw["rate"].median(), rw["rate"].mean(), rw["rate"].max(), rw["rate"].sum()))
    log("")
    log("  分「有参照价类别」的奖励池:")
    for c in USABLE:
        s = rw[rw["cls"] == c]
        if len(s):
            log("     %-12s %4d 个市场, 日奖励中位=$%.1f 合计=$%.0f/天" %
                (c, len(s), s["rate"].median(), s["rate"].sum()))

    # ------------------------------------------------------------------
    log("")
    log("=" * 104)
    log("3. 需要多少启动资金？—— 用真实订单簿算")
    log("=" * 104)
    log("")
    log("  规则：在 mid 的 rewardsMaxSpread 美分内挂单，单边至少 rewardsMinSize 股。")
    log("  最小占用资金 V_min = rewardsMinSize x mid x 2（双边各挂一份）")
    log("  ★ 日收益率 = rate / (V + D)，D = 其他做市商的合格挂单额")
    log("    **收益率随 V 增大而下降 —— 你自己稀释自己。**")
    log("    所以最高收益率出现在【竞争者少】的市场。")
    log("")

    cand = rw[rw["vol24"] > 3000].sort_values("rate", ascending=False).head(14)
    log("  抓取 %d 个市场的订单簿以估计 D ..." % len(cand))
    dep = []
    for _, r in cand.iterrows():
        if not r["tok0"]:
            continue
        bk = get(CLOB + "/book", {"token_id": r["tok0"]})
        if not isinstance(bk, dict) or "bids" not in bk:
            continue
        try:
            ms_ = float(r["maxSpread"]) / 100.0
        except Exception:
            ms_ = 0.035
        mid = float(r["mid"])
        qb = sum(float(l["price"]) * float(l["size"]) for l in (bk.get("bids") or [])
                 if float(l["price"]) >= mid - ms_)
        qa = sum(float(l["price"]) * float(l["size"]) for l in (bk.get("asks") or [])
                 if float(l["price"]) <= mid + ms_)
        try:
            msz = float(r["minSize"])
        except Exception:
            msz = np.nan
        dep.append({"cls": r["cls"], "q": r["q"][:40], "mid": mid, "rate": r["rate"],
                    "minSize": msz, "vmin": msz * mid * 2 if np.isfinite(msz) else np.nan,
                    "D": qb + qa})
        time.sleep(0.2)

    dd = pd.DataFrame(dep)
    if len(dd):
        dd = dd[dd["vmin"].notna() & (dd["vmin"] > 0)]
        dd["y_min"] = dd["rate"] / (dd["vmin"] + dd["D"]) * 365
        dd["y_3x"] = dd["rate"] / (dd["vmin"] * 3 + dd["D"]) * 365
        dd["y_10x"] = dd["rate"] / (dd["vmin"] * 10 + dd["D"]) * 365
        log("")
        log("  %-12s %-38s %7s %8s %10s %9s %9s %9s" %
            ("分类", "问题", "rate", "V_min", "竞争者D", "年化1x", "年化3x", "年化10x"))
        log("  " + "-" * 104)
        for _, r in dd.sort_values("y_min", ascending=False).head(16).iterrows():
            log("  %-12s %-38s %7.0f %8.0f %10.0f %8.1f%% %8.1f%% %8.1f%%" %
                (r["cls"], r["q"], r["rate"], r["vmin"], r["D"],
                 r["y_min"] * 100, r["y_3x"] * 100, r["y_10x"] * 100))
        log("")
        log("  --- 统计（只含【有参照价】的类别）---")
        log("  最小资金下年化收益率: 中位=%.1f%% 均值=%.1f%% max=%.1f%%" %
            (dd["y_min"].median() * 100, dd["y_min"].mean() * 100, dd["y_min"].max() * 100))
        log("  放大到 3x 资金:        中位=%.1f%%" % (dd["y_3x"].median() * 100))
        log("  放大到 10x 资金:       中位=%.1f%%  <- 收益率的自我稀释" %
            (dd["y_10x"].median() * 100))
        log("")
        log("  ★ 收益率随资金【快速衰减】是这类策略的核心特征：")
        log("     奖励池是固定的，你投入越多，单位资金的收益率越低。")
        log("     => 它有一个【最优资金规模】，而不是「越多越好」。")
        log("")
        log("  ⚠️ 三个必须注意的风险（做市不是无风险收租）：")
        log("     1. 【逆向选择】你的挂单只在价格朝不利方向走时成交 ——")
        log("        奖励是补偿你承担这个风险的，不是白拿的。")
        log("     2. 【库存风险】成交后你持有头寸，比赛结果出来前价格会波动。")
        log("     3. 【奖励规则变化】rate 和 maxSpread 由平台调整，随时可能变。")
        v = dd["vmin"].dropna()
        log("")
        log("  启动资金参考：")
        log("     单市场最小占用资金 V_min: 中位=$%.0f 均值=$%.0f max=$%.0f" %
            (v.median(), v.mean(), v.max()))
        for n in (3, 5, 10):
            log("     同时做 %2d 个市场 ⇒ 约 $%.0f" % (n, v.median() * n))
        dd.to_csv(r"D:\26050\Documents\quant_system\output_poly_maker.csv", index=False)

    _write(df)


def _write(df):
    df.to_csv(r"D:\26050\Documents\quant_system\output_poly_technical.csv", index=False)
    with open(r"D:\26050\Documents\quant_system\output_poly_technical.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_technical.txt / .csv")


if __name__ == "__main__":
    main()
