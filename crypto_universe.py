"""
crypto_universe.py - 加密币种池：回答"该用哪些币"以及量化幸存者偏差
============================================================================
两个问题：
  Q1 该用哪些币？—— 本项目现有 30 币，但"存活到今天"本身就是筛选条件
  Q2 幸存者偏差有多大？—— 必须量化，否则任何回测结论都不可信

做法：
  1. 拉 Binance exchangeInfo，拿到**所有** USDT 现货交易对的 onboardDate
     （上市时间，Binance 官方字段，可 point-in-time 重建币种池）
  2. 对比"本项目 30 币"与"全部曾上市交易对"，量化缺了多少
  3. 用 onboardDate 重建每个历史时点的可交易币种池 —— 这才是无前视的池子
  4. 估计偏差方向与量级

⚠️ 关键限制（必须写在结论里）：
     Binance exchangeInfo 只返回**当前仍在交易**的交易对。
     **已下架的（=真正死掉的）币不会出现。**
     所以本脚本能算出"上市了多少"，但**算不出"死了多少"** ——
     而后者正是幸存者偏差的核心。这是一个数据源层面的硬限制，
     不是实现问题。我们只能给出【下界估计】＋【偏差方向】。
"""

import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

import crypto_data as CD

OUT = []
CACHE = "data/crypto/_exchangeinfo.json"
LISTING_CACHE = "data/crypto/_listing_dates.json"

