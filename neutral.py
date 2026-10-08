"""
neutral.py - BRAIN 式组合后处理原语 + 加密分组中性化
============================================================================
BRAIN 的真实流水线顺序（这是面试考点，也是本项目刻意对齐的部分）：

    表达式 → pasteurize → decay → 【中性化】 → 【截断】 → 【归一化】 → ×$20M → 持仓
                                    ↑            ↑           ↑
                                 本模块       本模块      本模块

本模块把最后三步实现为**可复用原语**，并额外提供加密市场的分组中性化：

  1. truncate_weights(w, max_weight)      单名权重上限（BRAIN 的 truncation）
  2. normalize_weights(w)                 ÷ Σ|w|（BRAIN 的 normalize）
  3. neutralize_groups(w, groups, mode)   分组中性化
        mode='demean'  组内去均值（剥离组共同因子的绝对暴露）
        mode='pair'    组内多空配对（只留组内相对排序，净暴露≈0）
        mode='rank'    组内截面排序后转权重（秩变换，抗极值）
  4. neutralize_beta(w, betas, tol)       净 Beta → 0
  5. brain_pipeline(w, ...)               按 BRAIN 顺序串起来

加密侧分组工具：
  size_buckets(adv, n)    按成交额(ADV)分档 —— 市值代理，见下方⚠️
  beta_buckets(betas, n)  按滚动 BTC-beta 分档
  SECTORS                 30 币的板块手工映射（静态，见下方⚠️）

⚠️ 两个诚实的局限（写进代码而不是埋在文档里）：
  1. **没有真实市值面板**。历史流通量数据本项目没采集，用 ADV(美元成交额)
     作市值代理。ADV 与市值高度相关但不等于市值，且**流动性本身会被策略影响**。
  2. **SECTORS 是静态手工映射**。真实板块归属随时间变化（例如 INJ 从 DeFi 转向 L1、
     ARB/OP 属于 L2 但常被归入 L1 板块）。用今天的板块映射去划分历史 =
     **轻度前视偏差**。这里的用法是"分组中性化"，影响远小于选股，
     但必须知道它存在。

用法：
    from neutral import brain_pipeline, size_buckets, neutralize_groups
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "truncate_weights", "normalize_weights", "neutralize_groups",
    "neutralize_beta", "brain_pipeline",
    "size_buckets", "beta_buckets", "SECTORS", "sector_panel",
]


# ===========================================================================
# 一、BRAIN 式后处理原语
# ===========================================================================
def truncate_weights(w: pd.DataFrame, max_weight: float = 0.10) -> pd.DataFrame:
    """
    单名权重上限（BRAIN 的 truncation）。

    对 w 的每个元素取符号保留、绝对值封顶：
        w_i ← sign(w_i) · min(|w_i|, max_weight)

    ⚠️ 截断**不保持总额**：被削掉的部分就是被削掉了。
        BRAIN 也是在截断之后才做 normalize，所以顺序必须是
        「先截断、后归一化」—— 反过来会让截断失效（归一化会重新放大）。
    """
    if max_weight is None or max_weight <= 0:
        return w.copy()
    return np.sign(w) * w.abs().clip(upper=max_weight)


def normalize_weights(w: pd.DataFrame) -> pd.DataFrame:
    """
    归一化：w ← w / Σ|w| → 总毛暴露 = 1（BRAIN 的 normalize，除以 Σ|w| 而非 Σw）。

    除以 **Σ|w|** 而不是 Σw 很关键：多空组合 Σw = 0，除以它会爆炸。
    BRAIN 用 Σ|w|，得到的组合总额固定为 book size，多空都能容纳。
    """
    gross = w.abs().sum(axis=1).replace(0.0, np.nan)
    return w.div(gross, axis=0).fillna(0.0)


def cap_and_normalize(w: pd.DataFrame, max_weight: float = 0.10,
                      iters: int = 50, tol: float = 1e-12) -> pd.DataFrame:
    """
    **硬上限**版：保证最终 |w_i| ≤ max_weight。

    ⚠️ 两个必须知道的性质（都是实测出来的，不是理论猜测）：

    性质 1 —— 「截断 → 归一化」对**等权组合**是恒等变换。
        若某日持有 n 名且权重全为 1/n，则无论 cap 取多少：
            截断:  每个 1/n → min(1/n, cap)（若 cap ≥ 1/n 完全不触发）
            归一化: 若 cap < 1/n，全体被压到 cap，Σ=n·cap，再归一化 ⇒ 每个又变回 1/n
        ⇒ 结果与 cap 无关。这就是为什么在等权 long-only 组合上
          "截断"看起来完全没生效 —— 它不是 bug，是数学事实。
        截断只有在**权重本来就不等**时才有意义（按信号强度加权、或上限比 1/n 更严）。

    性质 2 —— cap < 1/n 时，「Σ|w| = 1 且 |w_i| ≤ cap」**不可行**。
        n 名最多承担 n·cap 的毛暴露，达不到 1。
        此时本函数不假装能做到，而是**按 cap 等比缩小总暴露**（剩余留现金）：
            最终 gross = n·cap < 1，且 |w_i| ≤ cap 严格成立。
        这也解释了为什么朴素迭代会"不动点却不满足上限"：
        归一化每次都把权重重新放大回 1/n，迭代永远在同一处打转。
    """
    if max_weight is None or max_weight <= 0 or max_weight >= 1:
        return normalize_weights(w)

    out = normalize_weights(w)
    for _ in range(iters):
        prev = out
        out = normalize_weights(truncate_weights(out, max_weight))
        if float((out - prev).abs().to_numpy().max(initial=0.0)) < tol:
            break

    # 不可行时收尾：整体缩放到刚好满足上限（gross < 1，剩余现金）
    mx = out.abs().max(axis=1).replace(0.0, np.nan)
    scale = (max_weight / mx).clip(upper=1.0).fillna(1.0)
    return out.mul(scale, axis=0)


def is_feasible(gross: float, n_names: float, max_weight: float) -> bool:
    """Σ|w| = gross 且每名 ≤ max_weight 是否可行：需要 n_names · max_weight ≥ gross。"""
    return n_names * max_weight >= gross - 1e-12


def neutralize_groups(w: pd.DataFrame, groups: pd.DataFrame,
                      mode: str = "demean") -> pd.DataFrame:
    """
    分组中性化。groups 是 date×coin 的**整数分档标签**面板
    （NaN = 当日不可交易/未分组，该名权重强制清零）。

    mode='demean'：组内去均值
        w_i ← w_i − mean_{j∈组(i)} w_j
        含义：剥离"该组整体被看多/看空"的绝对暴露，只留组内相对观点。
        结果：组合净暴露在每组内为 0 ⇒ 全组合 Σw = 0（天然 dollar-neutral）。

    mode='pair'：组内多空配对（更激进）
        把组内权重按大小分成上下两半，上半 +1/2、下半 −1/2（组内等权），
        再乘回组内原有权重的绝对量级。
        含义：**只交易组内相对排序**，完全剔除组的整体方向。
        这是"组内 long-short pair"的字面实现。

    mode='rank'：组内截面秩变换到 [−1, 1] 再转权重
        含义：抗极值。原始 funding 率有厚尾，排序后不受个别极端值支配。
    """
    w = w.copy()
    g = groups.reindex_like(w)
    out = pd.DataFrame(0.0, index=w.index, columns=w.columns)
    valid = g.notna()

    if mode == "demean":
        # 逐组：减去该组当日均值（只在组内有效成员上算均值）
        for _, cols in _iter_groups(g):
            sub = w[cols].where(valid[cols])
            gm = sub.mean(axis=1)
            out[cols] = sub.sub(gm, axis=0)
        out = out.where(valid, 0.0)

    elif mode == "pair":
        for _, cols in _iter_groups(g):
            sub = w[cols].where(valid[cols])
            n = sub.notna().sum(axis=1)
            # 每日组内：按权重降序，前半 +1 后半 −1
            r = sub.rank(axis=1, ascending=False, na_option="keep")
            half = n / 2.0
            sign = pd.DataFrame(
                np.where(r.le(half, axis=0), 1.0, -1.0),
                index=sub.index, columns=sub.columns)
            sign = sign.where(sub.notna(), 0.0)
            # 组内各有符号侧等权；量级取该组当日原有权重的平均绝对值
            mag = sub.abs().sum(axis=1).div(n.replace(0, np.nan)).fillna(0.0)
            out[cols] = sign.mul(mag, axis=0)

    elif mode == "rank":
        for _, cols in _iter_groups(g):
            sub = w[cols].where(valid[cols])
            r = sub.rank(axis=1, pct=True, na_option="keep")     # [0,1]
            out[cols] = (2.0 * r - 1.0).where(sub.notna(), 0.0)

    else:
        raise ValueError("mode 必须是 demean / pair / rank，收到: %r" % mode)

    return out.fillna(0.0)


def _iter_groups(g: pd.DataFrame):
    """按列分组标签产出 (label, 列名列表)。标签取全期出现过的整数值。"""
    vals = pd.unique(g.to_numpy().ravel())
    for v in vals:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        cols = [c for c in g.columns if (g[c] == v).any()]
        if cols:
            yield int(v), cols


def neutralize_beta(w: pd.DataFrame, betas: pd.DataFrame,
                    tol: float = 0.0) -> pd.DataFrame:
    """
    净 Beta → 0：对空头腿整体缩放 k，使 Σ(w_i·β_i) ≈ 0。

    ⚠️ 对 **Delta 中性 carry 是恒等变换**：每一名都是"现货多 + 永续空"，
       价格 Beta 天然为 0，weights 里根本不含方向性暴露。
        这个函数存在是为了**验证**这一点（见 run_neutral_carry.py），
        以及给真正有方向暴露的策略（时序动量等）用。
    """
    b = betas.reindex_like(w).fillna(1.0)
    long_beta = (w.clip(lower=0) * b).sum(axis=1)
    short_beta = (-w.clip(upper=0) * b).sum(axis=1)
    k = long_beta.div(short_beta.replace(0, np.nan)).fillna(1.0).clip(0.1, 10.0)
    out = w.where(w >= 0, w.mul(k, axis=0))
    return out.fillna(0.0)


def brain_pipeline(w: pd.DataFrame, groups: pd.DataFrame = None,
                   betas: pd.DataFrame = None, group_mode: str = "demean",
                   max_weight: float = 0.10, do_beta: bool = False,
                   strict_cap: bool = False) -> pd.DataFrame:
    """
    按 BRAIN 的真实顺序执行：中性化 → 截断 → 归一化。

    ⚠️ 顺序不可换。若先归一化再截断，截断会破坏总暴露且无后续修正；
       若先截断再中性化，中性化会重新引入被截掉方向的暴露。

    strict_cap=False（默认）→ 忠实复刻 BRAIN 的「截断后归一化」，
        注意最终权重**可以超过** max_weight（见 cap_and_normalize 说明）。
    strict_cap=True → 用迭代收敛版，保证最终 |w_i| ≤ max_weight。
    """
    out = w.copy()
    if groups is not None:
        out = neutralize_groups(out, groups, mode=group_mode)
    if do_beta and betas is not None:
        out = neutralize_beta(out, betas)
    if strict_cap:
        return cap_and_normalize(out, max_weight)
    out = truncate_weights(out, max_weight)
    return normalize_weights(out)


# ===========================================================================
# 二、加密分组工具
# ===========================================================================
def size_buckets(adv: pd.DataFrame, n: int = 3,
                 min_names: int = 3) -> pd.DataFrame:
    """
    按 ADV（30日均美元成交额）横截面分 n 档，0=最小，n-1=最大。
    当日有效名数 < n*min_names 时整日返回 NaN（不分组，交给上层处理）。
    """
    a = adv.rolling(30, min_periods=5).mean()
    cnt = a.notna().sum(axis=1)
    r = a.rank(axis=1, pct=True, na_option="keep")          # [0,1]
    lab = np.ceil(r * n).clip(upper=n) - 1                  # 0..n-1
    lab = lab.where(cnt >= n * min_names)
    return lab


def beta_buckets(prices: pd.DataFrame, market: str = "BTCUSDT",
                 window: int = 90, n: int = 3,
                 min_names: int = 3) -> pd.DataFrame:
    """按滚动 BTC-beta 分 n 档。窗口用 90 日（比 60 日稳一点，加密日频噪声大）。"""
    r = prices.pct_change()
    if market not in r.columns:
        raise KeyError("市场基准 %s 不在价格面板里" % market)
    m = r[market]
    cov = r.rolling(window, min_periods=window // 2).cov(m)
    var = m.rolling(window, min_periods=window // 2).var()
    b = cov.div(var, axis=0)
    cnt = b.notna().sum(axis=1)
    rk = b.rank(axis=1, pct=True, na_option="keep")
    lab = np.ceil(rk * n).clip(upper=n) - 1
    lab = lab.where(cnt >= n * min_names)
    return lab


# 30 币的板块手工映射（⚠️ 静态：见模块头部局限 2）
SECTORS = {
    "BTCUSDT": "L1", "ETHUSDT": "L1", "BNBUSDT": "L1", "SOLUSDT": "L1",
    "ADAUSDT": "L1", "AVAXUSDT": "L1", "DOTUSDT": "L1", "TRXUSDT": "L1",
    "ATOMUSDT": "L1", "ETCUSDT": "L1", "XLMUSDT": "L1", "ALGOUSDT": "L1",
    "VETUSDT": "L1", "NEARUSDT": "L1", "APTUSDT": "L1", "SUIUSDT": "L1",
    "TIAUSDT": "L1", "SEIUSDT": "L1",
    "ARBUSDT": "L2", "OPUSDT": "L2",
    "UNIUSDT": "DeFi", "AAVEUSDT": "DeFi", "RUNEUSDT": "DeFi",
    "INJUSDT": "DeFi",
    "LINKUSDT": "Oracle",
    "FILUSDT": "Storage",
    "LTCUSDT": "Payments", "BCHUSDT": "Payments",
    "DOGEUSDT": "Meme",
}

_SECTOR_CODE = {s: i for i, s in enumerate(sorted(set(SECTORS.values())))}
_SECTOR_NAME = {v: k for k, v in _SECTOR_CODE.items()}


def sector_panel(columns, index, min_names: int = 2) -> pd.DataFrame:
    """
    date×coin 的板块标签面板（静态映射广播到所有日期）。
    当日某板块成员数 < min_names 时置 NaN（单名板块无法"组内中性化"）。
    """
    lab = pd.DataFrame(np.nan, index=index, columns=columns)
    for c in columns:
        s = SECTORS.get(c)
        if s is not None:
            lab[c] = _SECTOR_CODE[s]
    for code in _SECTOR_CODE.values():
        cols = [c for c in columns if SECTORS.get(c) and _SECTOR_CODE[SECTORS[c]] == code]
        if len(cols) < min_names:
            lab[cols] = np.nan
    return lab


if __name__ == "__main__":
    idx = pd.bdate_range("2024-01-01", periods=3)
    cols = ["A", "B", "C", "D"]
    w = pd.DataFrame([[0.4, 0.3, 0.2, 0.1]] * 3, index=idx, columns=cols)
    g = pd.DataFrame([[0, 0, 1, 1]] * 3, index=idx, columns=cols)
    print("原权重:            ", w.iloc[0].round(4).to_dict())
    print("组内去均值:        ", neutralize_groups(w, g, "demean").iloc[0].round(4).to_dict())
    print("组内配对:          ", neutralize_groups(w, g, "pair").iloc[0].round(4).to_dict())
    print("组内秩变换:        ", neutralize_groups(w, g, "rank").iloc[0].round(4).to_dict())
    print("BRAIN顺序 截断+归一:",
          brain_pipeline(w, max_weight=0.25).iloc[0].round(4).to_dict(),
          "  <- 注意 0.3125 > 0.25，上限被突破")
    print("strict_cap 硬上限: ",
          brain_pipeline(w, max_weight=0.25, strict_cap=True).iloc[0].round(4).to_dict())
    pnl = normalize_weights(neutralize_groups(w, g, "demean"))
    print("中性化后 Σw =", round(float(pnl.sum(axis=1).iloc[0]), 12),
          " (应为 0 → 天然 dollar-neutral)")

    # 性质 1：等权组合下 截断→归一化 是恒等变换
    print()
    eq = pd.DataFrame([[0.25, 0.25, 0.25, 0.25]] * 3, index=idx, columns=cols)
    print("等权组合 原权重:", eq.iloc[0].to_dict())
    for c in (0.30, 0.10, 0.05):
        print("  截断@%.2f + 归一化 -> %s  (恒等？%s)" %
              (c, brain_pipeline(eq, max_weight=c).iloc[0].round(4).to_dict(),
               brain_pipeline(eq, max_weight=c).iloc[0].round(4).tolist() ==
               eq.iloc[0].round(4).tolist()))
    # 性质 2：cap < 1/n 不可行 -> 按 cap 缩总暴露
    print()
    cap = 0.05
    print("等权 n=4, cap=0.05 < 1/4=0.25 -> 不可行。")
    print("  严格版 gross=%.4f, 单名最大=%.4f (应 ≤0.05)" %
          (float(cap_and_normalize(eq, cap).abs().sum(axis=1).iloc[0]),
           float(cap_and_normalize(eq, cap).abs().max(axis=1).iloc[0])))
