"""
factor.py - 因子挖掘与评价模块
==============================
自主挖掘 alpha 的核心。提供：
  1. 因子算子：对价格面板做横截面/时序变换（类似 WorldQuant 的 operators）
  2. 因子评价：IC、ICIR、IC 胜率、分位收益、多空价差 —— 判断一个因子有没有预测力
  3. 因子到策略：把好的因子转成目标权重，交给回测引擎

核心约定（防前视）：
  - 因子值 t 日使用 t 日及以前的数据（算子内部不得引用未来）
  - 用"未来 period 日收益"作为预测目标（forward return）
  - 评价时按 date 对齐：factor[t] 预测 forward_return[t]（t+1..t+period）

典型用法：
    prices = load_a_share_panel("data/a_share_close.csv").dropna(how="any")
    f = momentum(prices, lookback=60)                 # 定义因子
    report = evaluate_factor(f, prices, period=5)     # 看它有没有alpha
    w = factor_to_weights(f, top_pct=0.2, long_only=True)
    from backtest import portfolio_backtest; ret, to = portfolio_backtest(prices, w)
"""

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 因子算子（全部防前视：只用 t 日及以前数据）
# ---------------------------------------------------------------------------

def zscore(panel: pd.DataFrame) -> pd.DataFrame:
    """横截面 z-score：每日对全市场股票去均值、除以横截面标准差。"""
    mu = panel.mean(axis=1)
    sd = panel.std(axis=1).replace(0, np.nan)
    return panel.sub(mu, axis=0).div(sd, axis=0)


def rank(panel: pd.DataFrame) -> pd.DataFrame:
    """横截面 rank，归一化到 [0,1]（按每日排序）。"""
    return panel.rank(axis=1, pct=True)