# ⚠️ api.binance.com 在本机【连接超时】（已被墙），
#    但 Binance 的公开数据镜像 data-api.binance.vision 可用（实测 200）。
#    该镜像的 exchangeInfo **不含 onboardDate**，所以改用
#    「该交易对的第一根 K 线时间」作为上市时间的等价代理
#    （实测与 data/crypto 里已落盘的起始日完全一致：BTC 2017-08-17、TIA 2023-10-31）。
BASE_MIRROR = "https://data-api.binance.vision"


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def fetch_exchange_info(force=False):
    """拉取并缓存 exchangeInfo（注意：镜像版本不含 onboardDate）。"""
    if os.path.exists(CACHE) and not force:
        return json.load(io.open(CACHE, encoding="utf-8"))
    import requests
    r = requests.get(BASE_MIRROR + "/api/v3/exchangeInfo", timeout=120)
    r.raise_for_status()
    j = r.json()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(j, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return j


def fetch_listing_dates(symbols, force=False, workers=8, verbose=True):
    """
    用「第一根日线的时间」作为上市时间代理。并发抓取并缓存。

    为什么不用 onboardDate：可用的镜像端点不返回该字段。
    为什么第一根 K 线是合理代理：交易对开始撮合的第一根 K 线
    就是它可交易的第一天；实测与本地已落盘数据的起始日完全一致。
    """
    cache = {}
    if os.path.exists(LISTING_CACHE) and not force:
        cache = json.load(io.open(LISTING_CACHE, encoding="utf-8"))
    todo = [s for s in symbols if s not in cache]
    if todo:
        import requests
        from concurrent.futures import ThreadPoolExecutor

        def one(sym):
            try:
                r = requests.get(BASE_MIRROR + "/api/v3/klines",
                                 params={"symbol": sym, "interval": "1d",
                                         "limit": 1, "startTime": 0},
                                 timeout=30)
                if r.status_code == 200 and r.json():
                    return sym, r.json()[0][0] / 1000.0
            except Exception:
                return sym, None
            return sym, None

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, ts in ex.map(one, todo):
                if ts:
                    cache[sym] = ts
                done += 1
                if verbose and done % 150 == 0:
                    print("  [listing] %d/%d" % (done, len(todo)), flush=True)
        os.makedirs(os.path.dirname(LISTING_CACHE), exist_ok=True)
        json.dump(cache, io.open(LISTING_CACHE, "w", encoding="utf-8"))
    return cache


def main():
    log("=" * 104)
    log("加密币种池分析：该用哪些币 + 幸存者偏差量化")
    log("=" * 104)
    log("")

    # ---------- 1. 现有池子 ----------
    log("=" * 104)
    log("1. 本项目现有币种池（30 币）")
    log("=" * 104)
    meta = json.load(io.open("data/crypto/_meta.json", encoding="utf-8"))
    rows = []
    for k, v in meta.items():
        if k.endswith("_1d"):
            rows.append({"symbol": k[:-3], "rows": v.get("rows"),
                         "start": v.get("start"), "end": v.get("end")})
    df = pd.DataFrame(rows).sort_values("start").reset_index(drop=True)
    df["days"] = df["rows"]
    df["years"] = (df["days"] / 365.0).round(1)
    log("  %-10s %8s %8s %-12s %-12s" % ("symbol", "days", "years", "start", "end"))
    for _, r in df.iterrows():
        log("  %-10s %8d %8.1f %-12s %-12s" %
            (r["symbol"], r["days"], r["years"], r["start"], r["end"]))
    log("")
    log("  总计 %d 币；历史最长 %.1f 年（BTC/ETH），最短 %.1f 年（%s）" %
        (len(df), df["years"].max(), df["years"].min(),
         df.loc[df["years"].idxmin(), "symbol"]))
    log("")
    log("  ⚠️ 只有 BTC/ETH 有 2017 起的完整历史。")
    log("     2017-08 起算时只有 2 个币，2020-09 之后才陆续到 20+ 个。")
    log("     ⇒ **早期样本的横截面宽度极小**，2017-2019 的选币类结论基本不可用。")

    # ---------- 2. 全部上市交易对 ----------
    log("")
    log("=" * 104)
    log("2. Binance 全部 USDT 现货交易对 + 上市时间（point-in-time 重建币种池）")
    log("=" * 104)
    log("  ⚠️ api.binance.com 在本机被墙（连接超时），改用公开镜像")
    log("     data-api.binance.vision（实测 200）。该镜像 exchangeInfo 不含 onboardDate，")
    log("     故用「第一根日线时间」作为上市时间代理。")
    log("")
    try:
        info = fetch_exchange_info()
    except Exception as e:
        log("  ⚠️ 拉取 exchangeInfo 失败（%s）—— 跳过第 2/3 节。" % str(e)[:60])
        _write()
        return

    syms = [s["symbol"] for s in info.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
    log("  当前 TRADING 的 USDT 现货交易对: %d 个" % len(syms))
    log("  ⚠️⚠️ 这只含【当前仍在交易】的 —— 已下架的（真正死掉的）币不会出现。")
    log("     所以下面算出的都是【下界】，真正的偏差只会更大。")
    log("")

    ld = fetch_listing_dates(syms)
    log("  成功取到上市时间: %d/%d" % (len(ld), len(syms)))

    listed = pd.DataFrame({
        "symbol": list(ld.keys()),
        "onboard": [pd.to_datetime(t, unit="s") for t in ld.values()],
    }).sort_values("onboard").reset_index(drop=True)

    mine = set(df["symbol"])
    listed["in_mine"] = listed["symbol"].isin(mine)
    log("")
    log("  本项目 30 币在其中的占比: %d/%d" % (int(listed["in_mine"].sum()), len(listed)))
    log("  ⇒ 也就是说：**当前在 Binance 交易的 USDT 对里有 %d 个不在本项目的池子里**。"
        % (len(listed) - int(listed["in_mine"].sum())))
    log("     这些里既有流动性不足的长尾（合理排除），")
    log("     也有**流动性与本项目所持币相当、但被漏掉**的中型币。")

    # ---------- 3. 逐年：上市数 vs 我池子可用数 ----------
    log("")
    log("=" * 104)
    log("3. 逐年对比：该年末【已上市且活到今天】的交易对数 vs 本项目实际可用的币数")
    log("=" * 104)
    log("  %-8s %16s %14s %12s %12s" %
        ("年末", "已上市(活到今天)", "本项目可用币", "覆盖率", "缺口"))
    log("  " + "-" * 96)
    for y in range(2018, 2027):
        cut = pd.Timestamp("%d-12-31" % y)
        n_listed = int((listed["onboard"] <= cut).sum())
        n_mine = int((pd.to_datetime(df["start"]) <= cut).sum())
        cov = n_mine / n_listed * 100 if n_listed else 0
        log("  %-8d %16d %14d %11.1f%% %12d" %
            (y, n_listed, n_mine, cov, n_listed - n_mine))

    log("")
    log("  ★ 这张表的正确读法（很关键）：")
    log("     第 2 列 = 当时已上市、且**至今没被下架**的交易对数 —— 这是个【下界】，")
    log("              因为当时上市后来死掉的币不在这 506 个里面。")
    log("     第 5 列「缺口」= 本项目漏掉的币数，其中大部分是流动性不足的长尾。")
    log("  ⇒ 所以「覆盖率低」本身不等于偏差大；关键是漏掉的**流动性分布**。")

    # ---------- 3b. 漏掉的币里，有多少是【真的能交易】的？ ----------
    log("")
    log("=" * 104)
    log("3b. 缺口里有多少是【流动性足够、本来能交易】的？（这才是真实缺口）")
    log("=" * 104)
    try:
        import requests
        r = requests.get(BASE_MIRROR + "/api/v3/ticker/24hr", timeout=90)
        tk = pd.DataFrame(r.json())
        tk["quoteVolume"] = pd.to_numeric(tk["quoteVolume"], errors="coerce")
        # 取最近 30 日的平均成交额近似需要额外调用；这里用 24h 成交额作即时快照
        m = tk[["symbol", "quoteVolume"]].dropna()
        m = m[m["symbol"].isin(set(listed["symbol"]))]
        miss = m[~m["symbol"].isin(mine)]
        MINE_MIN_DV = 3e6
        log("  用【24h 成交额】作即时流动性快照（与项目里 rolling(30) 的 ADV 口径不同，")
        log("  但足以判断「这个币现在是否真能交易」）：")
        log("")
        for th in (1e6, 3e6, 1e7, 5e7, 1e8):
            n = int((miss["quoteVolume"] >= th).sum())
            log("    24h 成交额 >= $%-10s : 漏掉的币里有 %3d 个（本项目池子共 30 个）" %
                ("%.0fM" % (th / 1e6), n))
        above = miss[miss["quoteVolume"] >= MINE_MIN_DV].sort_values(
            "quoteVolume", ascending=False)
        log("")
        log("  ★ 结论：以本项目自己的门槛 $3M 计，漏掉了 **%d 个流动性与所持币相当**的币。" %
            len(above))
        log("     按 24h 成交额排名前 15 的漏掉币种：")
        log("     %-12s %16s" % ("symbol", "24h成交额(USD)"))
        for _, rr in above.head(15).iterrows():
            log("     %-12s %16.0f" % (rr["symbol"], rr["quoteVolume"]))
        log("")
        log("  ⇒ 这不是幸存者偏差（这些币还活着），而是**选币范围过窄**。")
        log("     它是【可以修】的：把池子按 ADV 排名动态扩展即可。")
        log("     而真正不可修的是下面第 4 节的死币缺口。")
    except Exception as e:
        log("  ⚠️ 拉取 ticker/24hr 失败（%s）—— 跳过本节。" % str(e)[:60])
    # ---------- 4. 偏差方向与量级 ----------
    log("")
    log("=" * 104)
    log("4. 幸存者偏差：方向与量级")
    log("=" * 104)
    log("  偏差方向（可以确定的）：")
    log("    本项目 30 币 = 【活到今天且仍在 Binance 交易】的币。")
    log("    而历史上任何一个时点的可交易池里，都包含**后来死掉的币** ——")
    log("    LUNA、FTT、SRM、BTT、以及大量跌 99% 后下架的长尾。")
    log("    用今天的存活者去回测历史 ⇒ **系统性高估收益、低估尾部**。")
    log("")
    log("  量级估计（用第 3 节的覆盖率）：")
    for y in (2019, 2021, 2023, 2026):
        cut = pd.Timestamp("%d-12-31" % y)
        n_listed = int((listed["onboard"] <= cut).sum())
        n_mine = int((pd.to_datetime(df["start"]) <= cut).sum())
        log("    %d 年：现存交易对 %d，本项目只有 %d 个（覆盖率 %.0f%%）"
            % (y, n_listed, n_mine, n_mine / n_listed * 100 if n_listed else 0))
    log("")
    log("  ⚠️ 但这个覆盖率**不能直接读成偏差大小**，原因：")
    log("     · 分母只含现存交易对 ⇒ 缺的是「下架＋现存但被排除」两部分之和")
    log("     · 长尾交易对流动性极差，本来就进不了可交易池（本项目 min_dv=3e6）")
    log("     · 真正致命的是「活过一段时间的死币」（LUNA 类），不是纯长尾")
    log("")

    # 2021 年的具体检验：当时市值/成交额靠前但今天不在池子里的
    log("  一个可操作的检验：2021 年成交额领先、但今天已不在 Binance 的项目")
    log("    （从交易所官方信息无法回溯，只能列已知案例作为定性证据）")
    known_dead = {
        "LUNAUSDT": "2022-05 UST 脱锚，LUNA 归零，改名 LUNC 后价值趋零",
        "FTTUSDT": "2022-11 FTX 破产",
        "SRMUSDT": "FTX 生态，2022-11 后归零",
        "ANTUSDT": "Aragon 2024 解散",
        "WAVESUSDT": "2022 后长期阴跌，成交额跌出前列",
    }
    now = set(listed["symbol"])
    for s, why in known_dead.items():
        log("    %-11s %-8s %s" % (s, "(仍在交易)" if s in now else "(已不在)", why))
    log("")
    log("  ★ 关键结论：这些币**都不在本项目 30 币里**，")
    log("     也就是说，如果 2021 年做横截面选币，本项目**根本选不到它们** ——")
    log("     而真实策略会选到，并在 2022 年承受 −99% 级别的损失。")
    log("     ⇒ 本项目所有加密回测的收益是【乐观边界】，不是期望值。")

    # ---------- 5. 无前视币种池的实现 ----------
    log("")
    log("=" * 104)
    log("5. 如何构造无前视（point-in-time）币种池")
    log("=" * 104)
    log("  用 onboardDate 只能解决「何时上市」，解决不了「何时死掉」。")
    log("  完整的 point-in-time 池需要逐日快照「当日仍在交易的交易对集合」，")
    log("  这需要每日抓 exchangeInfo 并落盘（从今天开始积累），")
    log("  或使用有历史成分股的第三方数据源。")
    log("")
    log("  本项目可先做的【改进型近似】：")
    log("    · 用 onboardDate 硬约束「只能用已上市的币」（消除「用了还没上市的币」的前视）")
    log("    · 流动性门槛从【全样本平均】改为【滚动窗口】（已是现状：rolling(30)）")
    log("    · 显式记录未建模的偏差量级，在结论里把它作为不确定性来源列出 ✅（本文档在做）")
    log("")
    log("  实现：见 `crypto_universe.py` 的 `listed_before()` / `point_in_time_mask()`")

    _write()


def listed_before(listed: pd.DataFrame, date) -> set:
    """返回 date 之前已上市的 symbol 集合（可 point-in-time 调用）。"""
    return set(listed.loc[listed["onboard"] <= pd.Timestamp(date), "symbol"])


def point_in_time_mask(prices: pd.DataFrame, listed: pd.DataFrame) -> pd.DataFrame:
    """
    构造 date×coin 的布尔掩码：当日该币「已上市」才为 True。
    这是消除"用了还没上市的币"这类【硬前视】的最小修补。
    """
    onb = pd.Series({s: d for s, d in zip(listed["symbol"], listed["onboard"])})
    mask = pd.DataFrame(False, index=prices.index, columns=prices.columns)
    for c in prices.columns:
        if c in onb.index:
            mask[c] = prices.index >= onb[c]
        else:
            # 不在 exchangeInfo 里的（如已下架）保守地视为一直可用
            mask[c] = True
    return mask


def _write():
    with open("output_crypto_universe.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_crypto_universe.txt")


if __name__ == "__main__":
    main()
