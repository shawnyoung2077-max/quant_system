"""
fundamentals.py - A股基本面因子（Fama-French 风格）
====================================================
用 akshare 的季度财务指标构建基本面因子面板：
  - 价值 B/M : 每股净资产 / 股价
  - 规模 SIZE: 股东权益合计 / 每股净资产 = 总股本，再 × 股价 ≈ 市值
  - 盈利     : ROE / ROA
  - 投资     : 营收增长率 / 净利增长率
带"披露滞后"处理（年报滞后~120天、中报~60天、季报~45天），避免前视。
逐只存到 data/fundamentals/<code>.csv，断点续传。

用法：
    from fundamentals import build_factor_panels
    panels = build_factor_panels(prices, symbols=..., factors=['bm','size','roe','investment'])
"""

import os
import time

import numpy as np
import pandas as pd

DATA_DIR = "data/fundamentals"
os.makedirs(DATA_DIR, exist_ok=True)

# 报告期月份 -> 披露滞后天数（A股惯例：年报4月底前、中报8月底前、季报季后1个月）
LAG_BY_MONTH = {"03": 45, "06": 60, "09": 45, "12": 120}


def _disclosure_date(period: pd.Timestamp) -> pd.Timestamp:
    """报告期的披露日 = 期末 + 滞后。"""
    lag = LAG_BY_MONTH.get(f"{period.month:02d}", 45)
    return period + pd.Timedelta(days=lag)


def fetch_stock_fundamentals(symbol: str, retries: int = 3) -> pd.DataFrame:
    """
    拉单只股票季度财务指标，返回 DataFrame：
    index=报告期date，cols= bps/roe/roa/equity/rev_growth/profit_growth
    """
    import akshare as ak
    for _ in range(retries):
        try:
            raw = ak.stock_financial_abstract(symbol=symbol)
            if raw is None or raw.empty:
                return pd.DataFrame()
            raw = raw.set_index("指标")
            # 取报告期列（'20XXXXXX'格式）
            periods = [c for c in raw.columns if isinstance(c, str) and len(c) == 8 and c.isdigit()]
            if not periods:
                return pd.DataFrame()
            idx = pd.to_datetime(periods, format="%Y%m%d")

            def _row(sub):
                r = raw.loc[sub].astype(str) if sub in raw.index else None
                if r is None:
                    return pd.Series(np.nan, index=idx)
                return pd.to_numeric(r[periods].values, errors="coerce")

            def _pick(*subs):
                for s in subs:
                    if s in raw.index:
                        r = raw.loc[s]
                        if isinstance(r, pd.DataFrame):   # 重复指标行，取第一行
                            r = r.iloc[0]
                        return pd.to_numeric(r[periods].values, errors="coerce")
                return pd.Series(np.nan, index=idx)

            df = pd.DataFrame({
                "bps": _pick("每股净资产"),
                "roe": _pick("净资产收益率(ROE)", "摊薄净资产收益率"),
                "roa": _pick("资产收益率(ROA)", "总资产收益率"),
                "equity": _pick("股东权益合计(净资产)", "股东权益合计"),
                "rev_growth": _pick("营业总收入增长率"),
                "profit_growth": _pick("归母净利润增长率"),
            }, index=idx)
            df.index.name = "period"
            return df
        except Exception:
            time.sleep(1.0)
    return pd.DataFrame()


def cache_path(symbol: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol}.csv")


def ensure_stock_fundamentals(symbol: str, refetch: bool = False,
                              cached_only: bool = False) -> pd.DataFrame:
    """读缓存；无则抓取并缓存。cached_only=True 时若未缓存直接返回空（不联网）。"""
    p = cache_path(symbol)
    if os.path.exists(p) and not refetch:
        return pd.read_csv(p, index_col=0, parse_dates=True)
    if cached_only:
        return pd.DataFrame()
    df = fetch_stock_fundamentals(symbol)
    if not df.empty:
        os.makedirs(DATA_DIR, exist_ok=True)
        df.to_csv(p)
    return df


# ---------------------------------------------------------------------------
# 单股票 -> 每日因子序列
# ---------------------------------------------------------------------------

def _stock_factor_daily(symbol: str, price: pd.Series, fund: pd.DataFrame,
                        factor: str) -> pd.Series:
    """把季度基本面 + 披露滞后映射到每日，得到单股票因子序列（index=date）。"""
    if fund.empty or len(price) == 0:
        return pd.Series(dtype=float)
    close = price.reindex(fund.index).dropna()
    # 按披露日重新排序，作为"已知点"
    disc = pd.Series([_disclosure_date(d) for d in fund.index], index=fund.index)
    known = fund.copy()
    known["disc"] = disc
    known["close_at_report"] = close

    # 计算因子值（在报告期上）
    bps = known["bps"]; roe = known["roe"]; roa = known["roa"]
    equity = known["equity"]; revg = known["rev_growth"]; prog = known["profit_growth"]
    close_r = known["close_at_report"]

    if factor == "bm":
        val = bps / close_r.replace(0, np.nan)
    elif factor == "size":
        shares = equity / bps.replace(0, np.nan)   # 总股本
        val = np.log(shares * close_r)             # 对数市值
    elif factor == "roe":
        val = roe / 100.0
    elif factor == "roa":
        val = roa / 100.0
    elif factor == "investment":
        val = revg / 100.0
    elif factor == "profit_growth":
        val = prog / 100.0
    else:
        raise ValueError(f"unknown factor {factor}")

    known["val"] = val
    # 构建每日映射：date 上取"披露日 <= date 的最新一期的值"
    s = pd.Series(known["val"].values, index=pd.to_datetime(known["disc"].values))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    daily = pd.Series(np.nan, index=price.index)
    # 用 merge_asof：每个 date 取最近的前一个披露点
    tbl = pd.DataFrame({"disc": s.index, "val": s.values}).sort_values("disc")
    pft = pd.DataFrame({"date": price.index})
    merged = pd.merge_asof(pft, tbl, left_on="date", right_on="disc", direction="backward")
    return pd.Series(merged["val"].values, index=price.index)


# ---------------------------------------------------------------------------
# 整宇宙 -> 因子面板
# ---------------------------------------------------------------------------

def build_factor_panels(prices: pd.DataFrame, symbols=None, factors=None,
                        progress=True, cached_only: bool = False) -> dict:
    """
    构建基本面因子面板。返回 {factor: DataFrame(date x stock)}。
    symbols 缺省用 prices 的全部列；factors 缺省 ['bm','size','roe','investment']。
    cached_only=True 时只用已缓存数据（不触发网络抓取），供平台/实时使用。
    """
    symbols = symbols if symbols is not None else list(prices.columns)
    factors = factors if factors is not None else ["bm", "size", "roe", "investment"]
    out = {f: pd.DataFrame(index=prices.index, columns=symbols) for f in factors}
    for i, sym in enumerate(symbols, 1):
        fund = ensure_stock_fundamentals(sym, cached_only=cached_only)
        if fund.empty:
            continue
        price = prices[sym]
        for f in factors:
            try:
                out[f][sym] = _stock_factor_daily(sym, price, fund, f)
            except Exception:
                continue
        if progress and i % 50 == 0:
            print(f"[fund] {i}/{len(symbols)}")
    return out
