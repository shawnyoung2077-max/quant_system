"""
calibrate_floor.py - 给加密市场标定 Fitness 的换手率地板
============================================================================
问题：BRAIN 的 Fitness 公式
        Fitness = Sharpe × sqrt(|年化收益| / max(floor, 日换手))
      里的 floor = 0.125 是【股票市场（USA/delay1/TOP3000）】的经验值。
      直接套到加密上是否合理？

⚠️ 先搞清楚地板的方向（最容易搞反的地方，本脚本第一版就搞反了）：
     分母是 max(floor, TO)。
       · TO > floor → 分母 = TO，**地板完全不起作用**
       · TO < floor → 分母 = floor > TO，Fitness 比 floor=0 时【更低】
     ⇒ **地板只影响【低换手】策略，且是"惩罚"它们。**
       高换手策略与地板无关。
     所以「加密地板更高」的含义是：**加密对低换手（长持仓）策略更不宽容。**

     经济上说得通：加密的单币波动与信用风险都远高于股票，
     长持仓意味着承担了更多风险却没有相应换手，所以不该给"低换手红利"。

推导（见 alpha_score.turnover_floor）：
     地板的经济含义 = 「换手成本相对于日均波动可以忽略」的水平
     所以标尺是【成本 / 日均波动】，而不是成本本身。
     floor_crypto = floor_eq × (cost_eq/σ_eq) / (cost_crypto/σ_crypto)

用法：
    python calibrate_floor.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from alpha_score import (turnover_floor, brain_grade, brain_submittable)

PPY_CRYPTO = 365
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


def F(sharpe, ann_ret, turnover, floor):
    """直接按公式算 Fitness —— 避免构造合成收益序列带来的伪影。"""
    return sharpe * np.sqrt(abs(ann_ret)) / max(floor, turnover)


def main():
    from crypto_data import build_panel

    spot = build_panel("1d", "close")
    log("[data] 现货面板 %d天 × %d币  %s ~ %s" %
        (len(spot), spot.shape[1], spot.index.min().date(), spot.index.max().date()))

    # ---- 实测加密波动 ----
    r = spot.pct_change()
    per_coin_vol = r.std()
    port_vol_all = float(r.mean(axis=1).std())            # 30币等权组合
    log("")
    log("=" * 100)
    log("1. 实测日波动（加密）")
    log("=" * 100)
    log("  单币日波动 : 中位数=%.4f  均值=%.4f  min=%.4f  max=%.4f" %
        (per_coin_vol.median(), per_coin_vol.mean(),
         per_coin_vol.min(), per_coin_vol.max()))
    log("  30币等权组合日波动 = %.4f  (年化 %.1f%%)" %
        (port_vol_all, port_vol_all * np.sqrt(PPY_CRYPTO) * 100))
    log("")
    log("  ⚠️ 用哪个波动？floor 衡量的是「换手成本相对你承担的风险有多贵」。")
    log("     换手成本按【成交额】计，风险按【组合波动】担 ⇒ 应该用【组合】波动。")
    log("     这里用 30 币等权组合 = %.4f（代表「加密市场的典型风险水平」）。" % port_vol_all)
    log("     （单个策略的波动会小得多 —— 例如 carry 只有 2%/年 —— 但 floor 是")
    log("      市场层面的归一化常数，不该按每个策略重新标定，否则就失去可比性。）")

    COST_CRYPTO = 14.0        # bps 往返（现货 8 + 永续 6，carry_v3 设定）
    log("")
    log("  加密往返成本 = %.0f bps（现货 8 + 永续 6）" % COST_CRYPTO)
    log("  成本/日波动 = %.1f bps/单位波动" % (COST_CRYPTO / port_vol_all))

    # ---- 标定 ----
    log("")
    log("=" * 100)
    log("2. 标定：floor_crypto / floor_eq = (cost_eq/σ_eq) / (cost_crypto/σ_crypto)")
    log("=" * 100)
    EQ_COST, EQ_VOL = 10.0, 0.012
    log("  股票侧参照（【假设】，逐项列出以便替换）：")
    log("     往返成本 = %.0f bps，日波动 = %.3f（≈年化 19%%/√252）" % (EQ_COST, EQ_VOL))
    log("     → 成本/日波动 = %.1f" % (EQ_COST / EQ_VOL))
    log("")
    f_main = turnover_floor(COST_CRYPTO, port_vol_all)
    log("  ==> 加密地板 = 0.125 × (%.1f / %.1f) = **%.4f**" %
        (EQ_COST / EQ_VOL, COST_CRYPTO / port_vol_all, f_main))
    log("")
    log("  方向检验（容易搞反，所以显式写出来）：")
    log("     加密成本绝对值【更高】（14 vs 10 bps），但日波动约为股票的 %.1f 倍" %
        (port_vol_all / EQ_VOL))
    log("     ⇒ 成本/波动 更【小】(%.1f < %.1f) ⇒ 换手相对更便宜" %
        (COST_CRYPTO / port_vol_all, EQ_COST / EQ_VOL))
    log("     ⇒ 地板应该【更高】= 对【低换手】策略更不宽容：%.4f > 0.125" % f_main)
    log("     这与「加密手续费贵所以更该罚换手」的直觉相反 ——")
    log("     后者混淆了【绝对成本】与【波动调整后的成本】。")

    # ---- 敏感性 ----
    log("")
    log("=" * 100)
    log("3. 敏感性：股票侧假设变了，结论稳不稳？")
    log("=" * 100)
    log("%-14s %-14s %-12s %-14s" % ("股票成本(bps)", "股票日波动", "加密地板", "相对0.125"))
    log("-" * 100)
    vals = []
    for c_eq in (5.0, 10.0, 20.0):
        for v_eq in (0.008, 0.012, 0.020):
            f = turnover_floor(COST_CRYPTO, port_vol_all,
                               ref_cost_bps=c_eq, ref_daily_vol=v_eq)
            vals.append(f)
            log("%-14.0f %-14.3f %-12.4f %-14.2fx" %
                (c_eq, v_eq, f, f / 0.125))
    above = sum(1 for v in vals if v > 0.125)
    log("")
    log("  ★ 诚实的结论：地板落在 %.2f ~ %.2f，其中 %d/%d 个组合【高于】0.125。" %
        (min(vals), max(vals), above, len(vals)))
    if above == len(vals):
        log("     ⇒ 方向对全部假设都稳健：加密地板高于股票。")
    else:
        log("     ⇒ ⚠️ 方向【不是】无条件稳健：%d/%d 个组合低于 0.125（股票成本低且日波动高时，"
            % (len(vals) - above, len(vals)))
        log("        推导会给出比 0.125 更小的地板）。")
        log("        所以这个推导只能给出【数量级】，不能当作确定的常数。")
    log("")
    log("  推荐：默认取中性假设下的 **%.2f**（股票 10bps / 日波动 1.2%%）。" % f_main)
    log("        若只需要方向，可以说「加密地板在 0.1~1.0 量级，通常高于 0.125」。")

    # ---- 地板的作用方向（核心澄清）----
    log("")
    log("=" * 100)
    log("4. 地板的【作用方向】：它只影响低换手策略")
    log("=" * 100)
    log("  分母 = max(floor, TO)：")
    log("     · TO > floor → 分母 = TO，地板【完全不起作用】")
    log("     · TO < floor → 分母 = floor > TO → Fitness 比 floor=0 时【更低】")
    log("  ⇒ 地板是给低换手策略设的【惩罚下限】，不是给高换手设的宽容。")
    log("")
    SH, RET = 5.91, 0.128      # 本项目 carry 实测
    log("  用 carry 实测指标（Sharpe=%.2f, 年化=%.1f%%）扫描换手：" % (SH, RET * 100))
    log("")
    log("%-10s %-12s %-12s %-12s %-14s" %
        ("日换手", "floor=0.125", "floor=%.2f" % f_main, "floor=0.50", "地板起作用?"))
    log("-" * 100)
    for to in (0.03, 0.055, 0.125, 0.20, 0.33, 0.50, 1.00, 2.00):
        a = F(SH, RET, to, 0.125)
        b = F(SH, RET, to, f_main)
        c = F(SH, RET, to, 0.50)
        eff = "是" if to < max(0.125, f_main, 0.50) else "否"
        log("%-10.3f %-12.2f %-12.2f %-12.2f %-14s" % (to, a, b, c, eff))
    log("")
    log("  读法：")
    log("     · 换手 0.03（< 0.125）时三个地板的 Fitness 各不相同 ⇒ 地板起作用")
    log("     · 换手 1.00 / 2.00（> 所有地板）时三者完全相同 ⇒ 地板不起作用")
    log("     · carry 换手 0.055 属【低换手】，地板影响它：floor=0.125 → %.1f，" %
        F(SH, RET, 0.055, 0.125))
    log("       floor=%.2f → %.1f（差 %.1f 倍）" %
        (f_main, F(SH, RET, 0.055, f_main),
         F(SH, RET, 0.055, 0.125) / F(SH, RET, 0.055, f_main)))
    log("")
    log("  ⚠️ 但 carry 的 Sharpe 高达 %.2f，无论用哪个地板 grade 都是 EXCELLENT。" % SH)
    log("     地板在【边缘策略】上才有决定性 —— 见下一节。")

    # ---- 边缘策略：地板决定 grade ----
    log("")
    log("=" * 100)
    log("5. 地板真正决定 grade 的地方：边缘策略")
    log("=" * 100)
    MS, MR, MT = 1.6, 0.08, 0.05
    log("  取一个「边缘」策略：Sharpe=%.1f, 年化=%.0f%%, 日换手=%.2f" %
        (MS, MR * 100, MT))
    log("")
    log("%-12s %-12s %-14s %-14s" % ("地板", "Fitness", "BRAIN grade", "可提交(GOOD+)"))
    log("-" * 100)
    for fl in (0.125, 0.20, f_main, 0.40, 0.50):
        fv = F(MS, MR, MT, fl)
        log("%-12.3f %-12.3f %-14s %-14s" %
            (fl, fv, brain_grade(fv), str(brain_submittable(fv, MS))))
    log("")
    log("  ⇒ 同一个策略，地板从 0.125 提到 %.2f：Fitness 从 %.3f 降到 %.3f，" %
        (f_main, F(MS, MR, MT, 0.125), F(MS, MR, MT, f_main)))
    log("    grade 从 %s 变 %s。" %
        (brain_grade(F(MS, MR, MT, 0.125)), brain_grade(F(MS, MR, MT, f_main))))
    log("")
    log("  ★ 这才是标定地板的真正意义：")
    log("     对高 Sharpe 策略，地板怎么取都是 EXCELLENT，无所谓；")
    log("     对【低换手 + 中等 Sharpe】的边缘策略，地板直接决定它能不能提交。")
    log("     而加密策略的换手分布与股票不同，用错地板会让【整个筛选队列】发生偏移：")
    log("        · 地板取太小（0.125）→ 低换手策略被高估 → 提交一批不够格的 alpha")
    log("        · 地板取太大（1.0）→ 低换手策略被过度惩罚 → 漏掉真正的好 alpha")

    # ---- 结论 ----
    log("")
    log("=" * 100)
    log("结论")
    log("=" * 100)
    log("  1. 地板只惩罚【低换手】策略；高换手策略与地板无关（最容易搞反的一点）。")
    log("  2. 按「成本/日均波动」推导，中性假设下加密地板 ≈ **%.2f**（BRAIN 股票是 0.125）。"
        % f_main)
    log("  3. 方向：加密波动是股票的 %.1f 倍，而成本只高 1.4 倍" % (port_vol_all / EQ_VOL))
    log("     ⇒ 换手相对更便宜 ⇒ 地板更高 ⇒ 对长持仓更不宽容。")
    log("     经济上合理：加密的单币波动与信用风险远高于股票，长持仓不值得奖励。")
    log("  4. ⚠️ 推导的输入含【假设】（股票成本/波动），敏感性区间 %.2f~%.2f，" %
        (min(vals), max(vals)))
    log("     并非对全部输入都给出 >0.125。所以它给出的是【数量级与方向】，不是常数。")
    log("  5. 落地：`score_factor(..., turnover_floor=%.2f)`；" % f_main)
    log("     在 BRAIN 上调 alpha（股票口径）时仍用默认的 0.125。")

    with open("output_calibrate_floor.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_calibrate_floor.txt")


if __name__ == "__main__":
    main()
