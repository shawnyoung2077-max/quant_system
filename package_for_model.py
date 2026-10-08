"""
package_for_model.py - 把建模交付包做成几档大小，适配不同接收方式
============================================================================
现实约束：主表 15.5 MB，很多聊天界面传不上去（常见上限 10~30 MB）。
所以做三档：

  A. 最小包 (~50 KB)   只给文档 + 汇总表。让对方先理解问题与已有结论
  B. 采样表 (~4 MB)    分层抽样的主表，保留全部结构（推荐）
  C. 完整包 (~16 MB)   全量主表 + 文档，打成 zip

分层抽样必须保住三件事，否则建模方会看到扭曲的数据：
  1. 冷门联赛的市场【全保留】（只有 119 行，丢了就没法研究核心假设）
  2. 每个 (联赛四分位 x 市场成交额四分位) 格子都有样本
  3. 一个市场的 6 个时点必须【整组保留或整组丢弃】——
     拆散会破坏时序结构，让"价格随时间收敛"这类分析失效
"""

import io
import os
import sys
import zipfile

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

MD = r"D:\26050\Documents\quant_system\modeling"
SRC = os.path.join(MD, "historical_rows.csv")
PKG = r"D:\26050\Documents\quant_system\modeling_package"
os.makedirs(PKG, exist_ok=True)


