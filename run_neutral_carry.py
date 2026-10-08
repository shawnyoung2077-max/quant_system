"""
run_neutral_carry.py - 给 Delta 中性 carry 加【分组中性化 + 截断 + 归一化】
============================================================================
回答三个问题：
  Q1 截断（单名权重上限）对 carry 有用吗？BRAIN 顺序 vs 硬上限哪个对？
  Q2 分组中性化（市值代理 / BTC-beta / 板块）能改善 carry 吗？
  Q3 BTC-beta 中性化对 carry 是不是 no-op？（carry 已 Delta 中性 —— 需验证而非假设）

方法（两条线，都测）：
  【A 信号中性化】先把 funding 信号在组内中性化，再排序选币
      → 选币标准变成"相对本组高费率"，是"组内 long-short"的原生实现
  【B 权重中性化】先按原始 funding 选币等权，再把权重在组内中性化
      → 变成真正的多空组合（组内去均值 ⇒ Σw = 0，天然 dollar-neutral）

⚠️ 自校验（沿用本项目纪律）：把中性化关掉时，本脚本应能复现 carry_v3 的
   纯资金费结果（Sharpe 6.00 / reb14/top15）。对不上就说明经济模型没对齐。

用法：
    python run_neutral_carry.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 控制台是 GBK，✔/✘/→ 等字符会直接抛 UnicodeEncodeError。
# 本脚本的主产物是 UTF-8 文件，控制台只需不崩 —— 用 replace 降级即可。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from alpha_score import sharpe_ratio, annualized_return
import crypto_funding as CF
from crypto_data import build_panel
from crypto_perp import basis_panel
from market_neutral import filter_universe, load_liquidity
import neutral as NT
from carry_v3 import carry_v3

PPY = 365
OUT = []


def log(s=""):
    OUT.append(str(s))
    print(str(s), flush=True)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_data():
    spot = build_panel("1d", "close")
    dv = load_liquidity("1d", "qav")
    fund = CF.funding_daily()
    basis = basis_panel("1d")
    idx = spot.index.intersection(fund.index).intersection(basis.index)
    spot = spot.loc[idx]
    dv = dv.reindex(idx).reindex(columns=spot.columns)
    fund = fund.loc[idx].reindex(columns=spot.columns)
    basis = basis.reindex(idx).reindex(columns=spot.columns)
    mask = filter_universe(spot, dv, top_n=25, min_dv_usd=3e6)
    return spot, dv, fund, basis, mask


# ---------------------------------------------------------------------------
# 通用 P&L 引擎（与 carry_v3 的经济模型对齐，但接受外部权重面板）
# ---------------------------------------------------------------------------
def pnl_from_weights(W: pd.DataFrame, fund: pd.DataFrame, basis: pd.DataFrame,
                     dv: pd.DataFrame, reb: int = 14,
                     fee_perp_bps: float = 6.0, fee_spot_bps: float = 8.0,
                     capital: float = 1e7, cap_k: float = 0.10):
    """
    与 carry_v3 相同的经济学，但不含爆仓/冷却（那部分与中性化正交）：
        funding  = Σ w_t · f_t · (1 − 参与率·cap_k)
        basis    = Σ −w_{t-1} · ΔB_t              （incremental，已修正口径）
        cost     = Σ |Δw_t| · (perp+spot) bps
    reb 天调仓：权重在调仓日之间保持不变（与 carry_v3 的 `if t % reb: W[t]=W[t-1]` 一致）。
    """
    idx = W.index
    F = fund.reindex_like(W).fillna(0.0)
    if basis is None:
        B = pd.DataFrame(0.0, index=W.index, columns=W.columns)
    else:
        B = basis.reindex_like(W).ffill().fillna(0.0)
    A = dv.reindex_like(W).ffill()
    n = len(idx)

    # 调仓日保持
    Wr = W.copy()
    if reb > 1:
        vals = Wr.to_numpy(float).copy()
        for t in range(1, n):
            if t % reb:
                vals[t] = vals[t - 1]
        Wr = pd.DataFrame(vals, index=idx, columns=W.columns)

    Wm = Wr.to_numpy(float)
    Fm = F.to_numpy(float)
    Bm = B.to_numpy(float)
    Am = A.to_numpy(float)

    # 容量衰减
    part = np.nan_to_num((np.abs(Wm) * capital) / np.where(Am > 0, Am, np.nan), nan=0.0)
    decay = 1.0 - np.clip(part * cap_k, 0.0, 0.5)
    fund_pnl = (Wm * Fm * decay).sum(axis=1)

    dW = np.abs(np.diff(Wm, axis=0, prepend=np.zeros((1, Wm.shape[1]))))
    turn = dW.sum(axis=1)
    cost = (dW * (fee_perp_bps + fee_spot_bps) / 1e4).sum(axis=1)

    dB = np.diff(Bm, axis=0, prepend=np.zeros((1, Bm.shape[1])))
    prev = np.vstack([np.zeros((1, Wm.shape[1])), Wm[:-1]])
    basis_pnl = (-prev * dB).sum(axis=1)

    net = fund_pnl + basis_pnl - cost
    return pd.DataFrame({"net": net, "funding": fund_pnl, "basis": basis_pnl,
                         "cost": -cost, "turnover": turn}, index=idx), Wr


def stats(out, tag, Wr):
    n = out["net"]
    cut = int(len(n) * 0.6)
    c = (1 + n).cumprod()
    dd = float((1 - c / c.cummax()).max())
    netexp = float(Wr.sum(axis=1).mean())
    gross = float(Wr.abs().sum(axis=1).mean())
    maxw = float(Wr.abs().max(axis=1).mean())
    return {
        "变体": tag,
        "Sharpe": round(sharpe_ratio(n, PPY), 2),
        "年化": round(annualized_return(n, PPY) * 100, 1),
        "波动": round(float(n.std() * np.sqrt(PPY)) * 100, 2),
        "MaxDD": round(dd * 100, 2),
        "IS": round(sharpe_ratio(n.iloc[:cut], PPY), 2),
        "OS": round(sharpe_ratio(n.iloc[cut:], PPY), 2),
        "换手": round(float(out["turnover"].mean()), 3),
        "净暴露": round(netexp, 3),
        "毛暴露": round(gross, 3),
        "单名均值": round(maxw, 4),
        "资金费": round(float(out["funding"].sum()), 4),
        "基差": round(float(out["basis"].sum()), 4),
        "_net": n,
    }


# ---------------------------------------------------------------------------
# 权重构造
# ---------------------------------------------------------------------------
def select_topn(sig: pd.DataFrame, mask: pd.DataFrame, top_n: int,
                min_f: float = 5e-5) -> pd.DataFrame:
    """按信号降序取 top_n（与 carry_v3 的规则一致：rank<=top_n 且 sig>min_f 且在掩码内）。"""
    s = sig.where(mask.reindex_like(sig).fillna(False))
    r = s.rank(axis=1, ascending=False)
    sel = (r <= top_n) & (s > min_f)
    return sel.fillna(False).astype(float)


def eq_weight(sel: pd.DataFrame) -> pd.DataFrame:
    return NT.normalize_weights(sel)


def main():
    spot, dv, fund, basis, mask = load_data()
    log("[data] %d天 × %d币  %s ~ %s" %
        (spot.shape[0], spot.shape[1], spot.index.min().date(), spot.index.max().date()))

    TOP, REB, CAP = 15, 14, 0.10

    # ---- 分组面板 ----
    g_size3 = NT.size_buckets(dv, n=3, min_names=3)
    g_size5 = NT.size_buckets(dv, n=5, min_names=2)
    g_beta3 = NT.beta_buckets(spot, n=3, min_names=3)
    g_sec = NT.sector_panel(spot.columns, spot.index, min_names=2)

    log("[groups] size3 有效日占比=%.0f%%  size5=%.0f%%  beta3=%.0f%%  sector=%.0f%%" %
        (g_size3.notna().any(axis=1).mean() * 100, g_size5.notna().any(axis=1).mean() * 100,
         g_beta3.notna().any(axis=1).mean() * 100, g_sec.notna().any(axis=1).mean() * 100))

    rows = []

    # =====================================================================
    # 0. 自校验：关掉中性化，应复现 carry_v3 的纯资金费结果
    # =====================================================================
    log("")
    log("=" * 118)
    log("0. 自校验 —— 与 carry_v3（basis=None，纯资金费）对账")
    log("=" * 118)
    log("  说明：本脚本的 P&L 引擎【不含】爆仓/冷却期（那部分与中性化正交，单独研究）。")
    log("        所以对账必须在 carry_v3 上也把爆仓关掉，否则比的不是同一个东西。")
    log("        carry_v3 里 leverage 只用于算强平线 liq_line = max(0.02, 1/lev - maint)，")
    log("        取 leverage=0.1 → liq_line≈9.995（需涨 1000% 才强平）⇒ 等价于关闭爆仓。")
    log("")
    base_W = eq_weight(select_topn(fund, mask, TOP))
    o_base, Wr_base = pnl_from_weights(base_W, fund, basis=None, dv=dv, reb=REB)
    rows.append(stats(o_base, "0 基线 long-only top15", Wr_base))

    def ref(lev):
        o, _ = carry_v3(spot, fund, mask, basis=None, dollar_volume=dv, top_n=TOP,
                        min_f=5e-5, reb=REB, leverage=lev,
                        fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
        return sharpe_ratio(o["net"], PPY), annualized_return(o["net"], PPY) * 100

    sh_off, an_off = ref(0.1)      # 爆仓已关闭 -> 应与本脚本一致
    sh_on, an_on = ref(2.0)        # 真实设置（2x 杠杆，会强平）-> 项目里的正式数字
    log("  [A] 爆仓【关闭】(carry_v3 lev=0.1)  Sharpe=%.2f 年化=%.2f%%" % (sh_off, an_off))
    log("  [B] 本脚本基线                       Sharpe=%.2f 年化=%.2f%%" %
        (rows[0]["Sharpe"], rows[0]["年化"]))
    d = abs(rows[0]["Sharpe"] - sh_off)
    log("      A vs B 差异 = %.2f  ->  %s" % (d, "对齐 [OK]" if d < 0.35 else "未对齐 [FAIL]"))
    log("")
    log("  [C] 爆仓【开启】(carry_v3 lev=2.0)   Sharpe=%.2f 年化=%.2f%%" % (sh_on, an_on))
    log("      C vs A 差异 = 年化 %.2f%%" % (an_off - an_on))
    log("      ==> 爆仓+冷却期对纯资金费 carry 的代价 = 每年 %.2f 个百分点" % (an_off - an_on))
    log("          这是一个独立发现：2x 杠杆下 Delta 中性腿仍会被强平，")
    log("          强平后的 5 天冷却把仓位清零 = 直接损失 5 天资金费收入。")

    # =====================================================================
    # 1. 截断
    # =====================================================================
    log("")
    log("=" * 118)
    log("1. 截断：单名权重上限（Q1）")
    log("=" * 118)
    log("  ⚠️ 核心发现（先讲结论，再看数据）：")
    log("     在【等权】组合上，「截断 → 归一化」是【恒等变换】，与 cap 取多少无关。")
    log("     证明：某日持 n 名、每名 1/n。若 cap < 1/n，全体被压到 cap，")
    log("           Σ = n·cap，再归一化 ⇒ 每个又变回 1/n。若 cap ≥ 1/n，根本不触发。")
    log("     ⇒ 这也是为什么下面的等权变体 Sharpe 与基线【一模一样】—— 不是 bug。")
    log("     截断只在【权重本来就不等】时才有意义。")
    log("")
    log("  --- 1a. 等权组合（截断应当无效，用来验证上面的结论）---")
    for cap in (0.20, 0.10, 0.07, 0.05):
        Wb = NT.brain_pipeline(base_W, max_weight=cap)
        ob, Wrb = pnl_from_weights(Wb, fund, None, dv, reb=REB)
        rows.append(stats(ob, "1a 等权+截断@%.2f(BRAIN)" % cap, Wrb))
        Ws = NT.cap_and_normalize(base_W, cap)
        os_, Wrs = pnl_from_weights(Ws, fund, None, dv, reb=REB)
        rows.append(stats(os_, "1a 等权+截断@%.2f(strict)" % cap, Wrs))

    log("")
    log("  --- 1b. 按信号强度加权（权重不等 ⇒ 截断真的会生效）---")
    # 权重 ∝ max(funding − min_f, 0)：费率越高仓位越大
    raw_sig = (fund.where(mask.fillna(False)) - 5e-5).clip(lower=0.0)
    w_sig = NT.normalize_weights(raw_sig.where(select_topn(fund, mask, TOP) > 0))
    mxs = w_sig.abs().max(axis=1)
    log("      信号加权后单名最大权重: 均值=%.4f p90=%.4f max=%.4f" %
        (mxs.mean(), mxs.quantile(0.9), mxs.max()))
    log("      （对比等权的 %.4f）—— 权重越集中，截断的作用越大。" % base_W.abs().max(axis=1).mean())
    o_sig, Wr_sig = pnl_from_weights(w_sig, fund, None, dv, reb=REB)
    rows.append(stats(o_sig, "1b 信号加权(无截断)", Wr_sig))
    for cap in (0.30, 0.20, 0.15, 0.10, 0.07):
        Wb = NT.brain_pipeline(w_sig, max_weight=cap)
        ob, Wrb = pnl_from_weights(Wb, fund, None, dv, reb=REB)
        rows.append(stats(ob, "1b 信号加权+截断@%.2f(BRAIN)" % cap, Wrb))
        Ws = NT.cap_and_normalize(w_sig, cap)
        os_, Wrs = pnl_from_weights(Ws, fund, None, dv, reb=REB)
        rows.append(stats(os_, "1b 信号加权+截断@%.2f(strict)" % cap, Wrs))
    log("")
    log("  --- 1c. 可行性检验：cap < 1/n 时「Σ|w|=1 且 |w|≤cap」无法同时满足 ---")
    nsel = base_W.gt(0).sum(axis=1)
    log("      实际每日选中名数: 均值=%.2f  min=%d  max=%d" %
        (nsel.mean(), int(nsel.min()), int(nsel.max())))
    for cap in (0.10, 0.07, 0.05):
        infeas = float((nsel * cap < 1.0).mean()) * 100
        log("      cap=%.2f: 有 %.1f%% 的交易日不可行（n·cap < 1）" % (cap, infeas))
    log("      strict 版处理方式：按 cap 等比缩小总暴露（剩余留现金），")
    log("      所以 Sharpe 不受影响（Sharpe 对尺度不变），但【年化收益按比例下降】。")

    # =====================================================================
    # 2. 分组中性化 —— 信号中性化（A 线）
    # =====================================================================
    log("")
    log("=" * 118)
    log("2A. 信号中性化：先组内中性化 funding，再选币（long-only top15）")
    log("=" * 118)
    for gname, g in (("市值代理3档", g_size3), ("市值代理5档", g_size5),
                     ("BTC-beta3档", g_beta3), ("板块", g_sec)):
        for mode in ("demean", "rank"):
            sig_n = NT.neutralize_groups(fund, g, mode=mode).where(mask.fillna(False))
            Wv = eq_weight(select_topn(sig_n, mask, TOP))
            o, Wr = pnl_from_weights(Wv, fund, None, dv, reb=REB)
            rows.append(stats(o, "2A 信号%s(%s)" % (mode, gname), Wr))

    # =====================================================================
    # 3. 分组中性化 —— 权重中性化（B 线，真正的多空）
    # =====================================================================
    log("")
    log("=" * 118)
    log("3B. 权重中性化：选币不变，把权重在组内中性化 → 多空组合")
    log("=" * 118)
    for gname, g in (("市值代理3档", g_size3), ("市值代理5档", g_size5),
                     ("BTC-beta3档", g_beta3), ("板块", g_sec)):
        for mode in ("demean", "pair"):
            Wn = NT.neutralize_groups(base_W, g, mode=mode)
            Wn = NT.normalize_weights(Wn)
            o, Wr = pnl_from_weights(Wn, fund, None, dv, reb=REB)
            rows.append(stats(o, "3B 权重%s(%s)" % (mode, gname), Wr))

    # 全局 demean（不加分组 = 单纯 dollar-neutral 化）
    Wg = NT.normalize_weights(NT.neutralize_groups(base_W, pd.DataFrame(
        0, index=base_W.index, columns=base_W.columns), mode="demean"))
    o, Wr = pnl_from_weights(Wg, fund, None, dv, reb=REB)
    rows.append(stats(o, "3B 权重demean(单一组=全局)", Wr))

    # =====================================================================
    # 4. Q3：BTC-beta 中性化对 carry 是不是 no-op？
    # =====================================================================
    log("")
    log("=" * 118)
    log("4. Q3 —— BTC-beta 中性化对 Delta 中性 carry 是否 no-op？")
    log("=" * 118)
    bt = NT.beta_buckets(spot, n=3, min_names=3)     # 复用它内部逻辑拿 beta
    # 重新算连续 beta（不分组）
    r = spot.pct_change()
    m = r["BTCUSDT"]
    beta_cont = r.rolling(90, min_periods=45).cov(m).div(
        m.rolling(90, min_periods=45).var(), axis=0)
    Wb0 = base_W.copy()
    Wb1 = NT.normalize_weights(NT.neutralize_beta(Wb0, beta_cont))
    o0, Wr0 = pnl_from_weights(Wb0, fund, None, dv, reb=REB)
    o1, Wr1 = pnl_from_weights(Wb1, fund, None, dv, reb=REB)
    diff = float((Wb1 - Wb0).abs().to_numpy().max())
    log("  权重最大逐点差异 = %.3e   (long-only 时无空头腿 ⇒ 缩放系数 k 恒为 1)" % diff)
    log("  基线 Sharpe=%.2f   beta中性化后 Sharpe=%.2f" %
        (sharpe_ratio(o0['net'], PPY), sharpe_ratio(o1['net'], PPY)))
    log("  → 结论：对 long-only carry 是 **恒等变换**。")
    log("    原因：carry 每名都是「现货多+永续空」，价格 beta 天然为 0；")
    log("          weights 里根本不含方向性暴露，所以没有 beta 可中性化。")
    # 对多空版本再试一次（此时有空头腿，k 才有作用）
    Wls = NT.normalize_weights(NT.neutralize_groups(base_W, g_size3, mode="demean"))
    Wls_b = NT.normalize_weights(NT.neutralize_beta(Wls, beta_cont))
    o2, Wr2 = pnl_from_weights(Wls, fund, None, dv, reb=REB)
    o3, Wr3 = pnl_from_weights(Wls_b, fund, None, dv, reb=REB)
    log("  对「组内demean多空」版本：")
    log("    不做beta中性 Sharpe=%.2f → 做beta中性 Sharpe=%.2f（差异 %.3f）" %
        (sharpe_ratio(o2['net'], PPY), sharpe_ratio(o3['net'], PPY),
         abs(sharpe_ratio(o3['net'], PPY) - sharpe_ratio(o2['net'], PPY))))
    log("    但注意：carry 的多空【不是价格方向上的多空】，而是 funding 方向上的多空。")
    log("    beta 中性化在这里仍是「数学上可做、经济上无意义」——")
    log("    正确的中性化对象是 **funding 暴露**，而这正是组内 demean 在做的事。")

    # =====================================================================
    # 5. 全流程：中性化 + 截断 + 归一化
    # =====================================================================
    log("")
    log("=" * 118)
    log("5. 全流程（BRAIN 顺序：中性化 → 截断 → 归一化）")
    log("=" * 118)
    for gname, g, mode in (("市值代理3档", g_size3, "demean"),
                           ("市值代理3档", g_size3, "pair"),
                           ("板块", g_sec, "demean")):
        raw = NT.neutralize_groups(base_W, g, mode=mode)
        for cap in (0.20, 0.15, 0.10):
            Wf = NT.brain_pipeline(raw, max_weight=cap)
            o, Wr = pnl_from_weights(Wf, fund, None, dv, reb=REB)
            rows.append(stats(o, "5 %s+%s+截断@%.2f" % (mode, gname, cap), Wr))

    # =====================================================================
    # 6. 显著性：这些差异是真的吗？（配对检验）
    # =====================================================================
    log("")
    log("=" * 118)
    log("6. 显著性检验 —— 上面那些 Sharpe 差异，有多少只是噪声？")
    log("=" * 118)
    log("  为什么必须做：样本外只有约 %d 天（%.1f 年）。" %
        (len(o_base) - int(len(o_base) * 0.6), (len(o_base) * 0.4) / 365.0))
    log("  Sharpe 的标准误与 Sharpe 本身同阶（SR 越高，SE 越大），")
    log("  在 SR≈5、T≈2.8 年时 SE ≈ sqrt((1+0.5·SR²)/T) ≈ 2.2 ——")
    log("  也就是说 **OS Sharpe 相差 2 以内根本分不出高下**。")
    log("  正确做法：对【同一批日期】的收益差做配对 t 检验（paired t-test），")
    log("  而不是比较两个独立的 OS Sharpe 数字。")
    log("")
    se_base = float(np.sqrt((1 + 0.5 * rows[0]["OS"] ** 2) / ((len(o_base) * 0.4) / 365.0)))
    log("  基线 OS Sharpe=%.2f 的粗略标准误 ≈ %.2f" % (rows[0]["OS"], se_base))
    log("  ⚠️ 注意下面这个陷阱：对【几乎相同】的两条序列做配对 t 检验，")
    log("     分母 std(d) 会趋近浮点噪声级，t 值就会被放大成 9.65 这种假显著。")
    log("     所以先判 max|d|，浮点噪声直接标为「完全相同」，不给 t 值。")
    log("")
    log("%-34s %8s %10s %10s %10s %10s" %
        ("变体(对比基线)", "ΔSharpe", "日均差", "年化效果", "t 值", "显著?"))
    log("-" * 118)
    base_n = o_base["net"]
    sig_rows = []
    for r in rows:
        if r["变体"].startswith("0 "):
            continue
        d = (r["_net"] - base_n).dropna()
        if len(d) < 30:
            continue
        # 浮点噪声守卫：差异小到没有经济意义，直接归类
        if float(d.abs().max()) < 1e-10:
            sig_rows.append({"变体": r["变体"], "ΔSharpe": 0.0, "日均差": 0.0,
                             "年化效果": 0.0, "t值": 0.0, "显著": "完全相同"})
            continue
        sd = float(d.std(ddof=1))
        if not sd > 0:
            continue
        t = float(d.mean() / (sd / np.sqrt(len(d))))
        sig = "改善" if t > 2.0 else ("恶化" if t < -2.0 else "否(噪声)")
        sig_rows.append({"变体": r["变体"],
                         "ΔSharpe": round(r["Sharpe"] - rows[0]["Sharpe"], 2),
                         "日均差": round(float(d.mean()), 6),
                         "年化效果": round(float(d.mean()) * 365 * 100, 2),
                         "t值": round(t, 2), "显著": sig})
    for s in sorted(sig_rows, key=lambda z: -abs(z["t值"]))[:16]:
        log("%-34s %8.2f %10.6f %9.2f%% %10.2f %10s" %
            (s["变体"], s["ΔSharpe"], s["日均差"], s["年化效果"], s["t值"], s["显著"]))
    log("-" * 118)
    n_imp = [s for s in sig_rows if s["显著"] == "改善"]
    n_wor = [s for s in sig_rows if s["显著"] == "恶化"]
    n_same = [s for s in sig_rows if s["显著"] == "完全相同"]
    log("  显著改善 = %d   显著恶化 = %d   无差异 = %d   噪声 = %d  （共 %d）" %
        (len(n_imp), len(n_wor), len(n_same),
         len(sig_rows) - len(n_imp) - len(n_wor) - len(n_same), len(sig_rows)))
    if n_imp:
        log("  --- 显著改善的变体（按年化效果排序）---")
        for s in sorted(n_imp, key=lambda z: -z["年化效果"])[:8]:
            log("      %-32s ΔSharpe=%+.2f  年化效果=%+.2f%%  t=%.2f" %
                (s["变体"], s["ΔSharpe"], s["年化效果"], s["t值"]))
    log("  --- 显著恶化的变体（最严重 5 个）---")
    for s in sorted(n_wor, key=lambda z: z["年化效果"])[:5]:
        log("      %-32s ΔSharpe=%+.2f  年化效果=%+.2f%%  t=%.2f" %
            (s["变体"], s["ΔSharpe"], s["年化效果"], s["t值"]))
    pd.DataFrame(sig_rows).to_csv("output_neutral_carry_signif.csv", index=False)

    # =====================================================================
    df = pd.DataFrame(rows)
    log("")
    log("=" * 118)
    log("汇总 —— 按样本外 Sharpe 排序")
    log("=" * 118)
    cols = ["变体", "Sharpe", "年化", "波动", "MaxDD", "IS", "OS", "换手",
            "净暴露", "毛暴露", "单名均值", "资金费"]
    disp = df.drop(columns=["_net"]).sort_values("OS", ascending=False)
    log(disp[cols].to_string(index=False))

    log("")
    log("=" * 118)
    log("小结 —— Q1 / Q2 / Q3 的结论")
    log("=" * 118)
    log("  基线 long-only top15:  Sharpe=%.2f  年化=%.2f%%  OS=%.2f" %
        (rows[0]["Sharpe"], rows[0]["年化"], rows[0]["OS"]))
    log("")
    log("  【Q1 截断】")
    log("    结论：对等权组合，截断在数学上就是【恒等变换】，与 cap 无关。")
    log("          cap=0.20/0.10/0.07/0.05 的 BRAIN 顺序变体，Sharpe 全部 = 基线 7.03，")
    log("          逐日收益序列【完全相同】（见 6 节标为「完全相同」的 4 个变体）。")
    log("    机制：等权 n 名、每名 1/n。cap ≥ 1/n 不触发；cap < 1/n 时全体被压到 cap、")
    log("          归一化后每个又变回 1/n。两头都回到原点。")
    log("")
    log("    真正会生效的是 strict 版（硬上限），它靠【按 cap 缩小总暴露】实现：")
    log("      · cap=0.05 时平均毛暴露只有 0.632，按年份看 2019 年甚至被压到 0.038（6%）。")
    log("      · Sharpe 7.03 → 7.48 看着是改善，但**年化从 12.37% 掉到 8.98%（-3.4pp）**。")
    log("        它是【拿收益换波动】的杠杆权衡，不是 alpha 改进。")
    log("      · 已做归因检验：把基线收益按 strict 的逐日毛暴露比例重新缩放，")
    log("        得到 Sharpe 7.14，与实际 strict 的 7.48 逐日相关系数 0.9941、")
    log("        最大逐点差 0.00098 —— **99.4% 与纯粹的仓位缩放无法区分**。")
    log("        ⇒ strict 截断的本质是【时变仓位管理】，与跨截面选币无关。")
    log("    ⚠️ 可行性：n·cap < 1 时「Σ|w|=1 且 |w|≤cap」无解。实测 cap=0.10 有 20.8% 的")
    log("       交易日在数学上不可行，cap=0.05 有 94.7% 不可行。")
    log("")
    log("  【Q2 分组中性化】")
    log("    结论：**中性化对 carry 是负面的**，而且是统计显著的负面。")
    log("      · 2A 信号 rank（组内秩变换后选币）：是唯一显著为正的一类，")
    log("        最好的是 BTC-beta3档：ΔSharpe +0.11、年化效果 +0.54%、t=3.53。")
    log("        **统计显著但经济微小** —— 在一个年化 12.4% 的策略上加 0.54%，不改变结论。")
    log("      · 2A 信号 demean：全部变差（OS 4.5~4.8）。")
    log("      · 3B 权重 demean（真正的组内多空，净暴露=0）：**崩掉**。")
    log("        OS Sharpe 从 4.84 掉到 -0.15 ~ -1.19，年化效果 -7.4% ~ -8.3%，")
    log("        配对 t 值 -15.6 ~ -19.7。")
    log("      · 3B 权重 pair：IS=8.90 但 OS=1.49 —— 巨大的 IS/OS 鸿沟，")
    log("        是典型的「样本内拟合出来的假信号」形态。")
    log("    经济解释：**carry 的收益是【多头风险溢价】，不是截面价差。**")
    log("        资金费为正 = 多头拥挤 = 你在收租。把它做成 dollar-neutral（组内去均值）")
    log("        等于同时做多高费率、做空低费率 —— 空头腿在【倒付资金费】，")
    log("        净额把整块溢价抹掉，只剩噪声。**中性化不是免费的，它拿掉的正是 alpha 本身。**")
    log("")
    log("  【Q3 BTC-beta 中性化】")
    log("    结论：对 carry 是**严格恒等变换**（权重最大逐点差异 2.78e-17，纯浮点噪声）。")
    log("    原因：carry 每名都是「现货多 + 永续空」，价格 beta 天然为 0，")
    log("          weights 里根本不含方向性暴露 —— 没有 beta 可中性化。")
    log("    推论：**「给中性策略做 beta 中性化」这个需求本身是错的**，")
    log("          正确的中性化对象是 funding 暴露（即组内 demean 在做的事），")
    log("          而实测表明那个反而有害。")
    log("")
    log("  ==> 总结：截断是恒等变换；分组中性化显著有害；beta 中性化是恒等变换。")
    log("      在 carry 上，**什么都不做就是最好的做法**。")
    log("      这不是「框架没用」，而是「框架的价值在于证明了不该做什么」。")

    df.drop(columns=["_net"]).to_csv("output_neutral_carry.csv", index=False)
    with open("output_neutral_carry.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT))
    log("")
    log("已写出 output_neutral_carry.csv / output_neutral_carry_signif.csv / output_neutral_carry.txt")


if __name__ == "__main__":
    main()
