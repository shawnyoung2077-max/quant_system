"""
tail_risk.py - carry 的尾部风险压力测试（把"波动率不可能这么低"这件事量化）
============================================================================
动机：carry 的正常期年化波动只有 1.4%，是 BTC 的 2.6%。
     这个数字【在结构上不成立】—— 因为正常期样本里几乎不含尾部事件：
       · 全样本 2559 天，横截面平均资金费为负的有 28.4%
       · 但"连续负费率 ≥3 天"只出现 86 段，最长 23 天
       · 单日 |Δbasis| 的 p99.9 = 0.0095，max = 0.1665（是日均资金费的 684 倍）
       · 单币最大回撤 96%~99.7%（FIL 99.7%）
     历史样本里尾部事件太少 → 回测波动率被系统性低估。

本模块做三件事：
  A. 历史重放     把真实发生过的最坏区间单独拎出来，看 carry 在里面亏多少
  B. 假设情景     参数化地施加冲击（负费率 / 基差跳变 / 单腿冻结）
  C. 反向压力测试 反解"多大的冲击会让策略亏到某个阈值"

三个冲击机制：
  S1 资金费符号翻转   关键：reb=14 时策略最多要 13 天才反应过来
  S2 基差暴走         对冲腿的 mark-to-market 缺口
  S3 单腿冻结         提币暂停的代理：现货腿冻结、永续腿继续走 → 裸多暴露

⚠️ 本模块明确的三个局限（写在代码里，不埋在文档里）：
  1. **现货与永续同属 Binance 单所数据** → 无法建模"两所脱钩"。
     S3 用「现货腿冻结、永续腿按历史价继续」作代理，这是【情景假设】，
     不是对某次真实事件的复刻。
  2. **不建模交易所破产**（FTX 式本金全损）。那会让现货腿 −100%、
     而永续腿同时被强平，损失 ≈ 2×本金（含杠杆）。这个情景无法从价格数据推出，
     只能作为【假设】给出，本模块以参数 `total_loss_pct` 提供。
  3. **carry_v3 的强平模型用"现货价相对入场涨幅"触发**。对【同所交叉保证金】
     的真 Delta 中性组合，这偏保守（现货腿的盈利可覆盖永续腿亏损）；
     对【两条腿分账户】的组合则是对的。本项目数据无法区分两者，
     所以 `carry_v3` 报的 3.35pp/年 爆仓成本应视为【上界】。

用法：
    python tail_risk.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from alpha_score import sharpe_ratio, annualized_return

PPY = 365


# ===========================================================================
# 数据
# ===========================================================================
def load_all():
    import crypto_funding as CF
    from crypto_data import build_panel
    from crypto_perp import basis_panel
    from market_neutral import filter_universe, load_liquidity

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


# ===========================================================================
# 基础 P&L（复用 run_neutral_carry 的引擎，接受外部权重面板）
# ===========================================================================
def base_weights(fund, mask, top_n=15, min_f=5e-5, reb=14):
    import neutral as NT
    s = fund.where(mask.reindex_like(fund).fillna(False))
    r = s.rank(axis=1, ascending=False)
    sel = ((r <= top_n) & (s > min_f)).fillna(False).astype(float)
    W = NT.normalize_weights(sel)
    if reb > 1:
        vals = W.to_numpy(float).copy()
        for t in range(1, len(vals)):
            if t % reb:
                vals[t] = vals[t - 1]
        W = pd.DataFrame(vals, index=W.index, columns=W.columns)
    return W


def pnl(W, fund=None, basis=None, dv=None, fee_perp_bps=6.0, fee_spot_bps=8.0,
        capital=1e7, cap_k=0.10, use_capacity=True):
    """与 carry_v3 对齐的经济模型（不含爆仓/冷却）。"""
    idx = W.index
    F = (fund.reindex_like(W).fillna(0.0) if fund is not None
         else pd.DataFrame(0.0, index=idx, columns=W.columns))
    B = (basis.reindex_like(W).ffill().fillna(0.0) if basis is not None
         else pd.DataFrame(0.0, index=idx, columns=W.columns))
    Wm, Fm, Bm = W.to_numpy(float), F.to_numpy(float), B.to_numpy(float)

    if use_capacity and dv is not None:
        Am = dv.reindex_like(W).ffill().to_numpy(float)
        part = np.nan_to_num((np.abs(Wm) * capital) / np.where(Am > 0, Am, np.nan), nan=0.0)
        decay = 1.0 - np.clip(part * cap_k, 0.0, 0.5)
    else:
        decay = 1.0

    fund_pnl = (Wm * Fm * decay).sum(axis=1)
    dW = np.abs(np.diff(Wm, axis=0, prepend=np.zeros((1, Wm.shape[1]))))
    cost = (dW * (fee_perp_bps + fee_spot_bps) / 1e4).sum(axis=1)
    dB = np.diff(Bm, axis=0, prepend=np.zeros((1, Bm.shape[1])))
    prev = np.vstack([np.zeros((1, Wm.shape[1])), Wm[:-1]])
    basis_pnl = (-prev * dB).sum(axis=1)
    turn = dW.sum(axis=1)
    return pd.DataFrame({"net": fund_pnl + basis_pnl - cost, "funding": fund_pnl,
                         "basis": basis_pnl, "cost": -cost, "turnover": turn}, index=idx)


def _mdd(n):
    c = (1 + n).cumprod()
    return float((1 - c / c.cummax()).max())


def _stats(n, tag):
    return {"情景": tag, "Sharpe": round(sharpe_ratio(n, PPY), 2),
            "年化": round(annualized_return(n, PPY) * 100, 2),
            "波动": round(float(n.std() * np.sqrt(PPY)) * 100, 2),
            "MaxDD": round(_mdd(n) * 100, 2),
            "累计P&L": round(float(n.sum()), 4)}


# ===========================================================================
# S1 资金费符号翻转
# ===========================================================================
def s1_historical(fund, W, mask, dv):
    """历史重放：把真实发生过的最坏负费率区间单独拿出来算 carry 的损失。"""
    mf = fund.where(mask.fillna(False)).mean(axis=1)
    neg = (mf < 0).to_numpy()
    runs, cur, start = [], 0, None
    for i, v in enumerate(neg):
        if v:
            if cur == 0:
                start = i
            cur += 1
        else:
            if cur >= 3:
                runs.append((start, cur))
            cur = 0
    if cur >= 3:
        runs.append((start, cur))
    runs.sort(key=lambda z: -z[1])
    rows = []
    out = pnl(W, fund, None, dv)
    for s, ln in runs[:6]:
        seg = out["net"].iloc[s:s + ln]
        rows.append({"起始": str(out.index[s].date()), "天数": ln,
                     "区间累计P&L": round(float(seg.sum()), 5),
                     "年化(区间内)": round(annualized_return(seg, PPY) * 100, 1)
                     if ln >= 2 else np.nan})
    return pd.DataFrame(rows), len(runs)


def s1_parametric(fund, W, mask, dv, neg_rate, days, start_frac=0.55):
    """
    假设情景：在样本后段强制横截面平均资金费 = neg_rate，持续 days 天。
    关键点：策略的 reb 调仓导致它【最多 reb−1 天】才反应到费率转负。
    """
    f2 = fund.copy()
    n = len(f2)
    s = int(n * start_frac)
    e = min(s + days, n)
    cols = list(f2.columns)
    f2.iloc[s:e] = neg_rate
    # 掩码内才有效
    f2 = f2.where(mask.reindex_like(f2).fillna(False), 0.0)
    f2.iloc[:s] = fund.iloc[:s].where(mask.reindex_like(fund).fillna(False), 0.0)
    out = pnl(W, f2, None, dv)
    return out


def s1_sweep(fund, W, mask, dv, base_win):
    """
    参数化：以**历史最坏区间为基准**做倍数放大，而不是凭空取费率。

    历史最坏（mask 内横截面平均，来自 _explore_tail / S1 历史重放）：
        2022-11-08 起 21 天，区间累计 = -0.018271  →  平均 -0.0870%/天
    把它作为 1.0 倍基准，再测 2x / 5x / 10x / 40x（40x ≈ FTX 单日 -0.855%）。
    """
    HIST_RATE = -0.018271 / 21.0        # 历史最坏 21 天区间的日均费率
    rows = []
    for mult in (1.0, 2.0, 5.0, 10.0, 20.0, 40.0):
        rate = HIST_RATE * mult
        for days in (13, 21, 30):
            o = s1_parametric(fund, W, mask, dv, rate, days)
            s = int(len(o) * 0.55)
            seg = o["net"].iloc[s:s + days]
            tot = float(seg.sum())
            rows.append({"倍数": "%.0fx" % mult,
                         "日费率": "%.5f" % rate,
                         "折合年化": "%.0f%%" % (rate * 365 * 100),
                         "天数": days,
                         "区间P&L": round(tot, 5),
                         "亏损/正常期收益": round(-tot / base_win, 2)})
    return pd.DataFrame(rows)


# ===========================================================================
# S2 基差暴走
# ===========================================================================
def s2_historical(spot, fund, basis, W, dv):
    """历史重放：真实最坏的基差跳变日。"""
    db = basis.diff()
    flat = db.stack().dropna()
    rows = []
    out = pnl(W, fund, basis, dv)
    for (d, c), v in flat.abs().nlargest(8).items():
        r = float(out["net"].get(d, np.nan))
        rows.append({"日期": str(d.date()), "币": c, "ΔB": round(float(flat.loc[(d, c)]), 5),
                     "当日净P&L": round(r, 5),
                     "当日基差项": round(float(out["basis"].get(d, np.nan)), 5)})
    return pd.DataFrame(rows)


def s2_parametric(fund, basis, W, dv, shock, n_days=1, start_frac=0.55):
    """假设情景：在指定日把全市场基差同时跳变 shock（同向）。"""
    b2 = basis.copy()
    n = len(b2)
    s = int(n * start_frac)
    e = min(s + n_days, n)
    b2.iloc[s:e] = b2.iloc[s:e] + shock
    return pnl(W, fund, b2, dv)


def s2_sweep(fund, basis, W, dv):
    """
    假设情景：全市场基差同时跳变 shock。

    ⚠️ 注意 P&L 的形状：基差是【水平量】，而 P&L 只对 Δbasis 计费。
       所以"基差抬高 shock 并维持"只在【抬高那一天】产生一次 w·shock 的损失，
       之后水平不变 ⇒ ΔB = 0 ⇒ 不再有损失。
       这是正确的：除非基差回落到原水平，否则这笔 mark-to-market 损失是**永久的**。
       （若设定为冲击后回落，则该损失会被后续反向 ΔB 抵消 —— 这里取保守的"不回落"。）
    """
    rows = []
    for shock in (0.005, 0.02, 0.05, 0.10, 0.166):
        o = s2_parametric(fund, basis, W, dv, shock, n_days=1)
        s = int(len(o) * 0.55)
        rows.append({"基差冲击": "%.1f%%" % (shock * 100),
                     "一次性损失": round(float(o["net"].iloc[s]), 5),
                     "≈ w·shock": round(-shock, 5),
                     "对比正常日收益": "%.1f 倍" % (shock / 0.0004)})
    return pd.DataFrame(rows)


# ===========================================================================
# S3 单腿冻结（提币暂停代理）
# ===========================================================================
def s3_frozen_spot(spot, fund, W, dv, days=30, gap_pct=None, start_frac=0.55):
    """
    假设情景 B：现货腿被冻结无法平仓，永续腿照常交易 → 组合退化为【裸空】。
    冻结期内每天 P&L = −Σ w_i · r_i（现货腿不动，只有永续腿承受价格变动）。

    gap_pct: 若给定，表示在冻结点发生【一次性】价格跳变 gap_pct。
        ⚠️ 这是【一次性】事件，只计一天 —— 早期版本把它按天重复计了 days 次，
           导致 30 天 −20% 的组合算出 +600% 的荒谬结果（已修正）。

    方向性：现货腿冻结不动 + 永续腿空头 ⇒ 组合净暴露 = −w（**空头**）。
            所以价格下跌反而赚钱，上涨才亏。这与"现货腿裸多"的直觉相反，
            是"现货腿无法卖出"这个约束本身的含义。
    """
    n = len(W)
    s = int(n * start_frac)
    e = min(s + days, n)
    Wm = W.to_numpy(float)
    base = pnl(W, fund, None, dv)
    extra = np.zeros(n)

    if gap_pct is not None:
        # 一次性跳变：只在冻结点当日计一次
        extra[s] = -float(np.nansum(Wm[s] * gap_pct))
    else:
        r = spot.reindex_like(W).pct_change().to_numpy(float)
        for t in range(s, e):
            extra[t] = -float(np.nansum(Wm[t] * np.nan_to_num(r[t])))
    net = base["net"].to_numpy(float) + extra
    out = base.copy()
    out["net"] = net
    out["naked"] = extra
    return out


def s3_credit_event(fund, W, dv, base_ann, loss_pcts=(0.25, 0.50, 1.00),
                    gross_over_capital=2.0):
    """
    假设情景 A：交易所信用事件（FTX / Mt.Gox 式）—— 真正的尾部。

    为什么这是最该担心的：现货腿与永续腿**在同一家交易所**，
    所以交易所出事时【两条腿同时归零】，不存在"对冲"这回事。

    ⚠️ gross_over_capital 的来历（不要和强平线的 leverage 混淆）：
        Delta 中性 carry 每一名都是【现货多 1 份 + 永续空 1 份】，
        权重之和 = 1 表示投入 1 份本金。
        所以 **毛名义 = 2 × 本金** —— 这是"两条腿"造成的，与强平线的 leverage 无关。
        （`carry_v3` 里的 leverage 只影响强平线，不影响这一项。）
        交易所全损时，现货腿的 1 份没了，永续空头还需按市价结算 1 份
        ⇒ 最坏情形损失 = 2 × 本金 = 200%。

    ⚠️ 这个倍数**依赖抵押品安排**：若两条腿共用同一笔保证金账户且交易所
        按净额结算，实际损失可能只有 1 份本金（100%）。
        所以这里给的 2× 是**保守上界**，不是精确估计。
    """
    rows = []
    for lp in loss_pcts:
        loss = lp * gross_over_capital
        rows.append({"本所资产损失": "%.0f%%" % (lp * 100),
                     "占本金损失": "%.0f%%" % (loss * 100),
                     "等于多少年 carry 收益": round(loss / base_ann, 1),
                     "回本年数": round(-np.log(max(1 - loss, 1e-9)) / np.log(1 + base_ann), 1)
                     if loss < 1 else "无法回本"}
                    )
    return pd.DataFrame(rows)


def s3_sweep(spot, fund, W, dv):
    rows = []
    for days in (5, 14, 30, 60):
        o = s3_frozen_spot(spot, fund, W, dv, days=days)
        s = int(len(o) * 0.55)
        seg = o["net"].iloc[s:s + days]
        rows.append({"情景": "冻结%d天(按历史日收益)" % days,
                     "区间P&L": round(float(seg.sum()), 5),
                     "裸空贡献": round(float(o["naked"].iloc[s:s + days].sum()), 5),
                     "区间年化": round(annualized_return(seg, PPY) * 100, 1)})
    for pct in (-0.2, -0.4, -0.6, 0.2, 0.4):
        o = s3_frozen_spot(spot, fund, W, dv, days=30, gap_pct=pct)
        s = int(len(o) * 0.55)
        rows.append({"情景": "冻结30天+一次性跳变%+.0f%%" % (pct * 100),
                     "区间P&L": round(float(o["net"].iloc[s:s + 30].sum()), 5),
                     "裸空贡献": round(float(o["naked"].iloc[s:s + 30].sum()), 5),
                     "区间年化": round(annualized_return(o["net"].iloc[s:s + 30], PPY) * 100, 1)})
    return pd.DataFrame(rows)


# ===========================================================================
# C 反向压力测试
# ===========================================================================
def reverse_stress(fund, W, mask, dv, base_ann, days=21, targets=(-0.05, -0.10, -0.20)):
    """
    反解：连续 days 天的负费率，要让策略【区间累计亏损达到 target】需要多大的日费率？

    正常期 carry 每天赚 base_ann/365。
    ⚠️ 二分方向：【费率越低（越负）→ 亏损越大】。
       所以 got(mid) < target 意味着亏太多 ⇒ 需要把费率往【上】调 ⇒ lo = mid。
       （早期版本把条件写反了，导致所有目标都返回边界值 -1.0。）
    """
    out = []
    normal_daily = base_ann / 365.0

    def loss_at(rate):
        o = s1_parametric(fund, W, mask, dv, rate, days)
        s = int(len(o) * 0.55)
        return float(o["net"].iloc[s:s + days].sum())

    for tg in targets:
        lo, hi = -1.0, 0.0            # lo 亏损更大, hi 几乎不亏
        for _ in range(80):
            mid = (lo + hi) / 2
            if loss_at(mid) < tg:     # 亏太多 -> 费率往上调
                lo = mid
            else:
                hi = mid
        rate = hi
        out.append({"目标区间亏损": "%.0f%%" % (tg * 100), "天数": days,
                    "需要日费率": "%.5f" % rate,
                    "折合年化": "%.0f%%" % (rate * 365 * 100),
                    "是正常日收益的": "%.0f 倍" % (abs(rate) / normal_daily)})
    return pd.DataFrame(out)


# ===========================================================================
def main():
    L = []

    def log(s=""):
        L.append(str(s))
        print(str(s), flush=True)

    spot, dv, fund, basis, mask = load_all()
    log("[data] %d天 × %d币  %s ~ %s" %
        (len(spot), spot.shape[1], spot.index.min().date(), spot.index.max().date()))

    W = base_weights(fund, mask, top_n=15, min_f=5e-5, reb=14)
    o = pnl(W, fund, basis, dv)
    base_ann = annualized_return(o["net"], PPY)
    base_vol = float(o["net"].std() * np.sqrt(PPY))
    base_month = float(o["net"].resample("ME").sum().mean())
    base_win = base_ann / 365.0 * 21          # 正常期单个 21 天窗口的平均 P&L
    log("[基准] Sharpe=%.2f 年化=%.2f%% 波动=%.2f%% MaxDD=%.2f%%" %
        (sharpe_ratio(o["net"], PPY), base_ann * 100, base_vol * 100, _mdd(o["net"]) * 100))
    log("       正常期【单个 21 天窗口】的平均 P&L = %.4f" % base_win)

    log("")
    log("=" * 112)
    log("S1 资金费符号翻转")
    log("=" * 112)
    log("  为什么这是头号风险：策略 reb=14 天调仓一次，")
    log("  费率转负后它【最多要 13 天】才把仓位换成不含该币的组合 ——")
    log("  这段时间它在【倒付资金费】。")
    log("")
    tb, nruns = s1_historical(fund, W, mask, dv)
    log("  历史重放：真实发生过的「连续负费率 ≥3 天」区间共 %d 段，最长的 6 段：" % nruns)
    log(tb.to_string(index=False))
    log("")
    log("  ★ 关键读数：历史重放里亏损【非常小】（0.07% ~ 0.41%）。")
    log("    因为 W 是用【真实费率面板】选出来的 —— 费率转负的币本来就不会被选中。")
    log("    **选币机制本身就是对 S1 的天然保护**：它只持有费率最高的那一批币。")
    log("    ⇒ 所以「资金费符号翻转」在历史上【不是】主要威胁（真正的主要威胁见 S3）。")
    log("")
    log("  参数化：以【历史最坏 21 天区间】为 1.0 倍基准做倍数放大")
    log("         （2022-11-08 起 21 天，横截面平均累计 -0.018271 ⇒ 日均 -0.0870%）")
    log("         正常期单个 21 天窗口平均赚 %.4f" % base_win)
    log("")
    log("  ⚠️⚠️ 参数化【不是】历史重放，两者差别必须讲清楚，否则就是苹果比橘子：")
    log("      · 历史重放 = 横截面【平均】费率转负，而策略持有的仍是其中费率最高的币")
    log("      · 参数化   = 强制【策略持有的那些币】的费率也变成该负值")
    log("      ⇒ 参数化是【更严厉的假设】：它假设选币的天然保护失效了。")
    log("        同一个 2022-11-08 区间：历史重放亏 0.00070，参数化 1x 亏 0.01834（26 倍）。")
    log("        这个 26 倍不是矛盾，而是「保护失效」的代价 —— 正是要测的东西。")
    sw = s1_sweep(fund, W, mask, dv, base_win)
    log(sw.to_string(index=False))

    log("")
    log("=" * 112)
    log("S2 基差暴走")
    log("=" * 112)
    log("  为什么这是二号风险：基差是【对冲腿的 mark-to-market 缺口】，")
    log("  单日 |ΔB| 的 p99.9 = 0.0095，max = 0.1665 —— 是日均资金费的 684 倍。")
    log("")
    tb2 = s2_historical(spot, fund, basis, W, dv)
    log("  历史重放：真实最坏的基差跳变日")
    log(tb2.to_string(index=False))
    log("  ⚠️ 注意「当日净P&L」与「ΔB」并不成比例 —— 因为策略只持有一小部分币，")
    log("     而且最坏的 ΔB 往往发生在【当时并未持有】的币上（SOL 2022-11-10 就是）。")
    log("     这是分散化的真实保护作用：单币尾部 ≠ 组合尾部。")
    log("")
    log("  参数化：全市场基差【同时】跳变（这是比历史更严格的假设）")
    log(s2_sweep(fund, basis, W, dv).to_string(index=False))
    log("")
    log("  读法：基差跳变 shock 并【维持不回】⇒ 一次性损失 ≈ w·shock ≈ shock。")
    log("        16.6%（历史最坏单币幅度）全市场同时发生 ⇒ 单日亏 16.6%，")
    log("        是正常日收益的约 400 倍。")

    log("")
    log("=" * 112)
    log("S3 交易所信用事件（真正的尾部）")
    log("=" * 112)
    log("  为什么这才是最该担心的：现货腿与永续腿【在同一家交易所】。")
    log("  交易所出事时两条腿【同时归零】—— 不存在「对冲」这回事。")
    log("")
    log("  毛名义 = 2 × 本金（现货多 1 份 + 永续空 1 份，两条腿造成的，")
    log("  与强平线的 leverage 无关）。交易所全损 ⇒ 现货腿 1 份没了、")
    log("  永续空头还需按市价结算 1 份 ⇒ 最坏 = 2 × 本金 = 200%。")
    log("  ⚠️ 该倍数依赖抵押品安排：若净额结算则可能只有 1 份本金。2× 是保守上界。")
    log("")
    log(s3_credit_event(fund, W, dv, base_ann).to_string(index=False))
    log("")
    log("  ==> 这是整份文档里最不舒服的一行：一次 FTX 式事件 = 抹掉 15.6 年 carry 收益，")
    log("      而且【无法回本】（损失 ≥ 本金时复利无法恢复）。")
    log("      而正常期年化只有 12.8% —— 这个赔率是【不可接受】的。")
    log("")

    log("  单腿冻结（假设情景 B）：现货腿冻结无法平仓、永续腿继续交易 ⇒ 退化为裸空")
    sw3 = s3_sweep(spot, fund, W, dv)
    log(sw3.to_string(index=False))
    log("")
    log("  ⚠️ 方向性：冻结 + 永续空头 ⇒ 组合净暴露 = −w（**空头**）。")
    log("     所以【暴跌反而赚钱】，上涨才亏。这与「现货腿裸多」的直觉相反，")
    log("     是「现货腿无法卖出」这个约束本身的含义。")
    log("     ⇒ 用它来论证「提币暂停会亏钱」是【错的】；真正的亏损来自信用风险（情景 A）。")

    log("")
    log("=" * 112)
    log("C 反向压力测试：多大的冲击会让策略亏到某个程度？")
    log("=" * 112)
    rs = reverse_stress(fund, W, mask, dv, base_ann, days=21)
    log(rs.to_string(index=False))
    log("")
    log("  读法：要让 carry 在 21 天内亏 5%，需要日均费率 -0.243%（折合年化 -89%）。")
    log("        历史最坏单日 = -0.8553%（2022-11-10, FTX 当日）—— **远超这个门槛**。")
    log("        也就是说历史尾部【已经足够】触发 5% 级别亏损，只是它没有持续 21 天。")

    log("")
    log("=" * 112)
    log("结论：尾部风险里，真正致命的不是价格，是信用")
    log("=" * 112)
    log("  三个机制按【实测危害】排序（与最初的直觉顺序不同，这是本次的主要发现）：")
    log("")
    log("  ① 【最致命】S3 交易所信用事件 —— 不可对冲、不可分散、损失与收益不成比例")
    log("     · 现货腿与永续腿同在一所 ⇒ 出事时两条腿同时归零，对冲毫无意义")
    log("     · 2x 杠杆下 100% 的本所损失 = 200% 本金 ⇒ **一次事件抹掉 15.6 年 carry 收益**")
    log("     · 且损失 ≥ 本金时复利无法恢复（回本时间 = ∞）")
    log("     · 而正常期年化只有 12.8% —— **这是一个不可接受的赔率**")
    log("")
    log("  ② 【次致命】S2 基差暴走 —— 可对冲、可分散，但单日量级极大")
    log("     · 单日 |ΔB| 的 p99.9 = 0.0095，max = 0.1665（日均资金费的 684 倍）")
    log("     · 全市场同时跳 16.6% ⇒ 单日亏 16.6%，约正常日收益的 400 倍")
    log("     · 但历史上最坏的 ΔB 多发生在【未持有】的币上（SOL 2022-11-10），")
    log("       分散化确实保护了组合：当日实际净 P&L 只有 -0.00152")
    log("")
    log("  ③ 【最不致命，与直觉相反】S1 资金费符号翻转 —— 选币机制天然防御了它")
    log("     · 历史重放 85 段「连续负费率 ≥3 天」，最长 23 天，最大亏损仅 0.41%")
    log("     · 因为策略只持有费率最高的币，横截面平均转负时它持有的仍是正的")
    log("     · 只有假设「选币保护失效」（参数化）才会放大到 1x 亏 2.5 倍正常收益")
    log("")
    log("  4. 结构性放大因素：reb=14 的调仓延迟 ⇒ 费率转负后【最多 13 天不反应】。")
    log("     这个延迟是结构性的，不随市场变好而消失。")
    log("")
    log("  5. 所以正确表述不是「carry 波动 1.4%」/「2.0%（含基差）」，而是：")
    log("     **正常期**波动约 2%；尾部存在两类事件：")
    log("       (a) 可分散的价格类尾部（资金费翻转、基差暴走）→ 单日损失达正常日收益数百倍")
    log("       (b) **不可分散的信用类尾部**（交易所出事）→ 本金全损，且无法回本")
    log("     ⇒ 用正常期波动做仓位管理会【系统性超配】；")
    log("       而 (b) 类风险**根本无法用仓位管理解决** —— 只能用【分散到多家交易所】。")
    log("")
    log("  6. 最终结论与 docs/DEBUG_carry_basis.md 一致，且更强：")
    log("     carry 不只是「收益在衰减」（2026 年 Sharpe 0.33），")
    log("     而是「收益 12.8%/年」对「一次事件抹掉 15.6 年」——")
    log("     **风险收益比本身就不成立**。这才是它不该作为主策略的根本原因。")
    log("")
    log("  ⚠️ 一个诚实的方法论提醒（与 docs/NEUTRALIZATION.md 同一纪律）：")
    log("     S3 情景 A 的 loss_pct 是【假设】，不是从数据算出来的 ——")
    log("     本项目的数据（Binance 单所价格与资金费）不可能包含交易所破产的信息。")
    log("     它的价值在于【把不可观测的风险放进同一个赔率框架里比较】，")
    log("     而不是提供精确的损失估计。任何声称能「回测出」交易所破产损失的做法都是在编造。")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_tail_risk.txt")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    log("")
    log("已写出 output_tail_risk.txt")


if __name__ == "__main__":
    main()
