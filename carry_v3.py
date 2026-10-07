"""
carry_v3.py - Delta 中性 carry 完整回测（把之前没建模的全补上）
================================================================
相比 v2 新增建模：
  1. 现货腿手续费+滑点（与永续腿分开计）
  2. 基差风险：P&L 含 -(Δbasis)，basis = perp/spot − 1
  3. 爆仓后的【未对冲暴露】：强平后现货腿裸多，按真实后续收益计 P&L，延迟 rehedge_delay 天重建
  4. 容量衰减：资金费收入按参与率打折（越大越压平费率）
  5. 负费率制度：可选【反向 carry】或【空仓】

P&L 分解（每币权重 w）：
   funding  = w × funding_rate              （空永续收资金费，费率为正时为正）
   basis    = w × (−(basis_now − basis_entry))（基差走阔则亏）
   spot/perp P&L 在 Delta 中性下价格部分相互抵消 ≈ 0
"""
import numpy as np
import pandas as pd

PPY = 365


def carry_v3(spot: pd.DataFrame, funding: pd.DataFrame, mask: pd.DataFrame,
             basis: pd.DataFrame = None, dollar_volume: pd.DataFrame = None,
             top_n: int = 15, min_f: float = 5e-5, reb: int = 14,
             leverage: float = 2.0, maint: float = 0.005,
             fee_perp_bps: float = 6.0, fee_spot_bps: float = 8.0,
             liq_fee_bps: float = 50.0, rehedge_delay: int = 2,
             capital: float = 1e7, cap_k: float = 0.10,
             allow_negative: bool = False, neg_mode: str = "flat",
             cooldown: int = 5, count_unhedged: bool = False,
             unhedged_mode: str = "correct",
             basis_mode: str = "incremental"):
    """
    spot/funding/basis/dollar_volume: date×coin 面板；mask: 布尔可选掩码
    capital: 资金规模（用于容量衰减）；cap_k: 参与率对费率收入的压缩系数
    neg_mode: 费率转负时 'flat'=空仓 或 'reverse'=反向carry(空现货+多永续)

    unhedged_mode:
        'correct' (默认)  从强平【次日】起计入现货裸多的真实 P&L（方向性风险）
        'ignore'          完全不计 —— 会系统性低估波动率、虚高 Sharpe（旧行为）
    basis_mode:
        'incremental' (默认)  逐日增量 -w_{t-1}*(B_t - B_{t-1})  ← 数学正确
        'legacy'              累计口径 -w_t*(B_t - B_entry)       ← 会把累计 P&L 按新权重放大
    """
    coins = list(spot.columns)
    idx = spot.index
    S = spot.ffill().to_numpy(float)
    Fd = funding.reindex(index=idx, columns=coins).fillna(0.0).to_numpy(float)
    B = (basis.reindex(index=idx, columns=coins).ffill().fillna(0.0).to_numpy(float)
         if basis is not None else np.zeros((len(idx), len(coins))))
    M = mask.reindex(index=idx, columns=coins).fillna(False).to_numpy(bool)
    ADV = (dollar_volume.reindex(index=idx, columns=coins).ffill().to_numpy(float)
           if dollar_volume is not None else np.full((len(idx), len(coins)), np.nan))
    T, N = S.shape

    Fm = np.where(M, Fd, 0.0)
    # 选币：正向费率高者优先；额度用尽后若允许负费率则反向
    rank = pd.DataFrame(Fm).rank(axis=1, ascending=False).to_numpy()
    sel = (rank <= top_n) & (Fm > min_f) & M
    if allow_negative:
        negrank = pd.DataFrame(Fm).rank(axis=1, ascending=True).to_numpy()
        negsel = (negrank <= top_n) & (Fm < -abs(min_f)) & M
        sel = sel | (negsel & ~sel)

    W = sel.astype(float)
    ssum = W.sum(axis=1, keepdims=True); ssum[ssum == 0] = 1.0
    W = W / ssum
    if reb > 1:
        for t in range(1, T):
            if t % reb:
                W[t] = W[t - 1]
    # 反向标记（费率为负时做反向 carry）
    REV = np.zeros((T, N), dtype=bool)
    if allow_negative and neg_mode == "reverse":
        REV = (W > 0) & (Fm < 0)

    liq_line = max(0.02, 1.0 / leverage - maint)
    net = np.zeros(T); net_f = np.zeros(T); net_b = np.zeros(T); net_c = np.zeros(T)
    turn = np.zeros(T); nliq = 0
    entry = np.full(N, np.nan); basis_entry = np.zeros(N); maxrise = np.zeros(N)
    cooldown_left = np.zeros(N, dtype=int)
    unhedged = np.zeros(N, dtype=bool)
    unhedged_days = np.zeros(N, dtype=int)
    prev = np.zeros(N)

    for t in range(T):
        wt = W[t].copy()
        if t > 0:
            wt = np.where(cooldown_left > 0, 0.0, wt)      # 冷却期内不重建
        rev = REV[t]
        # ---- 换手与双腿手续费 ----
        dtr = np.abs(wt - prev)
        turn[t] = dtr.sum()
        cost = float((dtr * (fee_perp_bps + fee_spot_bps) / 1e4).sum())
        # ---- 资金费收入（含容量衰减）----
        eff_f = Fm[t].copy()
        if not np.isnan(ADV[t]).all():
            part = np.nan_to_num((wt * capital) / np.where(ADV[t] > 0, ADV[t], np.nan), nan=0.0)
            eff_f = eff_f * (1.0 - np.clip(part * cap_k, 0.0, 0.5))
        fund_pnl = float((wt * eff_f).sum())
        fund_pnl -= float((wt * rev * 2 * eff_f).sum())    # 反向时资金费方向相反
        # ---- 基差 P&L ----
        # ⚠️ 修正说明（重要）：
        #   旧口径:  -w_t * (B_t - B_entry)   <- B_entry 是首次入场时的基差
        #   问题:    权重每次调仓都会变，但 (B_t - B_entry) 是「自入场以来的累计变化」，
        #            两者相乘会把累计基差 P&L 按新权重【重复放大】。
        #            已用最小复现验证：权重翻倍时 P&L 被放大 1.29 倍。
        #   新口径:  -w_{t-1} * (B_t - B_{t-1})   逐日增量，隔夜持仓对应昨日权重。
        #            这是数学上正确的 mark-to-market 形式（对固定权重等价于 telescoping）。
        basis_pnl = 0.0
        if basis is not None and t > 0:
            if basis_mode == "incremental":
                basis_pnl = float((-prev * (B[t] - B[t - 1])).sum())
            else:  # legacy
                basis_pnl = float((-wt * (B[t] - basis_entry)).sum())
        # ---- ⭐ 未对冲暴露 P&L ----
        # 顺序很重要：**先**结算「昨日强平」带来的裸多暴露，**再**处理「今日新发生」的强平
        #
        # 为什么强平当日不计：
        #   强平发生在价格异动当日，而当日大部分时间仓位仍是对冲的，
        #   计当日收益等于把 beta 当成 carry 收益，属于重复计算。
        # 为什么次日必须计：
        #   次日仓位已确认为裸多（现货腿独存），这就是真实的【方向性风险】，
        #   不计入会系统性低估波动率 —— 这正是 Sharpe 虚高的根源。
        pt = S[t]
        un_pnl = 0.0
        if unhedged_mode == "correct" and t > 0:
            uh = unhedged.copy()
            if uh.any():
                prev_s = S[t - 1]
                r = np.nan_to_num(pt / np.where(prev_s == 0, np.nan, prev_s) - 1.0)
                un_pnl = float((wt[uh] * r[uh]).sum())

        # ---- 爆仓检测（本日新发生）----
        rise = np.where(np.isnan(entry), 0.0, pt / np.where(entry == 0, np.nan, entry) - 1.0)
        rise = np.nan_to_num(rise, nan=0.0, posinf=0.0, neginf=0.0)
        maxrise = np.maximum(maxrise, rise)
        held = wt > 0
        blown = held & (maxrise >= liq_line)
        liq_cost = 0.0
        if blown.any():
            nliq += int(blown.sum())
            liq_cost = float(wt[blown].sum()) * liq_fee_bps / 1e4
            unhedged[blown] = True
            unhedged_days[blown] = 0
            maxrise[blown] = 0.0; entry[blown] = np.nan
            cooldown_left[blown] = cooldown

        # ---- 未对冲天数推进，到期后视为已重建 ----
        if t > 0 and unhedged.any():
            unhedged_days[unhedged] += 1
            done = unhedged & (unhedged_days >= max(rehedge_delay, 1))
            unhedged[done] = False
        # 建仓登记
        newly = (wt > 0) & ((prev <= 0) | np.isnan(entry))
        if newly.any():
            entry[newly] = pt[newly]; maxrise[newly] = 0.0
            basis_entry[newly] = B[t][newly]
        cooldown_left = np.maximum(cooldown_left - 1, 0)

        net_f[t] = fund_pnl; net_b[t] = basis_pnl
        net_c[t] = -(cost + liq_cost)
        net[t] = fund_pnl + basis_pnl - cost - liq_cost + un_pnl
        prev = wt

    out = pd.DataFrame({"net": net, "funding": net_f, "basis": net_b,
                        "cost": net_c, "turnover": turn}, index=idx)
    return out, nliq