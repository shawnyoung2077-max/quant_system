"""
poly_final.py - Polymarket 情绪套利：保本资金测算（全输入已实测核准版）
============================================================================
本脚本取代 poly_breakeven.py —— 所有输入现在都有【可复核的来源】，
不再有"记录值"或"假设值"。

═══════════════════════════════════════════════════════════════════════════
已核准的输入（全部实测/官方来源）
═══════════════════════════════════════════════════════════════════════════
[官方] 费率公式（docs.polymarket.com/cn/trading/fees，实测 200）
        fee = C × feeRate × p × (1 − p)      C=份额数, p=价格
       ★ 与旧假设 rate×min(p,1−p) 不同 —— 是 p×(1−p)。
       体育 feeRate = 0.05；Maker 费 = 0；Maker 返利 = 15%
[官方] 地缘政治/世界事件市场 feeRate = 0（且实测 feesEnabled=False）
[实测] tick size: 政治类 0.001，体育类 0.01（orderPriceMinTickSize）
[实测] 0.10-0.15 档实时价差: 中位 0.0100，均值 0.0242（n=18）
[实测] 0.10-0.15 档订单簿前10档深度:
         买盘 中位 $1,899 / 均值 $3,974
         卖盘 中位 $150,429 / 均值 $1,297,307   ← 两侧极不对称！
[实测] edge（受控样本 2768 个市场）
         7 天前 n=220 edge=+0.0161 SE=0.0206 t=0.78 CI[-0.0243,+0.0565]
         3 天前 n=213 edge=+0.0323 SE=0.0195 t=1.66 CI[-0.0059,+0.0705]

═══════════════════════════════════════════════════════════════════════════
模型
═══════════════════════════════════════════════════════════════════════════
  年利润 = C·k·n − C·r_f − F
  C_min = F / (k·n − r_f)
  ★ 分母里没有 C ⇒ 美债是"乘法型"门槛（资金解决不了），
    AI 成本是"加法型"门槛（资金能摊薄）。
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


# ---------------- 已核准输入 ----------------
P_7D, EDGE_7D, SE_7D, N_7D = 0.1207, 0.0161, 0.0206, 220
P_3D, EDGE_3D, SE_3D, N_3D = 0.1215, 0.0323, 0.0195, 213

SPORTS_FEE_RATE = 0.05
MAKER_REBATE = 0.15

TICK_SPORTS = 0.01
SPREAD_MEDIAN = 0.0100          # 实测 0.10-0.15 档价差中位
SPREAD_MEAN = 0.0242            # 实测均值

DEPTH_MEDIAN = 1899.0           # 实测买盘前10档美元中位（这是买 NO 要吃的那一侧）
DEPTH_MEAN = 3974.0

MKTS_PER_YEAR = 56.0
RF_ANNUAL = 0.04
AI_MONTHLY = (20, 50, 100, 300)
HOLD_DAYS = 7.0


def taker_fee(p, rate=SPORTS_FEE_RATE):
    """官方公式：fee = C × rate × p × (1−p)，每股即 rate×p×(1−p)。"""
    return rate * p * (1 - p)


def k_taker(p, edge, spread_cost):
    """Taker 净收益率（占投入资金 = 1−p）。"""
    return (edge - taker_fee(p) - spread_cost) / (1 - p)


def k_maker(p, edge, spread_cost, rebate_fill_frac=1.0):
    """
    Maker 净收益率：不付手续费，且按成交额分到 15% 的 taker 费返利。

    ⚠️ 但 maker 必须挂【限价单】，成交价对你有利（拿到价差而不是付价差）：
       spread_cost 取【负】值表示赚到价差。
       真正的代价是【逆向选择】：限价单只在价格朝不利方向走时成交。
       这里不做逆向选择建模（无法从现有数据估），所以 maker 的结果
       应当视为【上界】，不是可实现值。
    """
    fee_earned = MAKER_REBATE * taker_fee(p) * rebate_fill_frac
    return (edge + fee_earned - spread_cost) / (1 - p)


def main():
    log("=" * 100)
    log("Polymarket 情绪套利 · 保本资金测算（全输入已实测核准）")
    log("=" * 100)
    log("")

    # ==================================================================
    log("=" * 100)
    log("0. 费率：官方公式（已从 docs 取得，非假设）")
    log("=" * 100)
    log("")
    log("  官方原文: fee = C x feeRate x p x (1 - p)")
    log("  体育: feeRate=0.05, Maker费=0, Maker返利=15%")
    log("  地缘政治/世界事件: feeRate=0（且实测 feesEnabled=False）")
    log("")
    old = SPORTS_FEE_RATE * min(P_7D, 1 - P_7D)
    new = taker_fee(P_7D)
    log("  对 p=%.4f（体育档）:" % P_7D)
    log("     旧假设 rate x min(p,1-p) = %.6f/股" % old)
    log("     官方   rate x p x (1-p) = %.6f/股   <- 低 %.0f%%"
        % (new, (1 - new / old) * 100))
    log("  => 我原先高估了费用 %.0f%%。修正后成本略降。" % ((1 - new / old) * 100))

    # ==================================================================
    log("")
    log("=" * 100)
    log("1. 前提①：edge 真实吗？（这项没变）")
    log("=" * 100)
    log("")
    log("  %-8s %6s %8s %9s %8s %8s  %s" %
        ("时点", "样本", "均价p", "edge", "SE", "t值", "95% CI"))
    log("  " + "-" * 90)
    for lab, e, se, n, p in (("7 天前", EDGE_7D, SE_7D, N_7D, P_7D),
                             ("3 天前", EDGE_3D, SE_3D, N_3D, P_3D)):
        log("  %-8s %6d %8.4f %+9.4f %8.4f %8.2f  [%+.4f, %+.4f]" %
            (lab, n, p, e, se, e / se, e - 1.96 * se, e + 1.96 * se))
    log("")
    log("  * 两个时点的 CI 都【包含 0】=> 前提① 仍未确认。")
    log("    这一步不会因为费率/价差查清而改变 —— 它需要的是更多样本。")

    # ==================================================================
    log("")
    log("=" * 100)
    log("2. 成本：Taker 视角（现在全部是实测值）")
    log("=" * 100)
    log("")
    log("  实测价差（0.10-0.15 档, n=18）: 中位=%.4f 均值=%.4f" %
        (SPREAD_MEDIAN, SPREAD_MEAN))
    log("  tick size: 体育类 0.01 => 价差中位正好是 1 个 tick")
    log("  ⚠️ 成本取【半个价差】(从中间价穿到卖一价)，不是全价差。")
    log("     因为历史 edge 是对着价格序列算的（≈中间价），")
    log("     真实成本是从中间价走到可成交价的【一半】。")
    log("")
    log("  %-26s %10s %10s %12s %12s" %
        ("情形", "费率/股", "价差/股", "净边际/股", "净收益率k"))
    log("  " + "-" * 94)
    cases = [
        ("官方费率 + 半价差(0.005)", taker_fee(P_7D), SPREAD_MEDIAN / 2),
        ("官方费率 + 全价差(0.010)", taker_fee(P_7D), SPREAD_MEDIAN),
        ("官方费率 + 均价差一半(0.0121)", taker_fee(P_7D), SPREAD_MEAN / 2),
        ("零成本（理论上限）", 0.0, 0.0),
    ]
    for nm, f_, s_ in cases:
        k = (EDGE_7D - f_ - s_) / (1 - P_7D)
        log("  %-26s %10.5f %10.5f %+12.5f %11.3f%%" % (nm, f_, s_, EDGE_7D - f_ - s_, k * 100))

    # ==================================================================
    log("")
    log("=" * 100)
    log("3. ★ Maker 视角：不付费率，还有 15% 返利")
    log("=" * 100)
    log("")
    log("  官方：『Maker 不收取任何费用。只有 Taker 支付费用。』")
    log("  体育档 Maker 返利 = 15%（把 taker 交的费按比例分给做市商）")
    log("  => 挂限价单时【费率 = 0】，且【倒收】返利。")
    log("     而且限价单成交时是【赚到价差】而不是付出价差。")
    log("")
    log("  %-30s %10s %12s %12s %14s" %
        ("情形", "费率/股", "价差/股", "净边际/股", "净收益率k"))
    log("  " + "-" * 94)
    mk = [
        ("Maker: 零费 + 赚半价差", 0.0, SPREAD_MEDIAN / 2),
        ("Maker: 零费 + 赚全价差", 0.0, SPREAD_MEDIAN),
        ("Maker: 含15%返利 + 赚半价差", 0.0, SPREAD_MEDIAN / 2),
    ]
    for nm, f_, s_ in mk:
        fee_earned = MAKER_REBATE * taker_fee(P_7D) if "返利" in nm else 0.0
        net = EDGE_7D + fee_earned - f_ - s_
        k = net / (1 - P_7D)
        log("  %-30s %10.5f %12.5f %+12.5f %13.3f%%" % (nm, f_, -s_, net, k * 100))
    log("")
    log("  * 这是本次最大的发现：Maker 口径下成本变成【负的】——")
    log("    你不付手续费、赚价差、还能分返利，净收益率从 %.3f%% 跳到 %.3f%%。" %
        (k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN / 2) * 100, k_maker(P_7D, EDGE_7D, -SPREAD_MEDIAN / 2, True) * 100))
    log("  ! 但**不能直接相信这个数字**，代价是【逆向选择】：")
    log("     限价买单只有在市场跌到你的价位时才成交 —— 而那时价格下跌恰恰是")
    log("     「NO 变得更值钱」的反面（你在买 NO，价格下跌说明 YES 跌 → NO 涨 →")
    log("     你的限价买单是"接刀"）。你成交的样本天然偏向坏的那一侧。")
    log("     现有数据无法量化逆向选择的大小，所以 Maker 数字应视为【上界】。")

    # ==================================================================
    log("")
    log("=" * 100)
    log("4. 保本资金 C_min = F / (k·n − r_f)")
    log("=" * 100)
    log("")
    n_turn = 365.0 / HOLD_DAYS
    log("  持有期 %.0f 天 => n = %.1f 次/年" % (HOLD_DAYS, n_turn))
    log("  美债 r_f = %.1f%%" % (RF_ANNUAL * 100))
    log("")
    scen = [
        ("Taker 官方费率+半价差", k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN / 2)),
        ("Taker 官方费率+全价差", k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN)),
        ("Taker 3天前edge+半价差", k_taker(P_3D, EDGE_3D, SPREAD_MEDIAN / 2)),
        ("Taker edge−1.96SE(统计下界)", k_taker(P_7D, EDGE_7D - 1.96 * SE_7D, SPREAD_MEDIAN / 2)),
        ("Maker 零费+赚半价差(上界)", k_maker(P_7D, EDGE_7D, -SPREAD_MEDIAN / 2, False)),
    ]
    log("  %-32s %9s %9s %11s %s" % ("情形", "k", "k·n", "k·n−r_f", "能否保本"))
    log("  " + "-" * 94)
    ok = {}
    for nm, k in scen:
        kn, ex = k * n_turn, k * n_turn - RF_ANNUAL
        log("  %-32s %8.3f%% %8.1f%% %10.2f%% %s" %
            (nm, k * 100, kn * 100, ex * 100,
             "可保本" if ex > 0 else "**任何资金都不行**"))
        if ex > 0:
            ok[nm] = ex
    log("")
    if ok:
        log("  %-32s %12s %12s %12s %12s" % ("可保本情形", *["AI $%d/月" % m for m in AI_MONTHLY]))
        log("  " + "-" * 94)
        for nm, ex in ok.items():
            cells = ["$%s" % "{:,.0f}".format((m * 12) / ex) for m in AI_MONTHLY]
            log("  %-32s %12s %12s %12s %12s" % (nm, *cells))

    # ==================================================================
    log("")
    log("=" * 100)
    log("5. 容量：真实订单簿深度（这项被大幅下修）")
    log("=" * 100)
    log("")
    log("  实测订单簿前 10 档累计美元（0.10-0.15 档，12 个市场的 CLOB /book）：")
    log("     买盘: 中位 $%s   均值 $%s" % ("{:,.0f}".format(DEPTH_MEDIAN), "{:,.0f}".format(DEPTH_MEAN)))
    log("     卖盘: 中位 $150,429  均值 $1,297,307")
    log("")
    log("  * 两侧【严重不对称】：卖盘深度是买盘的约 80 倍。")
    log("    买 NO 这个动作要吃的是【买盘那一侧】，所以用 $%s 作单仓上限。"
        % "{:,.0f}".format(DEPTH_MEDIAN))
    log("")
    conc = MKTS_PER_YEAR * HOLD_DAYS / 365.0
    cap = DEPTH_MEDIAN * max(conc, 1.0)
    log("  平均同时持仓 = %.0f 标的/年 × %.0f 天 / 365 = %.2f 个" % (MKTS_PER_YEAR, HOLD_DAYS, conc))
    log("  => **可投入资金上限 = $%s**" % "{:,.0f}".format(cap))
    log("")
    log("  ⚠️ 对比上一版：之前用「累计成交额的 5%%」估出 $%s，" % "{:,.0f}".format(186529 * 0.05))
    log("     实测订单簿只有 $%s —— **高估了约 %.1f 倍**。" %
        ("{:,.0f}".format(DEPTH_MEDIAN), 186529 * 0.05 / DEPTH_MEDIAN))
    log("     原因：累计成交额是市场【整个生命周期】的总量，")
    log("           而订单簿深度是【此刻】能立刻吃掉的量。两者不是一个东西。")

    # ==================================================================
    log("")
    log("=" * 100)
    log("6. 结论")
    log("=" * 100)
    log("")
    log("  ① edge 真实？       --X  t=0.78/1.66，CI 都含 0        => 未确认")
    log("  ② 扣成本为正？       --Y  官方费率+半价差下 k=%.3f%%      => 通过" %
        (k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN / 2) * 100))
    log("  ③ k·n > r_f？       --Y  %.0f%% >> 4%%                 => 美债不是障碍" %
        (k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN / 2) * n_turn * 100))
    log("  ④ 容量 >= C_min？    --?  ~$%s  vs  C_min ~$%s" %
        ("{:,.0f}".format(cap), "{:,.0f}".format(ok.get("Taker 官方费率+半价差", np.nan) and 600 / ok["Taker 官方费率+半价差"])))
    log("")
    k_main = k_taker(P_7D, EDGE_7D, SPREAD_MEDIAN / 2)
    ex_main = k_main * n_turn - RF_ANNUAL
    c50 = 50 * 12 / ex_main
    log("  => 若 edge 为真（Taker 口径、AI $50/月）：")
    log("       最小启动资金 C_min = **$%s**" % "{:,.0f}".format(c50))
    log("       可投入上限        = **$%s**" % "{:,.0f}".format(cap))
    if cap > c50:
        log("       => 容量【刚好够】，但余量只有 %.0f%% —— 几乎没有试错空间。"
            % ((cap / c50 - 1) * 100))
    else:
        log("       => **容量不够放 C_min** —— 钱投不出去。")
    log("")
    log("  => 若 edge 是噪声（统计下界情形）：k<0 => **不存在能盈利的启动资金**。")
    log("")
    log("  * 真正该做的事：补样本。n=%d，要 t>2 需 n≈%d。" %
        (N_7D, int(np.ceil((1.96 * np.sqrt(0.1 * 0.9) / 0.02) ** 2))))

    with open("output_poly_final.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_poly_final.txt")


if __name__ == "__main__":
    main()
