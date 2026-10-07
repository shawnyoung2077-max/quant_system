"""
analyst_data.py - A股 分析师一致预期 数据模块（平台数据类别 #3）
================================================================
数据源：东方财富"业绩预测"（akshare `stock_profit_forecast_em`）。
字段：研报数(覆盖广度)、机构买入/增持评级计数、前瞻预测EPS(2025-2028)。

⚠️ 诚实边界：akshare 此接口给的是"当前时点"的全市场盈利预测**截面**，
**不是**逐股的历史时间序列。因此本模块定位为：
  - 确认并缓存该数据类别（中国 A股分析师一致预期可用）。
  - 提供"当前覆盖/共识"快照查询，供选股/榜单/数据看板用。
真正的"历史一致预期回测"需要逐股 ths 预测序列（慢），不在本快照内。

用法：
    from analyst_data import fetch_consensus, load_consensus
    df = load_consensus(refetch=False)   # cached DataFrame
"""

import os
import time

import numpy as np
import pandas as pd

DATA_DIR = "data/analyst"
CACHE = os.path.join(DATA_DIR, "consensus.csv")

# 东财列 -> 平台标准化列
_COL_MAP = {
    "代码": "code",
    "名称": "name",
    "研报数": "n_report",                      # 覆盖广度(近6月研报数)
    "机构投资评级(近六个月)-买入": "n_buy",
    "机构投资评级(近六个月)-增持": "n_accum",   # 增持
    "机构投资评级(近六个月)-中性": "n_neutral",
    "机构投资评级(近六个月)-减持": "n_reduce",
    "机构投资评级(近六个月)-卖出": "n_sell",
}
# 前瞻预测EPS 各财年列（按列顺序取：年份越近越靠前）
_EPS_COLS = ["2025预测每股收益", "2026预测每股收益", "2027预测每股收益", "2028预测每股收益"]


def fetch_consensus(retries: int = 3) -> pd.DataFrame:
    """抓取东财业绩预测全市场截面并标准化。空失败返回空 DataFrame。"""
    import akshare as ak
    for _ in range(retries):
        try:
            raw = ak.stock_profit_forecast_em()
            if raw is None or raw.empty:
                return pd.DataFrame()
            out = pd.DataFrame()
            for src, dst in _COL_MAP.items():
                if src in raw.columns:
                    out[dst] = pd.to_numeric(raw[src], errors="coerce") \
                        if dst not in ("code", "name") else raw[src].astype(str)
            # 前瞻EPS：取"最早未结束财年"(>= 当前年) 的一致性预测，作前瞻盈利收益率。
            # 已结束财年(如当前为2026时2025)是已实现值，不能当"预测"。无则回退最大财年。
            cur = pd.Timestamp.now().year
            avail = {int(c[:4]): pd.to_numeric(raw[c], errors="coerce") for c in _EPS_COLS if c in raw.columns}
            fwd = None
            fwd_year = None
            fwd_years = sorted(y for y in avail if y >= cur)
            for y in (fwd_years if fwd_years else sorted(avail, reverse=True)[:1]):
                col = avail[y]
                if col.notna().any():
                    fwd, fwd_year = col, y
                    break
            if fwd is None:      # 兜底：全部年份空（理论不发生）
                fwd = pd.Series(np.nan, index=raw.index)
                fwd_year = cur
            out["fwd_eps"] = fwd
            out["fwd_year"] = fwd_year
            out = out.dropna(subset=["code"])
            out = out[out["code"].str.fullmatch(r"\d{6}")]
            out["code"] = out["code"].str.zfill(6)
            # 买入评级占比（buy + 0.5*accum 除以总评级，防分母0）
            tot = out[["n_buy", "n_accum", "n_neutral", "n_reduce", "n_sell"]].sum(axis=1)
            out["buy_ratio"] = (out["n_buy"] + 0.5 * out["n_accum"].fillna(0)) / tot.replace(0, np.nan)
            out["fetched"] = pd.Timestamp.now()
            return out
        except Exception as e:
            print(f"[analyst] fetch err: {e}")
            time.sleep(2.0)
    return pd.DataFrame()


def load_consensus(refetch: bool = False, max_age_days: int = 7) -> pd.DataFrame:
    """
    读缓存（缺省 7 天内的快照）；无/过期则联网抓取并缓存。
    cached_only 语义由调用方控制：传 max_age_days=inf 则只要缓存存在就不联网。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    fresh = False
    if os.path.exists(CACHE) and not refetch:
        df = pd.read_csv(CACHE, dtype={"code": str})
        if not df.empty and "fetched" in df.columns:
            age = (pd.Timestamp.now() - pd.to_datetime(df["fetched"].iloc[0])).days
            fresh = age <= max_age_days
        if fresh:
            return df
    if not fresh and max_age_days < 10**9:
        df = fetch_consensus()
        if not df.empty:
            df.to_csv(CACHE, index=False)
            return df
    if os.path.exists(CACHE):
        return pd.read_csv(CACHE, dtype={"code": str})
    return pd.DataFrame()


def forward_earnings_yield(consensus: pd.DataFrame,
                           close_latest: pd.Series) -> pd.Series:
    """
    前瞻盈利收益率 = 前瞻每股收益 / 最新收盘。仅用于当前截面（无历史）。
    返回 index=code 的 Series，剔除无共识或无量股票。
    """
    if consensus is None or consensus.empty:
        return pd.Series(dtype=float)
    df = consensus.set_index("code")[["fwd_eps"]]
    c = close_latest.astype(float)
    df = df.join(c.rename("close"), how="inner")
    df["fwd_yield"] = df["fwd_eps"] / df["close"].replace(0, np.nan)
    return df["fwd_yield"].dropna()