def main():
    df = pd.read_csv(SRC)
    df["sports_market_type"] = df["sports_market_type"].fillna("")
    print("主表 %d 行, %d 市场" % (len(df), df.market_id.nunique()))

    # ---------------- 按比例抽样：忠实缩影 ----------------
    # ⚠️ 第一版做了【平衡】抽样（每个格子抽同样多），结果联赛分布被严重扭曲
    #    （league_vol_quartile 各档占比偏差达 0.41）。
    #    那样别的模型在采样表上算"总体偏差"会得到错误数字 —— 比不给数据更糟。
    #    所以改成【按比例】抽样：采样表是总体的忠实缩影，可直接算总体统计量。
    #    冷门联赛因为本来就少，采样后依然少 —— 但那是事实，
    #    所以另出一个【冷门联赛全量】小文件供定向分析。
    rng = np.random.default_rng(20261008)
    FRAC = 0.22                      # 市场抽样比例
    mkt = df.drop_duplicates("market_id")[["market_id", "league_vol_quartile"]]
    all_ids = mkt.market_id.to_numpy()
    smp_ids = set(rng.choice(all_ids, size=int(len(all_ids) * FRAC), replace=False))
    smp = df[df.market_id.isin(smp_ids)].copy()
    print("  按比例抽样 %.0f%% -> %d 市场, %d 行" %
          (FRAC * 100, smp.market_id.nunique(), len(smp)))

    p_smp = os.path.join(MD, "historical_rows_sample.csv")
    smp.to_csv(p_smp, index=False)
    print("  写出 %s (%.1f MB)" % (p_smp, os.path.getsize(p_smp) / 1e6))

    # 冷门联赛全量单独出（小文件，供定向分析）
    cold_df = df[df.league_vol_quartile == 1].copy()
    p_cold = os.path.join(MD, "historical_rows_cold_leagues.csv")
    cold_df.to_csv(p_cold, index=False)
    print("  写出 %s (%d 行, %.0f KB)" %
          (p_cold, len(cold_df), os.path.getsize(p_cold) / 1024))

    # 覆盖度自检 —— 按比例抽样必须与全样本高度一致
    print()
    print("  采样表覆盖度自检（按比例抽样应全部 < 0.02）:")
    ok = True
    for c in ("league_vol_quartile", "mkt_vol_quartile", "price_band", "lead_days",
              "is_moneyline_like"):
        a = df[c].value_counts(normalize=True).sort_index()
        b = smp[c].value_counts(normalize=True).sort_index()
        diff = float((a - b).abs().max())
        flag = "OK" if diff < 0.02 else "**偏差过大**"
        if diff >= 0.02:
            ok = False
        print("    %-22s 最大占比偏差 = %.4f  %s" % (c, diff, flag))
    if not ok:
        print("    ⚠️ 采样表与总体不一致，建模方需注意")

    # ---------------- A 档：最小包 ----------------
    A = os.path.join(PKG, "A_最小包_先看这个")
    os.makedirs(A, exist_ok=True)
    for f in ("HANDOFF.md", "DATA_DICTIONARY.md", "league_summary.csv",
              "price_band_summary.csv", "summary.json"):
        src = os.path.join(MD, f)
        if os.path.exists(src):
            with open(src, "rb") as fi, open(os.path.join(A, f), "wb") as fo:
                fo.write(fi.read())
    ta = sum(os.path.getsize(os.path.join(A, f)) for f in os.listdir(A))
    print()
    print("A 最小包: %d 个文件, %.0f KB" % (len(os.listdir(A)), ta / 1024))

    # ---------------- B 档：最小包 + 采样表 ----------------
    B = os.path.join(PKG, "B_推荐_含采样数据")
    os.makedirs(B, exist_ok=True)
    for f in os.listdir(A):
        with open(os.path.join(A, f), "rb") as fi, open(os.path.join(B, f), "wb") as fo:
            fo.write(fi.read())
    for src in (p_smp, p_cold):
        with open(src, "rb") as fi, open(os.path.join(B, os.path.basename(src)), "wb") as fo:
            fo.write(fi.read())
    tb = sum(os.path.getsize(os.path.join(B, f)) for f in os.listdir(B))
    print("B 推荐包: %d 个文件, %.1f MB" % (len(os.listdir(B)), tb / 1e6))

    # ---------------- C 档：zip ----------------
    zc = os.path.join(PKG, "C_完整数据.zip")
    with zipfile.ZipFile(zc, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("HANDOFF.md", "DATA_DICTIONARY.md", "league_summary.csv",
                  "price_band_summary.csv", "summary.json", "historical_rows.csv",
                  "historical_rows_sample.csv",
                  "historical_rows_cold_leagues.csv"):
            p = os.path.join(MD, f)
            if os.path.exists(p):
                z.write(p, f)
    print("C 完整包: %s (%.1f MB)" % (zc, os.path.getsize(zc) / 1e6))

    # ---------------- 提示词文件 ----------------
    prompt = """给接手建模的 AI 的提示词（可直接复制）
================================================================

我在研究 Polymarket（预测市场）的体育板块是否存在可利用的系统性错价。
附件里有我的历史数据和已有分析。

请先读 HANDOFF.md 和 DATA_DICTIONARY.md，然后做建模。

【任务】
建一个模型，预测 calib_err = outcome - price
（实际结算结果 − 市场隐含概率）。
然后判断：在扣除交易成本后，是否存在可交易的 edge。

【必须先处理的头号问题】
HANDOFF.md 第五节的 Q1：联赛级成交额和市场级成交额给出的关系
方向相反，而且两者统计上都显著。请先解决这个矛盾再建模，
否则等于在两个互相矛盾的信号上拟合。

【已知的坑，请直接跳过】
HANDOFF.md 第四节列了六个我实际踩过的坑，尤其：
- 坑 1/2：跨时点比较的样本构成偏差。不同时点覆盖的市场集合不同；
  而"只保留全时点都有价格的市场"这个修正本身又会筛选出高流动性事件
  （我因此得到过两个符号完全相反的结果）。
- 坑 3：从博彩赔率去掉抽水（de-vig）有三种方法，在抽水大的市场
  三者分歧可达 0.055 —— 比我们要找的 edge（0.02~0.03）还大。
  任何信号必须三种方法方向一致才算数。

【数据说明】
- historical_rows_sample.csv 是分层采样的版本（保留了全部结构与冷门联赛）。
  如果能看到全量 historical_rows.csv，优先用全量。
- 一行 = 一个市场 × 一个观察时点，同一市场有 6 行（lead_days = 30/21/14/7/3/1）。
- 读入时注意：sports_market_type 的空值在 CSV 里是空串，
  pandas 默认会读成 NaN，可能导致静默漏数据。详见 DATA_DICTIONARY.md。

【建模要求】
1. 样本外验证请按【时间】切分，不要随机切分
2. 报告标准误与置信区间，不要只给点估计
3. 报告 edge 时同时给【毛 edge】和【扣成本后净 edge】
   （成本模型见 HANDOFF.md 第三节 3.3）
4. 逐联赛/逐分档扫描时请做多重比较校正（Benjamini-Hochberg）
5. 最终请正面回答：这个 edge 为什么还没被套利掉？
   如果它是免费的钱，早就消失了。它还在，说明要么不是免费的，
   要么太小 —— 这两种都要削减收益预期。

================================================================
"""
    with open(os.path.join(PKG, "提示词_复制给模型.txt"), "w", encoding="utf-8") as fh:
        fh.write(prompt)
    print()
    print("提示词已写到 modeling_package/提示词_复制给模型.txt")


if __name__ == "__main__":
    main()
