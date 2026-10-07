"""
data.py - 数据模块
=================
数据无关：回测引擎只要求"日期 x 资产"的价格表（或带 OHLCV 的 DataFrame）。
本模块提供：
  1. load_csv          : 从 CSV 读行情
  2. to_close_panel    : 从 OHLCV(多头长表) 提取收盘价面板
  3. make_sample_prices: 生成示例价格（几何布朗运动 + 共同因子），供演示/测试

后续接真实数据（akshare/tushare A股、或 yfinance 美股）时，只需替换"取数"部分，
回测引擎与策略不用改。
"""

import numpy as np
import pandas as pd


def load_csv(path: str, date_col: str = "date", close_col: str = "close",
             ticker_col: str = None) -> pd.DataFrame:
    """
    从 CSV 读行情。两种格式都支持：
      - 宽表：index=日期, column=每只股票价格
      - 长表：date, [ticker,] close（ticker_col 指定则转宽表）
    返回：index=日期, columns=股票 的收盘价 DataFrame
    """
    df = pd.read_csv(path)
    if ticker_col is not None and ticker_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col])
        panel = df.pivot(index=date_col, columns=ticker_col, values=close_col)
        return panel
    # 宽表
    df = df.set_index(date_col)
    return df


def to_close_panel(ohlcv: pd.DataFrame, date_col: str = "date",
                   ticker_col: str = "ticker", close_col: str = "close") -> pd.DataFrame:
    """从长表 OHLCV 提取收盘价面板。"""
    ohlcv[date_col] = pd.to_datetime(ohlcv[date_col])
    return ohlcv.pivot(index=date_col, columns=ticker_col, values=close_col)


def make_sample_prices(n_assets: int = 20, n_days: int = 1000,
                       seed: int = 42, drift: float = 0.0003,
                       vol: float = 0.02,
                       signal: float = 0.0,
                       signal_persistence: float = 0.97) -> pd.DataFrame:
    """
    生成示例价格：几何布朗运动，资产间通过共同因子产生相关性。
    用于在没有真实数据时演示/测试回测流程。

    Parameters
    ----------
    n_assets : 资产数
    n_days   : 天数
    seed     : 随机种子（可复现）
    drift    : 日漂移（年化约 drift*252）
    vol      : 日波动率
    signal   : 植入信号的强度（0 = 纯随机游走，无任何可利用信号）
    signal_persistence : 信号自相关（越高越"持续"，动量类策略越容易捕捉）

    Notes
    -----
    ``signal > 0`` 时额外植入一个 **持续性的横截面 alpha**：
    每只资产有一个缓慢变化的特质漂移（AR(1)），它同时影响当期收益，
    因此「过去的相对强弱」可以预测「未来的相对强弱」——即存在真实动量。

    这不是"作弊"：策略层只看得到价格序列，看不到 ``alpha`` 本身。
    它的用途是**验证回测管线能否检测到已知存在的信号**——
    如果连植入的信号都测不出来，说明管线有问题。

    默认 ``signal=0.0``，保持纯随机游走，用于测试"无可利用信息时不应产生虚假 alpha"。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)

    # 共同因子 + 个股特质
    common = rng.normal(drift, vol, n_days)
    idiosyncratic = rng.normal(0.0, vol * 0.8, (n_days, n_assets))
    factor_exposure = rng.uniform(0.5, 1.2, n_assets)

    returns = common[:, None] * factor_exposure[None, :] + idiosyncratic

    if signal > 0:
        # 持续性横截面 alpha：AR(1) 过程
        alpha = np.zeros((n_days, n_assets))
        alpha[0] = rng.normal(0.0, 1.0, n_assets)
        for t in range(1, n_days):
            alpha[t] = (signal_persistence * alpha[t - 1]
                        + (1.0 - signal_persistence) * rng.normal(0.0, 1.0, n_assets))
        # 标准化到单位波动，再按 signal 缩放
        alpha = alpha / (alpha.std(axis=1, keepdims=True) + 1e-12)
        returns = returns + signal * alpha * vol

    prices = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(prices, index=dates, columns=[f"A{i+1:02d}" for i in range(n_assets)])
