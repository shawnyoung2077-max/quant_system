"""
robustness.py - 回测平台修正模块（防过拟合 / 真实成本 / 稳健性）
==================================================================
解决自建平台此前的四个缺陷：
  1. 只算了线性成本（佣金+滑点），没有【市场冲击成本】——对低流动性标的严重高估收益
  2. 没有【样本外(OS)】——全样本当 IS，容易过拟合
  3. 挖了很多变体但没做【多重检验校正】——挑出来的"最强"可能只是数据窥探
  4. 没有【参数邻域稳健性】——最优参数若是孤立尖峰，多半是噪声

用法：
    from robustness import (impact_cost_bps, portfolio_backtest_impact,
                            walk_forward, neighborhood_stability, deflated_sharpe)
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# 1) 市场冲击成本（平方根模型，业界标准）
# ---------------------------------------------------------------------------
def impact_cost_bps(participation: pd.Series, daily_vol: float,
                    k: float = 0.5, cap_bps: float = 500.0) -> pd.Series:
    """
    平方根冲击模型：impact = k * σ_daily * sqrt(参与率)
      participation = |成交名义额| / ADV名义额
      σ_daily       = 标的日波动率
    返回 bps（单边）。
    """
    part = participation.clip(lower=0.0)
    imp = k * daily_vol * np.sqrt(part) * 1e4
    return imp.clip(upper=cap_bps)


def portfolio_backtest_impact(prices: pd.DataFrame, target_weights: pd.DataFrame,
                              dollar_volume: pd.DataFrame = None,
                              commission_bps: float = 10.0, slippage_bps: float = 5.0,
                              k: float = 0.5, capital: float = 1e7,
                              vol_lb: int = 30, return_detail: bool = False):
    """
    带【冲击成本】的回测。相比 portfolio_backtest：
      成本 = 换手 × (佣金+滑点) + 冲击成本(随参与率平方根增长)
    dollar_volume: date×asset 的成交额面板（缺省则退化为线性成本）
    capital      : 组合资金规模（越大冲击越重），用于把权重换算成名义成交额
    """
    asset_returns = prices.pct_change().fillna(0.0)
    w = target_weights.reindex(prices.index).ffill().fillna(0.0)

    gross = (w.shift(1).fillna(0.0) * asset_returns).sum(axis=1)
    drift = w.shift(1).fillna(0.0) * (1.0 + asset_returns)
    drift = drift.div((1.0 + gross).replace(0, np.nan), axis=0).fillna(0.0)
    trade = (w - drift).abs()
    turnover = trade.sum(axis=1)

    lin = turnover * (commission_bps + slippage_bps) / 1e4

    if dollar_volume is None:
        net = gross - lin
        return (net, turnover, lin, pd.Series(0.0, index=prices.index)) if return_detail else (net, turnover)

    dv = dollar_volume.reindex(prices.index).reindex(columns=prices.columns).ffill()
    trade_notional = trade * capital
    part = trade_notional.div(dv.replace(0, np.nan))
    vol_d = asset_returns.rolling(vol_lb).std()
    imp = (impact_cost_bps(part.stack(), 0).unstack() if False else
           (k * vol_d * np.sqrt(part.clip(lower=0)) * 1e4).clip(upper=500.0))
    imp_cost = (imp.fillna(0.0) * trade).sum(axis=1) / 1e4
    net = gross - lin - imp_cost
    return (net, turnover, lin, imp_cost) if return_detail else (net, turnover)


# ---------------------------------------------------------------------------
# 2) 样本外（walk-forward / 留出法）
# ---------------------------------------------------------------------------
def walk_forward(prices: pd.DataFrame, factor_fn, split: float = 0.6,
                 ppy: int = TRADING_DAYS, top_pct: float = 0.2,
                 long_only: bool = True, hold: int = 1,
                 commission_bps: float = 10.0, slippage_bps: float = 5.0):
    """
    把样本按时间切成 IS(前 split) / OS(后 1-split)，**因子参数只在 IS 上用于选型**，
    OS 只做验证。返回 {is_sharpe, os_sharpe, is_ret, os_ret, decay}。
    factor_fn: prices -> factor 面板（已含全部参数选择）
    """
    from factor import factor_to_weights
    from backtest import portfolio_backtest
    from alpha_score import sharpe_ratio, annualized_return

    cut = int(len(prices) * split)
    res = {}
    for tag, sl in (("is", slice(0, cut)), ("os", slice(cut, None))):
        px = prices.iloc[sl]
        f = factor_fn(px).reindex(px.index).reindex(px.columns, axis=1)
        w = factor_to_weights(f, top_pct=top_pct, long_only=long_only).reindex(px.index).fillna(0.0)
        net, _ = portfolio_backtest(px, w, commission_bps=commission_bps, slippage_bps=slippage_bps)
        res[f"{tag}_sharpe"] = float(sharpe_ratio(net, ppy))
        res[f"{tag}_ret"] = float(annualized_return(net, ppy))
    res["decay"] = res["is_sharpe"] - res["os_sharpe"]
    res["pass"] = (res["os_sharpe"] > 0) and (res["decay"] < max(1.0, abs(res["is_sharpe"]) * 0.7))
    return res


# ---------------------------------------------------------------------------
# 3) 参数邻域稳健性（最优参数是尖峰还是高原？）
# ---------------------------------------------------------------------------
def neighborhood_stability(prices: pd.DataFrame, param_grid: dict, score_fn):
    """
    param_grid: {"window":[10,20,30], "top":[0.1,0.2]}
    score_fn(prices, **params) -> sharpe (float)
    返回 (best_params, best_score, neighbor_mean, neighbor_std, is_plateau)
    is_plateau=True 说明最优附近普遍不差 → 更可能是真信号而非噪声。
    """
    import itertools
    rows = []
    keys = list(param_grid.keys())
    for vals in itertools.product(*[param_grid[k] for k in keys]):
        p = dict(zip(keys, vals))
        try:
            s = float(score_fn(prices, **p))
        except Exception:
            s = np.nan
        rows.append({**p, "score": s})
    df = pd.DataFrame(rows)
    if df["score"].isna().all():
        return None, np.nan, np.nan, np.nan, False
    best = df.loc[df["score"].idxmax()]
    bp = {k: best[k] for k in keys}
    bs = float(best["score"])
    others = df[df["score"].notna()]
    nb_mean = float(others["score"].mean())
    nb_std = float(others["score"].std())
    is_plateau = (bs - nb_mean) < (1.0 * (nb_std + 1e-9)) * 2 and nb_mean > 0
    return bp, bs, nb_mean, nb_std, bool(is_plateau)


# ---------------------------------------------------------------------------
# 4) 多重检验校正（Deflated Sharpe Ratio, Bailey & Lopez de Prado）
# ---------------------------------------------------------------------------
def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurt: float = 3.0,
                    sr_std: float = None) -> dict:
    """
    在"试了 n_trials 个变体"的前提下，Sharpe 还显著吗？
    返回 {sr0(门槛), psr(概率), pass}。psr>0.95 才算通过多重检验。
    """
    from math import sqrt, log, exp, erf
    if n_obs < 30 or n_trials < 1:
        return {"sr0": None, "psr": None, "pass": False}
    e = 0.5772156649
    if sr_std is None:
        sr_std = 1.0 / sqrt(max(n_obs, 2))
    sr0 = sr_std * ((1 - e) * _norm_ppf(1 - 1.0 / n_trials) +
                    e * _norm_ppf(1 - 1.0 / (n_trials * exp(1))))
    denom = sqrt(max(1e-12, 1 - skew * sharpe + (kurt - 1) / 4.0 * sharpe ** 2))
    z = (sharpe - sr0) * sqrt(max(n_obs - 1, 1)) / denom
    psr = 0.5 * (1 + erf(z / sqrt(2)))
    return {"sr0": float(sr0), "psr": float(psr), "pass": bool(psr > 0.95)}


def _norm_ppf(p: float) -> float:
    """标准正态分位（Acklam 近似）。"""
    if p <= 0: return -np.inf
    if p >= 1: return np.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = np.sqrt(-2 * np.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = np.sqrt(-2 * np.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5; r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