def ts_mean(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """时序均值（对每只股票在时间维滚动）。"""
    return panel.rolling(window, min_periods=1).mean()


def ts_delta(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """时序差分：t 值 - (t-window) 值。"""
    return panel - panel.shift(window)


def ts_std(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    """时序滚动标准差。"""
    return panel.rolling(window, min_periods=2).std()


def momentum(close: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    """动量因子：过去 lookback 日收益率。"""
    return close.pct_change(lookback)


def short_term_reversal(close: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """短期反转因子：近 lookback 日涨得越多，预测未来越可能跌（取负）。"""
    return -close.pct_change(lookback)


def price_vs_ma(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """价格相对均线：close/ma - 1，衡量偏离程度。"""
    ma = close.rolling(window).mean()
    return close / ma - 1.0


def volatility_factor(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """波动率因子：过去 window 日收益的标准差。"""
    return close.pct_change().rolling(window).std()


def volume_factor(amount: pd.DataFrame, close: pd.DataFrame,
                  window: int = 20) -> pd.DataFrame:
    """量能因子：近期成交额均值相对长期均值的偏离。"""
    short = amount.rolling(window).mean()
    long = amount.rolling(3 * window).mean()
    return short / long.replace(0, np.nan) - 1.0


def combine(a: pd.DataFrame, b: pd.DataFrame, wa: float = 0.5,
            wb: float = 0.5) -> pd.DataFrame:
    """线性组合两个因子（先各自 z-score 再加权）。"""
    return zscore(a) * wa + zscore(b) * wb


# ---------------------------------------------------------------------------
# 未来收益（预测目标）
# ---------------------------------------------------------------------------

def forward_returns(close: pd.DataFrame, period: int = 5) -> pd.DataFrame:
    """未来 period 日收益：r_t = close[t+period]/close[t] - 1。对齐：t 行对应因子 t 日的预测目标。"""
    return close.shift(-period) / close - 1.0


# ---------------------------------------------------------------------------
# 因子评价
# ---------------------------------------------------------------------------

def _row_spearman(f: pd.DataFrame, r: pd.DataFrame) -> pd.Series:
    """向量化逐日横截面 Spearman 秩相关（IC）。一次 numpy 全算，避免逐日 Python 循环。"""
    f = f.replace([np.inf, -np.inf], np.nan)
    r = r.replace([np.inf, -np.inf], np.nan)
    mask = f.notna() & r.notna()
    # 横截面 rank（method average = Spearman 所需）
    fr = f.where(mask).rank(axis=1, method="average")
    rr = r.where(mask).rank(axis=1, method="average")
    a = fr.values
    b = rr.values
    n = mask.sum(axis=1).astype(float)
    a0 = np.where(mask.values, a, np.nan)
    b0 = np.where(mask.values, b, np.nan)
    mean_a = np.nanmean(a0, axis=1)
    mean_b = np.nanmean(b0, axis=1)
    ca = np.where(np.isnan(a0), 0.0, a0 - mean_a[:, None])
    cb = np.where(np.isnan(b0), 0.0, b0 - mean_b[:, None])
    cov = (ca * cb).sum(axis=1)
    var_a = (ca ** 2).sum(axis=1)
    var_b = (cb ** 2).sum(axis=1)
    denom = np.sqrt(var_a * var_b)
    corr = np.where((denom == 0) | (n < 3), np.nan, cov / denom)
    return pd.Series(corr, index=f.index)


def factor_ic(factor: pd.DataFrame, forward_ret: pd.DataFrame) -> pd.Series:
    """逐日横截面 Spearman 秩相关（IC）。值越大，因子与未来收益关系越强。"""
    return _row_spearman(factor, forward_ret)


def evaluate_factor(factor: pd.DataFrame, close: pd.DataFrame,
                    period: int = 5, n_quantiles: int = 5,
                    name: str = "factor", quiet: bool = False) -> dict:
    """
    完整评价一个因子的预测力。
    返回 dict 并打印报告：IC、ICIR、IC 胜率、分位收益、多空价差。
    """
    fwd = forward_returns(close, period)
    ic = factor_ic(factor, fwd)
    ic = ic.replace([np.inf, -np.inf], np.nan).dropna()

    ic_mean = float(ic.mean())
    ic_std = float(ic.std()) if len(ic) > 1 else 0.0
    icir = ic_mean / ic_std if ic_std > 0 else 0.0
    ic_positive = float((ic > 0).mean())

    # 分位收益：每天按因子分成 n 组，算每组平均未来收益，再对时间平均
    q = factor.rank(axis=1, method="first")
    n = len(q.columns)
    q_bins = np.linspace(0, n, n_quantiles + 1)[1:-1]
    labels = pd.qcut(q.values.ravel(), n_quantiles, labels=False).reshape(q.shape)
    qdf = pd.DataFrame(labels, index=q.index, columns=q.columns)
    quantile_ret = {}
    for g in range(n_quantiles):
        mask = (qdf == g)
        group_ret = (fwd.where(mask)).mean(axis=1).mean()
        quantile_ret[g + 1] = float(group_ret) if not np.isnan(group_ret) else 0.0

    q1 = quantile_ret[1]
    qn = quantile_ret[n_quantiles]
    spread = qn - q1  # 高因子组收益 - 低因子组收益

    report = {
        "name": name, "period": period,
        "ic_mean": ic_mean, "icir": icir, "ic_positive_ratio": ic_positive,
        "n_quantiles": n_quantiles, "quantile_returns": quantile_ret,
        "long_short_spread": spread, "n_days": int(len(ic)),
    }

    if not quiet:
        print("=" * 50)
        print(f"  Factor: {name}  (predict next {period}d)")
        print("=" * 50)
        print(f"  IC 均值     : {ic_mean:>8.4f}")
        print(f"  ICIR        : {icir:>8.4f}")
        print(f"  IC 胜率     : {ic_positive:>8.2%}")
        print(f"  样本天数    : {len(ic):>8d}")
        for g in sorted(quantile_ret):
            print(f"  分位 {g} 收益 : {quantile_ret[g]:>8.4f}")
        print(f"  多空价差    : {spread:>8.4f}")
        print("=" * 50)
    return report


def factor_to_weights(factor: pd.DataFrame, top_pct: float = 0.2,
                      long_only: bool = True, shift: int = 1) -> pd.DataFrame:
    """
    把因子转成目标权重。
    long_only=True : 做多前 top_pct，等权。
    long_only=False: 多空对冲，做多前 top_pct，做空后 top_pct。
    """
    r = factor.rank(axis=1, pct=True)
    top = r > (1.0 - top_pct)
    if long_only:
        w = top.astype(float)
        w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    else:
        bot = r < top_pct
        w = top.astype(float) - bot.astype(float)
        # 重要修正：多空组合必须按【总暴露=绝对值和】归一化。
        # 旧版按净和(sum)归一化，而多空净和≈0（如5多4空=1）→ 权重被放大成 ±1，
        # 总暴露高达 8~10 倍（隐含杠杆），所有多空回测结果都是错的。
        w = w.div(w.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    w = w.shift(shift)  # 防前视：明天才按今天的因子下单
    return w.fillna(0.0)
