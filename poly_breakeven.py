"""
poly_breakeven.py - Polymarket 情绪套利保本资金测算（最终版）
============================================================================
用户问：加上调用 AI 的成本 + 启动资金投美债的机会成本，
       至少多少启动资金才能盈利？

═══════════════════════════════════════════════════════════════════════════
模型（三段式，把三类约束分开看）
═══════════════════════════════════════════════════════════════════════════
  年利润 = C · k · n  −  C · r_f  −  F
           ↑收益        ↑机会成本    ↑固定成本
  k = 每次交易的净收益率（占投入资金）
  n = 资金每年周转次数 = 365 / 持有天数
  r_f = 美债年化
  F = 年固定成本（AI 订阅等）

  保本 ⇒  C·(k·n − r_f) > F  ⇒  **C_min = F / (k·n − r_f)**

★★ 关键结构：分母 (k·n − r_f) 里【没有 C】。
   · k·n > r_f → 有有限解，加大资金能摊薄 F     （资金【能】解决的约束）
   · k·n ≤ r_f → **任何资金量都不盈利**          （资金【不能】解决的约束）
   ⇒ 美债机会成本是"乘法型"门槛（与资金无关）；
     AI 固定成本是"加法型"门槛（与资金有关）。两者性质完全不同。

═══════════════════════════════════════════════════════════════════════════
三个必须同时成立的前提（缺一个，答案都是"不做"）
═══════════════════════════════════════════════════════════════════════════
  ① edge 真实存在（统计显著）
  ② edge 扣掉成本后仍为正
  ③ k·n > r_f  且  容量 ≥ C_min（钱投得出去）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np

OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


# ===========================================================================
# 输入（每一项标注来源）
# ===========================================================================
EDGE_7D, SE_7D, N_7D, P_7D = 0.0161, 0.0206, 220, 0.1207    # 实测 7 天前
EDGE_3D, SE_3D, N_3D, P_3D = 0.0323, 0.0195, 213, 0.1215    # 实测 3 天前
README_EDGE = (0.0220, 0.0247, 0.0294)                      # 对照 validate.py

MKTS_PER_YEAR = 56.0        # 实测：220 标的 / 3.9 年
VOL_MEDIAN = 186529.0       # 实测：该档累计成交额中位
DEPTH_FRAC = 0.05

FEE_RATE = 0.05             # sports_fees_v3 rate（记录值，本次无法复核）
SPREADS = (0.0, 0.001, 0.005, 0.010, 0.020)

RF_ANNUAL = 0.04
AI_MONTHLY = (20, 50, 100, 300)
HOLD_DAYS = 7.0


def k_net(edge, p, fee_rate, spread):
    """每次交易净收益率，占投入资金（买 NO 的价格 = 1−p）。"""
    fee = fee_rate * min(p, 1 - p)
    return (edge - fee - spread) / (1 - p)


def main():
    log("=" * 100)
    log("Polymarket 情绪套利 · 保本资金测算")
    log("=" * 100)
    log("")
    log("  标的：结算前 7 天、价格位于 0.10-0.15 的体育合约")
    log("  动作：买入 NO（以 1−p 买入，赌它结算为 0）")
    log("")

    # ------------------------------------------------------------------
    log("=" * 100)
    log("1. 前提①：edge 真实存在吗？")
    log("=" * 100)
    log("")
    log("  受控样本（2768 个在所有 6 个时点都有价格的市场，消除样本构成偏差）")
    log("")
    log("  %-8s %6s %8s %8s %9s %8s %8s  %s" %
        ("时点", "样本", "均价p", "胜率r", "edge", "SE", "t值", "95% 置信区间"))
    log("  " + "-" * 94)
    for lab, e, se, n, p in (("7 天前", EDGE_7D, SE_7D, N_7D, P_7D),
                             ("3 天前", EDGE_3D, SE_3D, N_3D, P_3D)):
        log("  %-8s %6d %8.4f %8.4f %+9.4f %8.4f %8.2f  [%+.4f, %+.4f]" %
            (lab, n, p, p - e, e, se, e / se, e - 1.96 * se, e + 1.96 * se))
    log("")
    log("  对照：项目 README 里 validate.py 的独立实现给出 7/3/1 天前")
    log("        edge = +%.4f / +%.4f / +%.4f" % README_EDGE)
    log("  => 两套实现都给出「该档被高估 1.6~3.2 个百分点」这个量级，互相印证。")
    log("")
    log("  * 但结论是【不显著】：7 天前 t = %.2f，95%% CI = [%+.4f, %+.4f]，含 0。"
        % (EDGE_7D / SE_7D, EDGE_7D - 1.96 * SE_7D, EDGE_7D + 1.96 * SE_7D))
    log("    n 只有 %d；这类「小赚多次 + 偶尔亏光本金」的结构标准差很大，" % N_7D)
    log("    1.6 个百分点的偏差完全可能只是噪声。")
    log("  => **前提① 未确认**（不是「edge 为零」，而是「还不知道」）。")

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("2. 前提②：扣掉成本后还剩多少？")
    log("=" * 100)
    log("")
    fee = FEE_RATE * min(P_7D, 1 - P_7D)
    log("  以 7 天前、p=%.4f 为例（买 NO 投入 %.4f/股）：" % (P_7D, 1 - P_7D))
    log("    平台费 = %.2f x min(%.4f, %.4f) = %.4f/股" % (FEE_RATE, P_7D, 1 - P_7D, fee))
    log("  ! 该公式本次【无法复核】：代理关闭，docs/help.polymarket.com 均不可达。")
    log("")
    log("  %-14s %9s %12s %11s %10s" % ("情形", "价差", "净边际/股", "净收益率k", "成本/edge"))
    log("  " + "-" * 94)
    for sp in SPREADS:
        log("  %-14s %9.4f %+12.4f %10.3f%% %9.0f%%" %
            ("spread=%.3f" % sp, sp, EDGE_7D - fee - sp,
             k_net(EDGE_7D, P_7D, FEE_RATE, sp) * 100,
             (fee + sp) / EDGE_7D * 100))
    log("")
    log("  => 成本吃掉 edge 的 %.0f%%~%.0f%%。" %
        (fee / EDGE_7D * 100, (fee + 0.020) / EDGE_7D * 100))
    log("     乐观成本下净收益为正，悲观成本下接近归零。**前提② 勉强成立。**")

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("3. 前提③：保本资金 C_min = F / (k*n - r_f)")
    log("=" * 100)
    log("")
    n_turn = 365.0 / HOLD_DAYS
    log("  持有期 %.0f 天 => n = 365/%.0f = **%.1f 次/年**" % (HOLD_DAYS, HOLD_DAYS, n_turn))
    log("  美债 r_f = %.1f%%（与资金量无关的门槛）" % (RF_ANNUAL * 100))
    log("")

    scenarios = [("edge点估计 + spread=%.3f" % sp,
                  k_net(EDGE_7D, P_7D, FEE_RATE, sp)) for sp in (0.0, 0.001, 0.005, 0.010)]
    k_lo = k_net(EDGE_7D - 1.96 * SE_7D, P_7D, FEE_RATE, 0.005)
    scenarios.append(("edge 95%下界 + spread=0.005", k_lo))
    k_3d = k_net(EDGE_3D, P_3D, FEE_RATE, 0.005)
    scenarios.append(("3天前edge + spread=0.005", k_3d))

    log("  %-34s %9s %9s %11s %s" % ("情形", "k", "k*n", "k*n-r_f", "能否保本"))
    log("  " + "-" * 94)
    for name, k in scenarios:
        kn, ex = k * n_turn, k * n_turn - RF_ANNUAL
        log("  %-34s %8.3f%% %8.1f%% %10.2f%% %s" %
            (name, k * 100, kn * 100, ex * 100,
             "可保本" if ex > 0 else "**任何资金都不行**"))
    log("")
    pos = [k for _, k in scenarios if k > 0]
    log("  * 两类截然不同的情形：")
    if pos:
        log("    - 全部【点估计】情形：k*n = %.0f%%~%.0f%% >> r_f=4%%" %
            (min(pos) * n_turn * 100, max(pos) * n_turn * 100))
        log("      => 只要 edge 为真，年化净收益远超美债 => **美债不是障碍**。")
    log("    - 【统计下界】情形：k = %.3f%% < 0 => k*n - r_f < 0" % (k_lo * 100))
    log("      => **任何资金量都不盈利** —— 且亏损随资金等比放大。")
    log("")
    log("  => 答案完全取决于「edge 是否真实」，而与资金量无关。")

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("4. 若 edge 为真：最小启动资金 C_min")
    log("=" * 100)
    log("")
    valid = [(nm, k) for nm, k in scenarios if k * n_turn - RF_ANNUAL > 0]
    if valid:
        log("  %-30s %12s %12s %12s %12s" % ("情形", *["AI $%d/月" % m for m in AI_MONTHLY]))
        log("  " + "-" * 94)
        for nm, k in valid:
            ex = k * n_turn - RF_ANNUAL
            cells = ["$%s" % "{:,.0f}".format((m * 12) / ex) for m in AI_MONTHLY]
            log("  %-30s %12s %12s %12s %12s" % (nm, *cells))
    log("")

    # ------------------------------------------------------------------
    log("=" * 100)
    log("5. 更硬的约束：容量（钱投得出去吗？）")
    log("=" * 100)
    log("")
    per_pos = VOL_MEDIAN * DEPTH_FRAC
    conc = MKTS_PER_YEAR * HOLD_DAYS / 365.0
    cap = per_pos * max(conc, 1.0)
    log("  实测：该档约 %.0f 个标的/年，累计成交额中位 $%s" %
        (MKTS_PER_YEAR, "{:,.0f}".format(VOL_MEDIAN)))
    log("  单仓上限（吃累计成交额 %.0f%%）= $%s" % (DEPTH_FRAC * 100, "{:,.0f}".format(per_pos)))
    log("  平均同时持仓 = %.1f 个" % conc)
    log("  => **可投入资金上限 = $%s**" % "{:,.0f}".format(cap))
    log("")
    log("  ! 三处保守（真实容量只会更小）：")
    log("     1. 累计成交额是市场生命周期总量，不是建仓时的盘口深度")
    log("     2. 假设能吃掉 5%，实际滑点更大")
    log("     3. 只算了单一价格档")
    log("")
    log("  * 一个反直觉但重要的发现（详见 output_poly_edge_capacity.txt）：")
    log("     edge 随成交额【递减】，$1M 以上甚至是负的：")
    log("       $10K-100K : +0.070 / +0.086 / +0.090  (t=2.0 / 2.5 / 2.8)")
    log("       $100K-1M  : +0.021 / +0.031 / +0.019  (t=0.8 / 1.4 / 0.8)")
    log("       $1M+      : -0.076 / -0.016 / -0.006  (t=-1.0 / -0.3 / -0.1)")
    log("     => 信号集中在【中低成交额】市场 —— 与「错价在低流动性处更大」一致，")
    log("        但也意味着**能承载的资金天然很小**。")
    log("     ! 而 $10K-100K 档的 t=2.5~2.8 是【分组搜出来的】：")
    log("       分组看了 5 档 x 3 时点 = 15 个组合，出现 t=2.8 并不意外，")
    log("       **不能当作独立证据**。")

    # ------------------------------------------------------------------
    log("")
    log("=" * 100)
    log("6. 结论")
    log("=" * 100)
    log("")
    log("  用户问「要多少启动资金」。但这个策略的瓶颈不在资金，而在下面这条链：")
    log("  任何一环断开，投多少都是亏。")
    log("")
    log("    edge 真实？      --X (t=%.2f, CI 含 0) --> 不做" % (EDGE_7D / SE_7D))
    log("         | 假设为真")
    log("         v")
    log("    扣成本后为正？    --? (乐观 +1pp，悲观 约0) --> 边缘")
    log("         | 假设为真")
    log("         v")
    log("    k*n > r_f？      --Y (点估计 ~%d%% >> 4%%) --> 美债不是障碍" %
        (int(max(pos) * n_turn * 100) if pos else 0))
    log("         | 是")
    log("         v")
    log("    容量 >= C_min？   --? (容量 ~$%s) --> 可行但规模极小" % "{:,.0f}".format(cap))
    log("")
    ex50 = k_3d * n_turn - RF_ANNUAL
    log("  => **如果 edge 为真**（3天前建仓、AI $50/月、spread 0.005 口径）：")
    log("       最小启动资金 = **$%s**" % "{:,.0f}".format(50 * 12 / ex50))
    log("       可投入上限   = **$%s**  => 刚好可行，但规模极小" % "{:,.0f}".format(cap))
    log("       注：该资金需求随 AI 月费【线性】放大 ——")
    log("           AI $100/月 => $%s ；AI $300/月 => $%s" %
        ("{:,.0f}".format(100 * 12 / ex50), "{:,.0f}".format(300 * 12 / ex50)))
    log("")
    log("  => **如果 edge 是噪声**（现有数据无法排除）：")
    log("       k*n - r_f < 0 => **不存在能盈利的启动资金**。")
    log("       此时加大资金完全无用 —— 亏损随资金等比放大。")
    log("")
    n_need = int(np.ceil((1.96 * np.sqrt(0.1 * 0.9) / 0.02) ** 2))
    log("  * 直接回答：")
    log("     - 你现在需要投的不是钱，而是【更多数据】。")
    log("       该档 n=%d；要让 2%% 的 edge 达到 t>2，需要 n = **%d**（当前 %.0f%%）。"
        % (N_7D, n_need, N_7D / n_need * 100))
    log("     - 补足样本前，$0 是唯一理性的投入。")
    log("     - 补足后若 edge 确认，所需启动资金是 **$500~$2,300 量级**")
    log("       （取决于 3天前还是7天前建仓、以及真实价差；AI $50/月口径），")
    log("       但容量上限也只有 $%s 上下 —— 这是个零花钱规模的策略，" % "{:,.0f}".format(cap))
    log("       **不是能靠它生活的策略**。")
    log("")
    log("  ! 方法论提醒：本次【费率公式】与【建仓时刻价差】都无法用本地数据复核")
    log("     （市场元数据的 bid/ask 是结算后快照，已失去意义；官方 docs 不可达）。")
    log("     所以成本是【参数化】的，不是实测的 —— 请看第 2 节的扫描而非单一数字。")

    with open("output_poly_breakeven.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_breakeven.txt")


if __name__ == "__main__":
    main()
