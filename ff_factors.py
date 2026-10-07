"""
ff_factors.py - Fama-French 风格因子组合收益构造
=================================================
用基本面因子面板（bm/size/roe/investment）构造学术因子组合收益：
  - MKT : 市场组合收益（等权或市值加权）
  - SMB : 规模因子（小盘 - 大盘）
  - HML : 价值因子（高账面市值比 - 低）
  - RMW : 盈利因子（高盈利 - 低盈利）
  - CMA : 投资因子（低投资 - 高投资）
Fama-French 经典做法：按 Size×{HML/RMW/CMA} 双维排序成 2×3 组合，取组合均值之差。

用法：
    from ff_factors import build_ff_factors
    ff = build_ff_factors(prices, panels, mktcap=None, rebalance='M')
    # ff 是 DataFrame(date × [MKT,SMB,HML,RMW,CMA])
"""

import numpy as np
import pandas as pd

from fundamentals import build_factor_panels


def market_return(prices: pd.DataFrame, mktcap: pd.DataFrame = None,
                  freq: str = "D") -> pd.Series:
    """市场组合收益：市值加权（有 mktcap）或等权。"""
    r = prices.pct_change().fillna(0.0)
    if mktcap is not None:
        w = mktcap.reindex(prices.index).fillna(0.0)
        w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
        mkt = (w * r).sum(axis=1)
    else:
        mkt = r.mean(axis=1)
    if freq.upper() in ("M", "ME"):
        mkt = (1 + mkt).resample("ME").prod() - 1
    return mkt


def _double_sort_portfolios(factor_a: pd.DataFrame, factor_b: pd.DataFrame,
                            prices: pd.DataFrame, n_bins_a: int = 2,
                            n_bins_b: int = 3,
                            mktcap: pd.DataFrame = None) -> pd.Series:
    """
    按 factor_a 分 n_bins_a 组、factor_b 分 n_bins_b 组做 2D 排序，
    返回各组合的市值加权收益（DataFrame：date × 组合标签）。
    """
    ret = prices.pct_change().fillna(0.0)
    qa = factor_a.rank(axis=1, pct=True)
    qb = factor_b.rank(axis=1, pct=True)
    ea = np.linspace(0, 1, n_bins_a + 1)   # 分箱边界
    eb = np.linspace(0, 1, n_bins_b + 1)
    out = {}
    for ia in range(1, n_bins_a + 1):
        for ib in range(1, n_bins_b + 1):
            la, ua = ea[ia - 1], ea[ia]
            lb, ub = eb[ib - 1], eb[ib]
            sel = (qa > la) & (qa <= ua) & (qb > lb) & (qb <= ub)
            # 市值加权组合收益
            if mktcap is not None:
                w = mktcap.where(sel).fillna(0.0)
                w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
                port_ret = (w * ret).sum(axis=1)
            else:
                port_ret = ret.where(sel).mean(axis=1).fillna(0.0)
            label = f"P{ia}{ib}"
            out[label] = port_ret
    return pd.DataFrame(out)


def build_ff_factors(prices: pd.DataFrame, panels: dict = None,
                     mktcap: pd.DataFrame = None, freq: str = "D",
                     size_factor: str = "size", value_factor: str = "bm",
                     prof_factor: str = "roe", inv_factor: str = "investment") -> pd.DataFrame:
    """
    构造 Fama-French 因子组合收益（日或月）。
    panels: build_factor_panels 的结果（bm/size/roe/investment 因子面板）。
    mktcap: 市值面板（可选，市值加权）。
    返回 DataFrame(date × [MKT, SMB, HML, RMW, CMA])。
    """
    if panels is None:
        panels = build_factor_panels(prices)
    size = panels[size_factor]
    bm = panels[value_factor]
    roe = panels[prof_factor]
    inv = panels[inv_factor]

    # Size×B/M -> SMB, HML
    sb = _double_sort_portfolios(size, bm, prices, 2, 3, mktcap)
    smb_small = (sb["P11"] + sb["P12"] + sb["P13"]) / 3
    smb_big = (sb["P21"] + sb["P22"] + sb["P23"]) / 3
    smb = smb_small - smb_big
    hml_low = (sb["P11"] + sb["P21"]) / 2
    hml_high = (sb["P13"] + sb["P23"]) / 2
    hml = hml_high - hml_low

    # Size×ROE -> RMW
    sr = _double_sort_portfolios(size, roe, prices, 2, 3, mktcap)
    rmw_low = (sr["P11"] + sr["P21"]) / 2
    rmw_high = (sr["P13"] + sr["P23"]) / 2
    rmw = rmw_high - rmw_low

    # Size×Investment -> CMA
    si = _double_sort_portfolios(size, inv, prices, 2, 3, mktcap)
    cma_low = (si["P11"] + si["P21"]) / 2
    cma_high = (si["P13"] + si["P23"]) / 2
    cma = cma_low - cma_high

    mkt = market_return(prices, mktcap, freq)
    ff = pd.DataFrame({"MKT": mkt, "SMB": smb, "HML": hml, "RMW": rmw, "CMA": cma})
    if freq.upper() in ("M", "ME"):
        ff = (1 + ff).resample("ME").prod() - 1
    return ff


def print_ff_summary(ff: pd.DataFrame) -> None:
    """打印各因子组合的年化收益、波动、夏普、t 统计。"""
    print("=" * 66)
    print("  Fama-French 因子组合收益（月频统计）")
    print("=" * 66)
    print(f"{'因子':>5} {'年化':>8} {'年化波动':>9} {'夏普':>7} {'均值t':>7}")
    for c in ff.columns:
        r = ff[c].dropna()
        ann = (1 + r).prod() ** (12 / max(len(r), 1)) - 1
        vol = r.std() * np.sqrt(12)
        sh = ann / vol if vol > 0 else 0
        t = r.mean() / (r.std() / np.sqrt(len(r))) if len(r) > 1 and r.std() > 0 else 0
        print(f"{c:>5} {ann:>8.2%} {vol:>9.2%} {sh:>7.2f} {t:>7.2f}")
    print("=" * 66)
