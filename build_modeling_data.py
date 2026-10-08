"""
build_modeling_data.py - 把历史数据整理成"可直接建模"的干净交付包
============================================================================
用户安排：我这个模型负责整理数据，建模交给别的模型。
所以产物必须满足：
  · 字段含义清晰、无歧义（别的模型看不到我的推理过程）
  · 已知结论与未决问题写清楚（避免重复劳动 / 重复踩坑）
  · 无需复现我的代码就能直接读入建模

产出（都在 modeling/ 下）：
  historical_rows.csv      主建模表（一行 = 一个市场 x 一个观察时点）
  league_summary.csv       逐联赛校准统计
  price_band_summary.csv   逐价格档校准统计
  DATA_DICTIONARY.md       逐字段说明
  HANDOFF.md               已知结论 / 未决问题 / 建模任务
"""

import csv
import collections
import json
import os
import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

SRC = r"D:\26050\Documents\polymarket_sports\data\dataset_all.csv"
OUT = r"D:\26050\Documents\quant_system\modeling"
os.makedirs(OUT, exist_ok=True)


def main():
    rows = []
    with open(SRC, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                r["price"] = float(r["price"])
                r["outcome"] = int(r["outcome"])
                r["volume"] = float(r["volume"] or 0)
                r["n_points"] = int(r["n_points"])
                r["lead_days"] = int(r["lead_days"])
                r["neg_risk"] = int(r["neg_risk"] or 0)
                r["final_price"] = float(r["final_price"]) if r["final_price"] not in ("", None) else np.nan
                r["drift"] = float(r["drift"]) if r["drift"] not in ("", None) else np.nan
            except Exception:
                continue
            rows.append(r)
    df = pd.DataFrame(rows)
    print("原始行数 %d，市场数 %d" % (len(df), df["market_id"].nunique()))

    # ---------------- 派生特征 ----------------
    # 空类型要显式处理：pandas 把 CSV 里的 "" 读成 NaN，
    # 直接 isin([""]) 会全程 False，把 14920 个胜负市场误标成非胜负。
    df["sports_market_type"] = df["sports_market_type"].fillna("")

    # 联赛"冷门程度"——两个代理，各有优劣，都给出让建模方自己选：
    #   A. league_total_volume    总额。会被"市场数量多"污染
    #      （世界杯 2986 个市场 x 大额 → 总额第一，但那不代表它好做）
    #   B. league_median_mkt_vol  联赛内【中位每市场成交额】。更接近
    #      "这个联赛的典型市场有多少钱在参与" = 机构覆盖强度的代理
    lg_vol = df.groupby("league_name")["volume"].sum()
    lg_med = df.groupby("league_name")["volume"].median()
    lg_cnt = df.groupby("league_name")["market_id"].nunique()
    df["league_total_volume"] = df["league_name"].map(lg_vol)
    df["league_median_mkt_vol"] = df["league_name"].map(lg_med)
    df["league_market_count"] = df["league_name"].map(lg_cnt)

    # 用【联赛四分位】而不是中位数 —— 前者能给出 ~25% 的联赛样本，
    # 后者会让 99% 的行落在"热门"一侧，分层建模没法用。
    lgdf = pd.DataFrame({"total_volume": lg_vol, "median_mkt_vol": lg_med,
                         "n_markets": lg_cnt}).reset_index()
    lgdf["vol_quartile"] = pd.qcut(lgdf["total_volume"], 4, labels=[1, 2, 3, 4]
                                   ).astype(int)          # 1 = 最冷
    lgdf["medmkt_quartile"] = pd.qcut(lgdf["median_mkt_vol"], 4, labels=[1, 2, 3, 4],
                                      duplicates="drop").astype(int)
    q_map = dict(zip(lgdf["league_name"], lgdf["vol_quartile"]))
    qm_map = dict(zip(lgdf["league_name"], lgdf["medmkt_quartile"]))
    df["league_vol_quartile"] = df["league_name"].map(q_map)     # 1 最冷 .. 4 最热
    df["league_medmkt_quartile"] = df["league_name"].map(qm_map)
    df["league_is_cold"] = (df["league_vol_quartile"] == 1).astype(int)
    df["league_is_cold_medmkt"] = (df["league_medmkt_quartile"] == 1).astype(int)

    # ★ 市场级"注意力"分位 —— 这是最佳可用代理。
    #   为什么必须有它：联赛级分位虽然概念更贴近"机构覆盖"，
    #   但【冷门联赛本来就没几个市场】——实测 179 个联赛取四分位后，
    #   最冷那一档只贡献 119 行（占 0.16%），分层建模没法用。
    #   市场级成交额分位能把 74242 行大致均分到 4 档，样本充足。
    #   概念上：成交额高的市场 = 有人认真研究 = 接近有效；
    #          成交额低的市场 = 没人看 = 错价可能更大（用户假设的市场级版本）
    df["mkt_vol_quartile"] = pd.qcut(df["volume"], 4, labels=[1, 2, 3, 4],
                                     duplicates="drop").astype(int)   # 1 = 最小额
    df["mkt_vol_decile"] = pd.qcut(df["volume"], 10, labels=False,
                                   duplicates="drop").astype(int) + 1

    df["price_band"] = pd.cut(df["price"],
                              [0, .05, .10, .15, .20, .30, .50, .70, .90, 1.01],
                              labels=["0-.05", ".05-.10", ".10-.15", ".15-.20",
                                      ".20-.30", ".30-.50", ".50-.70", ".70-.90",
                                      ".90-1.0"])
    df["calib_err"] = df["outcome"] - df["price"]      # 实际 − 隐含
    df["abs_calib_err"] = df["calib_err"].abs()
    # 买 NO 的每股毛期望（= 价格 − 实际胜率）
    df["edge_no"] = df["price"] - df["outcome"]
    # 买 YES 的每股毛期望
    df["edge_yes"] = df["outcome"] - df["price"]
    # 买 NO 的【占投入资金】收益率（投入 = 1 − price）
    df["ret_no_on_capital"] = df["edge_no"] / (1.0 - df["price"])
    df["ret_yes_on_capital"] = df["edge_yes"] / df["price"]

    # 市场类型归类（便于按类型筛选）
    df["is_moneyline_like"] = df["sports_market_type"].isin(
        ["moneyline", "child_moneyline", "first_half_moneyline", ""]).astype(int)

    # ---------------- 主表输出 ----------------
    cols = ["market_id", "league", "league_name", "question", "sports_market_type",
            "neg_risk", "volume", "n_points", "lead_days", "price", "outcome",
            "final_price", "drift", "league_total_volume", "league_median_mkt_vol",
            "league_market_count", "league_is_cold", "league_vol_quartile",
            "league_is_cold_medmkt", "league_medmkt_quartile",
            "mkt_vol_quartile", "mkt_vol_decile", "price_band",
            "calib_err", "abs_calib_err", "edge_no", "edge_yes",
            "ret_no_on_capital", "ret_yes_on_capital", "is_moneyline_like"]
    out = df[cols].copy()
    # 空类型写成空串而不是 NaN —— 否则读进来是 NaN，下游 isin([""]) 会静默失败
    out["sports_market_type"] = out["sports_market_type"].fillna("")
    p1 = os.path.join(OUT, "historical_rows.csv")
    out.to_csv(p1, index=False)
    print("写出 %s  (%d 行, %.1f MB)" % (p1, len(out), os.path.getsize(p1) / 1e6))

    # ---------------- 联赛汇总 ----------------
    lg = df.groupby("league_name").agg(
        n_rows=("market_id", "size"),
        n_markets=("market_id", "nunique"),
        total_volume=("volume", "sum"),
        mean_price=("price", "mean"),
        mean_outcome=("outcome", "mean"),
        mean_calib_err=("calib_err", "mean"),
        abs_calib_err=("abs_calib_err", "mean"),
    ).reset_index()
    lg["bias"] = lg["mean_outcome"] - lg["mean_price"]
    lg["se"] = np.sqrt(lg["mean_outcome"] * (1 - lg["mean_outcome"]) / lg["n_rows"])
    lg["t"] = lg["bias"] / lg["se"]
    # reliability：分档后偏差平方的样本数加权（更严格的错价指标）
    rel = []
    for lname, g in df.groupby("league_name"):
        v = 0.0
        n = len(g)
        for lo in np.arange(0, 1.0, 0.05):
            b = g[(g["price"] >= lo) & (g["price"] < lo + 0.05)]
            if len(b) >= 10:
                v += len(b) / n * (b["outcome"].mean() - b["price"].mean()) ** 2
        rel.append({"league_name": lname, "reliability": v})
    lg = lg.merge(pd.DataFrame(rel), on="league_name", how="left")
    lg["is_cold"] = (lg["total_volume"] <= lg["total_volume"].median()).astype(int)
    lg = lg.sort_values("total_volume", ascending=False)
    p2 = os.path.join(OUT, "league_summary.csv")
    lg.to_csv(p2, index=False)
    print("写出 %s  (%d 个联赛)" % (p2, len(lg)))

    # ---------------- 价格档汇总（分冷门/热门） ----------------
    pb = df.groupby(["lead_days", "price_band"]).agg(
        n=("market_id", "size"), mean_price=("price", "mean"),
        mean_outcome=("outcome", "mean")).reset_index()
    pb["bias"] = pb["mean_outcome"] - pb["mean_price"]
    pb["edge_no"] = -pb["bias"]
    pb["se"] = np.sqrt(pb["mean_outcome"] * (1 - pb["mean_outcome"]) / pb["n"])
    pb["t"] = pb["bias"] / pb["se"]
    pb["is_cold"] = 0
    pb2 = df[df["league_is_cold"] == 1].groupby(["lead_days", "price_band"]).agg(
        n=("market_id", "size"), mean_price=("price", "mean"),
        mean_outcome=("outcome", "mean")).reset_index()
    pb2["bias"] = pb2["mean_outcome"] - pb2["mean_price"]
    pb2["edge_no"] = -pb2["bias"]
    pb2["se"] = np.sqrt(pb2["mean_outcome"] * (1 - pb2["mean_outcome"]) / pb2["n"])
    pb2["t"] = pb2["bias"] / pb2["se"]
    pb2["is_cold"] = 1
    pbs = pd.concat([pb, pb2], ignore_index=True)
    p3 = os.path.join(OUT, "price_band_summary.csv")
    pbs.to_csv(p3, index=False)
    print("写出 %s  (%d 行)" % (p3, len(pbs)))

    # ---------------- 摘要 ----------------
    summ = {
        "n_rows": int(len(df)),
        "n_markets": int(df["market_id"].nunique()),
        "n_leagues": int(df["league_name"].nunique()),
        "lead_days": sorted(df["lead_days"].unique().tolist()),
        "market_types": df["sports_market_type"].value_counts().head(12).to_dict(),
        "volume_median": float(df["volume"].median()),
        "volume_mean": float(df["volume"].mean()),
        "league_volume_median": float(lg_vol.median()),
        "overall_bias_lead7": float(
            (df[df["lead_days"] == 7]["outcome"].mean()
             - df[df["lead_days"] == 7]["price"].mean())),
    }
    p4 = os.path.join(OUT, "summary.json")
    json.dump(summ, io.open(p4, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              default=str)
    print("写出 %s" % p4)
    print()
    print("摘要:")
    for k, v in summ.items():
        print("  %-22s %s" % (k, v))


import io
if __name__ == "__main__":
    main()
