"""
stats.py - 学术统计检验模块（论文复现核心）
==========================================
提供论文级显著性检验：
  1. Newey-West HAC 标准误 / t 统计（处理自相关）
  2. Fama-MacBeth 两步回归（横截面因子检验，业界标准）
  3. 时序回归 + FF alpha（策略超额收益是否显著）
  4. GRS 检验（一组 alpha 是否联合为零）

用法：
    from stats import fama_macbeth, ff_alpha
    fm = fama_macbeth(factor, forward_ret)   # 因子是否显著
    a  = ff_alpha(strategy_ret, market_ret)  # 策略相对市场的超额是否显著
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Newey-West HAC
# ---------------------------------------------------------------------------

def _nw_lags(T: int) -> int:
    """Newey-West 建议滞后数：floor(4*(T/100)^(2/9))。"""
    return max(1, int(4 * (T / 100.0) ** (2 / 9)))


def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int = None):
    """
    OLS + Newey-West HAC 标准误。
    y: (T,) 因变量；X: (T, k) 自变量（含常数项则 k 含截距）。
    返回 (beta, nw_se, nw_tstat)。
    """
    T, k = X.shape
    if lags is None:
        lags = _nw_lags(T)
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ (X.T @ y)
    e = y - X @ beta
    # 矩向量 u_t = e_t * X_t
    U = e[:, None] * X
    S0 = U.T @ U / T
    # 自协方差加权和（HAC 长程协方差 Σ̂）
    S = S0.copy()
    for j in range(1, lags + 1):
        w = 1.0 - j / (lags + 1.0)
        Gj = (U[j:, :].T @ U[:-j, :]) / T
        S += w * (Gj + Gj.T)
    # var(β) = (X'X/T)^{-1} S (X'X/T)^{-1} / T = T * XtX_inv S XtX_inv
    cov = T * XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.diag(cov))
    tstat = beta / np.where(se == 0, np.nan, se)
    return beta, se, tstat


# ---------------------------------------------------------------------------
# 时序回归（含 NW t 统计），FF alpha
# ---------------------------------------------------------------------------

def time_series_regression(y: pd.Series, factors: pd.DataFrame,
                           add_const: bool = True, nw: bool = True) -> dict:
    """
    时序回归 y = alpha + Σ beta_j * factor_j + e。
    factors: 各因子组合收益。返回 alpha/beta、t 统计、R2。
    """
    f = factors.dropna()
    y = y.reindex(f.index).dropna()
    common = f.index.intersection(y.index)
    f = f.loc[common]
    y = y.loc[common]
    X = np.column_stack([np.ones(len(common))] + [f[c].values for c in f.columns]) \
        if add_const else f.values
    names = (["alpha"] + list(f.columns)) if add_const else list(f.columns)
    beta, se, tstat = newey_west_ols(y.values, X) if nw else _plain_ols(y.values, X)
    resid = y.values - X @ beta
    ss_res = np.sum(resid ** 2); ss_tot = np.sum((y.values - y.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"params": dict(zip(names, beta)), "tstats": dict(zip(names, tstat)),
            "se": dict(zip(names, se)), "r2": r2, "resid": pd.Series(resid, index=y.index)}


def _plain_ols(y, X):
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ beta
    dof = len(y) - X.shape[1]
    sigma2 = (e @ e) / max(dof, 1)
    se = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    return beta, se, beta / np.where(se == 0, np.nan, se)


def ff_alpha(strategy_ret: pd.Series, market_ret: pd.Series,
             smb: pd.Series = None, hml: pd.Series = None,
             rmw: pd.Series = None, cma: pd.Series = None,
             nw: bool = True) -> dict:
    """
    FF alpha：把策略收益对市场(SMB/HML/RMW/CMA 可选)做时序回归，截距即 alpha。
    返回 alpha、beta、t 统计、R2，以及是否显著（|t|>2）。
    """
    factors = {"MKT": market_ret}
    if smb is not None: factors["SMB"] = smb
    if hml is not None: factors["HML"] = hml
    if rmw is not None: factors["RMW"] = rmw
    if cma is not None: factors["CMA"] = cma
    ff = pd.DataFrame(factors)
    r = time_series_regression(strategy_ret, ff, nw=nw)
    alpha_t = r["tstats"]["alpha"]
    return {**r, "alpha": r["params"]["alpha"],
            "alpha_t": alpha_t, "significant": abs(alpha_t) > 2.0}


# ---------------------------------------------------------------------------
# Fama-MacBeth 横截面回归
# ---------------------------------------------------------------------------

def fama_macbeth(factor: pd.DataFrame, forward_ret: pd.DataFrame,
                 lags: int = None) -> dict:
    """
    Fama-MacBeth：每期做横截面回归 r_i = a + λ * factor_i，得每期 λ_t，
    然后 λ_t 的时间序列用 Newey-West 求显著性。

    factor: (T, N) 因子暴露（t 日已知，预测未来）
    forward_ret: (T, N) 未来收益（与 factor 同日对齐，即 t 日的因子预测 t+1..t+p 收益）
    返回：lambda 均值、NW t 统计、每期 lambda 序列、样本期数。
    """
    cols = list(factor.columns)
    lambdas = []
    for t in factor.index:
        f = factor.loc[t]
        r = forward_ret.loc[t]
        m = f.notna() & r.notna() & f.replace([np.inf, -np.inf], np.nan).notna()
        if m.sum() < 5:
            continue
        fv = f[m].values
        rv = r[m].values
        X = np.column_stack([np.ones(len(fv)), fv])
        try:
            beta = np.linalg.lstsq(X, rv, rcond=None)[0]
        except Exception:
            continue
        lambdas.append(beta[1])  # 因子斜率 λ
    lam = np.array(lambdas)
    T = len(lam)
    if T == 0:
        return {"ok": False, "error": "no valid periods"}
    mean_lam = lam.mean()
    # NW 标准误
    Xc = np.ones((T, 1))
    _, se, tstat = newey_west_ols(lam, Xc, lags=lags)
    pval = 2 * (1 - _tdist_cdf(abs(tstat[0]), T - 1))
    return {"ok": True, "lambda_mean": float(mean_lam), "nw_tstat": float(tstat[0]),
            "nw_se": float(se[0]), "p_value": float(pval), "n_periods": T,
            "lambda_series": pd.Series(lam, index=factor.index[:T])}


def _tdist_cdf(z, dof):
    """标准 t 分布 CDF。优先 scipy，否则正态近似。"""
    try:
        from scipy import stats
        return float(stats.t.cdf(z, dof))
    except Exception:
        import math
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))


# ---------------------------------------------------------------------------
# GRS 检验（一组策略 alpha 是否联合为零）
# ---------------------------------------------------------------------------

def grs_test(alphas: np.ndarray, cov_alpha: np.ndarray,
             n_assets: int, T: int, factor_returns: pd.DataFrame) -> dict:
    """
    Gibbons-Ross-Shanken：检验 n 个资产/策略的 alpha 是否联合为 0。
    GRS = (T-n-k)/n * alpha' Σ^-1 alpha / (1 + μ'Ω^-1μ)，服从 F(n, T-n-k)。
    """
    n = n_assets
    k = factor_returns.shape[1]
    mu = factor_returns.mean().values
    Omega = np.cov(factor_returns, rowvar=False) + np.eye(k) * 1e-10
    try:
        cov_inv = np.linalg.inv(cov_alpha)
        stat = alphas @ cov_inv @ alphas
        f_stat = (T - n - k) / n * stat / (1 + mu @ np.linalg.inv(Omega) @ mu)
    except np.linalg.LinAlgError:
        return {"grs_stat": None, "p_value": None, "error": "singular covariance"}
    try:
        from scipy import stats
        p = float(1 - stats.f.cdf(f_stat, n, T - n - k))
    except Exception:
        p = None
    return {"grs_stat": float(f_stat), "p_value": p, "df1": n, "df2": T - n - k}
