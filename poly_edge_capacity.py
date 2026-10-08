"""
poly_edge_capacity.py - 决定性检验：那个 0.10-0.15 的边际，能不能真的交易？
============================================================================
前面的分档校准（validate.py）用**全部**同一批市场算出的边际是 2.2~2.9pp。
但 validate.py 第 4 节已经露了一个尾巴：
  低成交额档偏差为负（-2.01%），高成交额档偏差为正（+1.3%~+4.2%）
如果边际只存在于低成交额市场里，那它【不可交易】—— 与资金多少无关。

所以本脚本做两件事：
  1. 在 0.10-0.15 档内部，按成交额分层重算边际 + 标准误
  2. 给出"可交易容量"上限：成交额能承受多大的仓位

⚠️ 数据限制（必须写进结论）：
   dataset_all.csv 里没有成交额的分时数据，volume 是【市场累计成交额】，
   不是建仓时刻的即时深度。所以下面算的是【这个市场总共交易过多少钱】，
   作为"能否容纳仓位"的代理。
"""

import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, r"D:\26050\Documents\quant_system")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np

DS = r"D:\26050\Documents\polymarket_sports\data\dataset_all.csv"
OUT = []
BUCKET_LO, BUCKET_HI = 0.10, 0.15


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def main():
    rows = []
    with open(DS, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["price"] = float(r["price"])
            r["outcome"] = int(r["outcome"])
            r["volume"] = float(r["volume"] or 0)
            r["lead_days"] = int(r["lead_days"])
            rows.append(r)
    log("总行数 %d" % len(rows))
    lead_set = sorted({r["lead_days"] for r in rows}, reverse=True)

    # 受控样本：所有时点都有数据（消除样本构成偏差，与 validate.py 一致）
    by_mid = defaultdict(dict)
    for r in rows:
        by_mid[r["market_id"]][r["lead_days"]] = r
    common = {m: d for m, d in by_mid.items() if all(l in d for l in lead_set)}
    log("受控样本（全时点都有数据）: %d 个市场" % len(common))
    log("")

    def stats(sel):
        n = len(sel)
        if n < 15:
            return None
        p = np.mean([r["price"] for r in sel])
        r_ = np.mean([r["outcome"] for r in sel])
        se = np.sqrt(r_ * (1 - r_) / n)
        return {"n": n, "p": p, "r": r_, "edge": p - r_, "se": se,
                "t": (p - r_) / se if se > 0 else np.nan}

    # ------------------------------------------------------------------
    # 1. 0.10-0.15 档内部按成交额分层
    # ------------------------------------------------------------------
    log("=" * 100)
    log("1. 决定性检验：0.10-0.15 档内部，按【市场累计成交额】分层")
    log("=" * 100)
    log("")
    log("  edge = 均价 p − 实际胜率 r  （>0 表示该档被高估 ⇒ 买 NO 有正期望）")
    log("")
    bands = [(0, 1e3), (1e3, 1e4), (1e4, 1e5), (1e5, 1e6), (1e6, 1e12)]
    for lead in (7, 3, 1):
        sel = [d[lead] for d in common.values()]
        sub = [r for r in sel if BUCKET_LO <= r["price"] < BUCKET_HI]
        log("  --- 结算前 %d 天（该档共 %d 个）---" % (lead, len(sub)))
        log("  %-22s %6s %8s %8s %9s %8s %7s %8s" %
            ("累计成交额", "样本", "均价p", "胜率r", "edge", "SE", "t值", "显著?"))
        log("  " + "-" * 92)
        for lo, hi in bands:
            b = [r for r in sub if lo <= r["volume"] < hi]
            st = stats(b)
            if st is None:
                log("  %-22s %6d %8s %8s %9s %8s %7s %8s" %
                    (_b(lo, hi), len(b), "-", "-", "-", "-", "-", "样本少"))
                continue
            sig = "是" if abs(st["t"]) > 2 else "**否**"
            log("  %-22s %6d %8.4f %8.4f %+9.4f %8.4f %7.2f %8s" %
                (_b(lo, hi), st["n"], st["p"], st["r"], st["edge"], st["se"], st["t"], sig))
        st_all = stats(sub)
        if st_all:
            log("  %-22s %6d %8.4f %8.4f %+9.4f %8.4f %7.2f %8s" %
                ("【全档合计】", st_all["n"], st_all["p"], st_all["r"],
                 st_all["edge"], st_all["se"], st_all["t"],
                 "是" if abs(st_all["t"]) > 2 else "**否**"))
        log("")

    # ------------------------------------------------------------------
    # 2. 边际在哪一档？—— 用 >= 某门槛 的累计视角
    # ------------------------------------------------------------------
    log("=" * 100)
    log("2. 累计视角：只交易「成交额 ≥ 门槛」的市场时，边际还剩多少？")
    log("=" * 100)
    log("")
    log("  %-16s %8s %9s %8s %8s %7s" %
        ("成交额门槛", "样本", "edge", "SE", "t值", "显著?"))
    log("  " + "-" * 92)
    for lead in (7, 3, 1):
        sel = [d[lead] for d in common.values()]
        sub = [r for r in sel if BUCKET_LO <= r["price"] < BUCKET_HI]
        log("  --- 结算前 %d 天 ---" % lead)
        for th in (0, 1e3, 1e4, 1e5, 1e6):
            b = [r for r in sub if r["volume"] >= th]
            st = stats(b)
            if st is None:
                log("  %-16s %8d %9s %8s %8s %7s" % ("≥ $%s" % _m(th), len(b), "-", "-", "-", "样本少"))
                continue
            sig = "是" if abs(st["t"]) > 2 else "**否**"
            log("  %-16s %8d %+9.4f %8.4f %7.2f %7s" %
                ("≥ $%s" % _m(th), st["n"], st["edge"], st["se"], st["t"], sig))
        log("")

    # ------------------------------------------------------------------
    # 3. 容量：这笔钱能投多深？
    # ------------------------------------------------------------------
    log("=" * 100)
    log("3. 容量上限：把整个 0.10-0.15 档的市场全买一遍，能投多少钱？")
    log("=" * 100)
    log("")
    sel = [d[7] for d in common.values()]
    sub = [r for r in sel if BUCKET_LO <= r["price"] < BUCKET_HI]
    vols = np.array([r["volume"] for r in sub])
    log("  7 天前处于 0.10-0.15 档的市场: %d 个" % len(sub))
    log("  累计成交额分布: 中位=$%.0f 均值=$%.0f p90=$%.0f max=$%.0f"
        % (np.median(vols), vols.mean(), np.percentile(vols, 90), vols.max()))
    log("  合计累计成交额 = $%.0f" % vols.sum())
    log("")
    log("  ⚠️ 关键：成交额是【整个市场生命周期】的累计量，不是你能吃到的深度。")
    log("     业界经验：单笔最多吃到累计成交额的 1%~5% 而不显著推价。")
    for fr in (0.01, 0.05):
        cap = vols.sum() * fr
        log("     按 %.0f%% 计 ⇒ 该批标的的合计可容纳约 **$%.0f**" % (fr * 100, cap))
    log("")
    log("  但这是【全部标的加起来】的容量，且要按持有期分割：")
    log("     若持有 7 天、且这 %d 个标的分布在 3.9 年（参见 output_poly_costs.txt），" % len(sub)
        )
    log("     则平均每年约 %.0f 个标的 ⇒ 同时可持有约 %.0f 个仓位。"
        % (len(sub) / 3.9, len(sub) / 3.9 * 7 / 365))
    log("     按中位累计成交额 $%.0f 的 5%% 作单仓上限 ⇒ 单仓约 $%.0f" %
        (np.median(vols), np.median(vols) * 0.05))
    log("     ⇒ **可以用到的资金上限大约是几百美元量级。**")
    log("")
    log("  ★ 结论三（容量层）：这不是「资金不够」，而是**标的本身装不下钱**。")
    log("     低成交额市场的边际即使为真，也只能支持一个几百美元规模的策略 ——")
    log("     这个规模下，AI 订阅费都未必赚得回来。")

    with open(r"D:\26050\Documents\quant_system\output_poly_edge_capacity.txt",
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_edge_capacity.txt")


def _b(lo, hi):
    return "$%s-%s" % (_m(lo), "∞" if hi >= 1e11 else _m(hi))


def _m(v):
    if v >= 1e6:
        return "%.0fM" % (v / 1e6)
    if v >= 1e3:
        return "%.0fK" % (v / 1e3)
    return "%.0f" % v


if __name__ == "__main__":
    main()
