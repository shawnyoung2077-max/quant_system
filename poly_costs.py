"""
poly_costs.py - 从本地已结算市场元数据里提取真实交易成本与供给
============================================================================
元数据里直接有 best_bid / best_ask / spread / end_date，所以：
  · 买卖价差可以用【实测分布】，不用记忆值
  · 日历区间可以用 end_date 得到 ⇒ 能算"每年多少个标的"
"""

import glob
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

DATA = r"D:\26050\Documents\polymarket_sports\data\markets"
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def main():
    fs = sorted(glob.glob(os.path.join(DATA, "*.json")))
    log("[files] %d 个元数据文件" % len(fs))

    recs = []
    for i, f in enumerate(fs):
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, list):
            d = [d]
        for m in d:
            recs.append({
                "market_id": m.get("market_id"),
                "end_date": m.get("end_date"),
                "start_date": m.get("start_date"),
                "volume": m.get("volume"),
                "liquidity": m.get("liquidity"),
                "best_bid": m.get("best_bid"),
                "best_ask": m.get("best_ask"),
                "spread": m.get("spread"),
                "last": m.get("last_trade_price"),
                "league": m.get("league"),
            })
        if (i + 1) % 80 == 0:
            log("  ... %d/%d 文件, %d 条" % (i + 1, len(fs), len(recs)))

    df = pd.DataFrame(recs)
    log("[meta] 共 %d 个市场" % len(df))

    for c in ("end_date", "start_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
    for c in ("volume", "liquidity", "best_bid", "best_ask", "spread", "last"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    log("")
    log("=" * 100)
    log("1. 日历区间（决定「每年多少个标的」）")
    log("=" * 100)
    e = df["end_date"].dropna()
    log("  结算日范围: %s ~ %s" % (e.min().date(), e.max().date()))
    log("  跨度: %.2f 年" % ((e.max() - e.min()).days / 365.25))
    span_y = max((e.max() - e.min()).days / 365.25, 0.01)

    log("")
    log("=" * 100)
    log("2. 买卖价差：实测分布（不用记忆值）")
    log("=" * 100)
    sp = df["spread"].dropna()
    log("  spread 全样本: n=%d 中位=%.4f 均值=%.4f p90=%.4f p99=%.4f max=%.4f"
        % (len(sp), sp.median(), sp.mean(), sp.quantile(.9), sp.quantile(.99), sp.max()))
    # 重新算 bid/ask 价差（更可靠）
    df["sp_calc"] = df["best_ask"] - df["best_bid"]
    spc = df["sp_calc"].dropna()
    log("  ask−bid 自算  : n=%d 中位=%.4f 均值=%.4f p90=%.4f"
        % (len(spc), spc.median(), spc.mean(), spc.quantile(.9)))

    # 只看向 0.10-0.15 的合约（用 last_trade_price 判定价格所在档）
    mid = (df["best_bid"] + df["best_ask"]) / 2
    sel = df[(mid >= 0.10) & (mid < 0.15)]
    log("")
    log("  0.10-0.15 档（按 bid/ask 中间价筛选）: n=%d" % len(sel))
    if len(sel):
        s2 = sel["sp_calc"].dropna()
        if len(s2):
            log("    该档 ask−bid: 中位=%.4f 均值=%.4f p10=%.4f p90=%.4f"
                % (s2.median(), s2.mean(), s2.quantile(.1), s2.quantile(.9)))
            log("    该档中间价均值 = %.4f" % float(mid[sel.index].mean()))
    # 也看 NO 侧（0.85-0.90），因为买 NO 才是实际动作
    sel_no = df[(mid >= 0.85) & (mid < 0.90)]
    log("")
    log("  0.85-0.90 档（= 买 NO 的价格区间）: n=%d" % len(sel_no))
    if len(sel_no):
        s3 = sel_no["sp_calc"].dropna()
        if len(s3):
            log("    该档 ask−bid: 中位=%.4f 均值=%.4f" % (s3.median(), s3.mean()))
            log("    该档中间价均值 = %.4f" % float(mid[sel_no.index].mean()))

    log("")
    log("=" * 100)
    log("3. 供给：每年有多少个 0.10-0.15 档标的？")
    log("=" * 100)
    df["ym"] = df["end_date"].dt.to_period("M")
    cnt = df[df["market_id"].notna()].groupby("ym").size()
    log("  每月结算市场数（全档）: 中位=%.0f 均值=%.0f" % (cnt.median(), cnt.mean()))
    sel_all = df[(mid >= 0.10) & (mid < 0.15)]
    cnt2 = sel_all.groupby("ym").size()
    log("  每月 0.10-0.15 档市场数: 中位=%.0f 均值=%.0f"
        % (cnt2.median() if len(cnt2) else 0, cnt2.mean() if len(cnt2) else 0))
    log("")
    log("  ⚠️ 注意：这里只用【当前快照】的 bid/ask 判定价格档，")
    log("     而价格是随时变的 —— 所以这个计数是【上界量级】，不是精确的可交易数。")

    log("")
    log("=" * 100)
    log("4. 成交额与流动性（决定单笔能下多大）")
    log("=" * 100)
    v = df["volume"].dropna()
    log("  volume 全样本: 中位=$%.0f 均值=$%.0f" % (v.median(), v.mean()))
    s = df[(mid >= 0.10) & (mid < 0.15)]["volume"].dropna()
    if len(s):
        log("  0.10-0.15 档 volume: n=%d 中位=$%.0f 均值=$%.0f p90=$%.0f"
            % (len(s), s.median(), s.mean(), s.quantile(.9)))
    liq = df[(mid >= 0.10) & (mid < 0.15)]["liquidity"].dropna()
    if len(liq):
        log("  0.10-0.15 档 liquidity: 中位=$%.0f 均值=$%.0f" % (liq.median(), liq.mean()))
    # 汇总用于 breakeven
    res = {
        "span_years": span_y,
        "spread_median_all": float(spc.median()) if len(spc) else np.nan,
        "spread_median_bucket": float(sel["sp_calc"].dropna().median())
        if len(sel) and len(sel["sp_calc"].dropna()) else np.nan,
        "mid_bucket": float(mid[sel.index].mean()) if len(sel) else np.nan,
        "n_bucket_markets": int(len(sel)),
        "vol_median_bucket": float(s.median()) if len(s) else np.nan,
    }
    log("")
    log("[汇总] " + json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in res.items()}, ensure_ascii=False))
    json.dump(res, io.open(os.path.join(r"D:\26050\Documents\quant_system",
                                        "_poly_costs.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    with open(r"D:\26050\Documents\quant_system\output_poly_costs.txt", "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_costs.txt / _poly_costs.json")


if __name__ == "__main__":
    main()
