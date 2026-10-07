"""
research.py - 学术因子研究工具（论文复现）
==========================================
在 factor / stats 之上，补齐论文级研究步骤：
  1. 因子预处理：winsorize、行业/市场中性化、z-score
  2. 组合构建：市值加权、多空、按分位
  3. 因子显著性：Fama-MacBeth + Newey-West t 统计
  4. FF alpha：策略相对市场（及可选 SMB/HML...）的超额是否显著
  5. 市值/价值/盈利/投资 等风格因子打分（若提供市值/基本面）

用法见各函数 docstring 与 README。
"""

import numpy as np
import pandas as pd

from stats import fama_macbeth, ff_alpha, time_series_regression, grs_test


# ---------------------------------------------------------------------------
# 因子预处理
# ---------------------------------------------------------------------------

def winsorize(factor: pd.DataFrame, lower=0.01, upper=0.99) -> pd.DataFrame:
    """截面 winsorize：每天把因子值压缩到分位数 [lower, upper] 之间。"""
    lo = factor.quantile(lower, axis=1)
    hi = factor.quantile(upper, axis=1)
    return factor.clip(lower=lo, upper=hi, axis=0)


def cross_section_zscore(factor: pd.DataFrame) -> pd.DataFrame:
    """截面 z-score（去均值/除标准差）。"""
    mu = factor.mean(axis=1)
    sd = factor.std(axis=1).replace(0, np.nan)
    return factor.sub(mu, axis=0).div(sd, axis=0)


def neutralize(factor: pd.DataFrame, prices: pd.DataFrame,
               market: bool = True, sectors: pd.Series = None) -> pd.DataFrame:
    """
    中性化：从因子中剥离市场/行业共同暴露。
    - market=True：每天对因子做截面回归取残差（去市场风格）
    - sectors：若提供 股票->行业，对每个行业哑变量一起回归，得行业中性
    返回中性化后的残差因子。
    """
    out = factor.copy()
    for t in factor.index:
        f = factor.loc[t].dropna()
        if len(f) < 5:
            continue
        X = [np.ones(len(f))]
        names = ["const"]
        if market:
            # 用当日全市场等权收益做解释变量
            mkt = prices.pct_change().fillna(0).mean(axis=1).loc[t]
            X.append(np.full(len(f), mkt)); names.append("mkt")
        if sectors is not None:
            s = sectors.reindex(f.index)
            for sec in s.dropna().unique():
                d = (s == sec).astype(float)
                X.append(d.values); names.append(f"sect_{sec}")
        X = np.column_stack(X)
        y = f.values
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            resid = y - X @ beta
            out.loc[t, f.index] = resid
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# 组合构建（市值加权）
# ---------------------------------------------------------------------------

def build_portfolio(factor: pd.DataFrame, prices: pd.DataFrame,
                    mktcap: pd.DataFrame = None, n_quantiles: int = 5,
                    quantile: int = 5, long_only: bool = True,
                    shift: int = 1) -> pd.DataFrame:
    """
    按因子分位构建组合权重。
    - n_quantiles 分位数，quantile 取第几组（5=最高）。
    - 权重：等权或市值加权（mktcap 提供时）。
    - 多头则只做多该组；空头可再取低位组（long_only=False 时自动多空）。
    """
    q = factor.rank(axis=1, method="first")
    n = len(q.columns)
    edges = np.linspace(0, n, n_quantiles + 1)[1:-1]
    bins = pd.qcut(q.values.ravel(), n_quantiles, labels=False).reshape(q.shape)
    qdf = pd.DataFrame(bins, index=q.index, columns=q.columns)

    sel = (qdf == (quantile - 1))
    w = sel.astype(float)
    if mktcap is not None:
        mc = mktcap.reindex(factor.index).where(sel).fillna(0.0)
        w = mc
    else:
        w = w
    if long_only:
        pass
    else:
        bot = (qdf == 0)
        wb = bot.astype(float)
        if mktcap is not None:
            wb = mktcap.reindex(factor.index).where(bot).fillna(0.0)
        w = w - wb
    # 归一化多头
    w = w.div(w.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    w = w.shift(shift)  # 防前视
    return w.fillna(0.0)


# ---------------------------------------------------------------------------
# 因子显著性报告
# ---------------------------------------------------------------------------

def factor_significance(factor: pd.DataFrame, prices: pd.DataFrame,
                        period: int = 5, lags: int = None) -> dict:
    """Fama-MacBeth + Newey-West 检验因子是否显著。"""
    from factor import forward_returns
    fwd = forward_returns(prices, period)
    # 用经 winsorize + zscore 的因子
    f = cross_section_zscore(winsorize(factor))
    return fama_macbeth(f, fwd, lags=lags)


def alpha_report(strategy_ret: pd.Series, market_ret: pd.Series,
                 smb=None, hml=None, rmw=None, cma=None) -> dict:
    """策略相对市场（FF 因子可选）的 alpha 显著性报告。"""
    r = ff_alpha(strategy_ret, market_ret, smb, hml, rmw, cma)
    return {
        "alpha": r["alpha"], "alpha_t": r["alpha_t"],
        "annualized_alpha": float((1 + r["alpha"]) ** 252 - 1) if r["alpha"] > -1 else 0,
        "beta_mkt": r["params"].get("MKT"), "r2": r["r2"],
        "significant": r["significant"],
    }


def print_fm_report(res: dict, name: str = "factor") -> None:
    """打印 Fama-MacBeth 显著性报告。"""
    if not res.get("ok"):
        print(f"[{name}] FM 失败: {res.get('error')}")
        return
    print("=" * 52)
    print(f"  Fama-MacBeth: {name}")
    print("=" * 52)
    print(f"  lambda 均值  : {res['lambda_mean']:>10.5f}")
    print(f"  NW t 统计    : {res['nw_tstat']:>10.3f}")
    print(f"  p 值         : {res['p_value']:>10.4f}")
    print(f"  显著(p<0.05) : {'是' if res['p_value'] < 0.05 else '否'}")
    print(f"  样本期数     : {res['n_periods']:>10d}")
    print("=" * 52)


def print_alpha_report(r: dict, name: str = "strategy") -> None:
    print("=" * 52)
    print(f"  FF alpha: {name}")
    print("=" * 52)
    print(f"  月度 alpha    : {r['alpha']:>10.5f}")
    print(f"  alpha t 统计  : {r['alpha_t']:>10.3f}")
    print(f"  市场 beta     : {r['beta_mkt']:>10.3f}")
    print(f"  R2            : {r['r2']:>10.3f}")
    print(f"  显著(|t|>2)   : {'是' if r['significant'] else '否'}")
    print("=" * 52)
