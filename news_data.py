"""
news_data.py - A股 个股新闻情绪 数据模块（平台数据类别 #4）
============================================================
数据源：akshare `stock_news_em`（东方财富个股新闻，逐只近10条）。
给每条新闻打一个**粗略中文情感标签**（正/负关键词词典计数 → [-1,1]）。

⚠️ 诚实边界：
  - 逐只联网，做全宇宙(800只)很贵。这里按覆盖广度选 **流动性TOP ~15只** 建快照缓存，
    定位为"新闻情绪数据类别已可消费/展示"，不做全量历史。
  - 情感标签是词典启发式，非BERT级情绪，仅作类别验证与粗筛。

用法：
    from news_data import load_news, aggregate_sentiment
    df = load_news(refetch=False)         # cached
    agg = aggregate_sentiment(df)          # per-code 聚合
"""

import os
import time

import numpy as np
import pandas as pd

DATA_DIR = "data/news"
CACHE = os.path.join(DATA_DIR, "news.csv")

# 平台价格宇宙内的高关注标的（覆盖广度较高），默认抓取集合
DEFAULT_SYMBOLS = ["600519", "300750", "002594", "600036", "601318",
                   "000858", "601012", "600887", "601899", "600276",
                   "002415", "601398", "600030", "000001"]

_POS = ["增长", "上涨", "突破", "新高", "中标", "增持", "回购", "超预期", "盈利",
        "利好", "涨停", "合作", "获批", "扭亏", "放量", "涨价", "订单", "扩产"]
_NEG = ["下跌", "亏损", "减持", "下滑", "违规", "处罚", "立案", "退市", "利空",
        "跌停", "爆雷", "违约", "下调", "诉讼", "问询", "召回", "警示", "风险"]


def tag_sentiment(text: str) -> float:
    """词典启发式情感分 → [-1,1]；无命中返回0。"""
    if not isinstance(text, str):
        return 0.0
    p = sum(1 for w in _POS if w in text)
    n = sum(1 for w in _NEG if w in text)
    if p + n == 0:
        return 0.0
    return (p - n) / (p + n)


def fetch_stock_news(code: str, retries: int = 2) -> pd.DataFrame:
    """抓单只股票近 ~10 条新闻，打情感分。失败返回空。"""
    import akshare as ak
    for _ in range(retries):
        try:
            raw = ak.stock_news_em(symbol=code)
            if raw is None or raw.empty:
                return pd.DataFrame()
            df = pd.DataFrame({
                "code": code,
                "title": raw.get("新闻标题", ""),
                "time": raw.get("发布时间", ""),
                "source": raw.get("文章来源", ""),
                "url": raw.get("新闻链接", ""),
            })
            content = raw.get("新闻内容", "")
            txt = df["title"].astype(str) + " " + content.fillna("").astype(str)
            df["sentiment"] = txt.map(tag_sentiment).astype(float)
            return df
        except Exception as e:
            print(f"[news] {code} err: {e}")
            time.sleep(1.0)
    return pd.DataFrame()


def build_cache(symbols=None) -> pd.DataFrame:
    """逐个抓取并拼接缓存。返回合并 DataFrame。"""
    symbols = symbols or DEFAULT_SYMBOLS
    frames = []
    for i, code in enumerate(symbols, 1):
        df = fetch_stock_news(code)
        if not df.empty:
            frames.append(df)
        if i % 5 == 0:
            print(f"[news] {i}/{len(symbols)}")
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    out.to_csv(CACHE, index=False)
    return out


def load_news(refetch: bool = False, max_age_hours: int = 24,
              cached_only: bool = False) -> pd.DataFrame:
    """读缓存；无/过期则抓取。cached_only=True 时仅用已有缓存，绝不联网。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE) and not refetch:
        df = pd.read_csv(CACHE, dtype={"code": str})
        if not df.empty:
            mtime = pd.Timestamp(os.path.getmtime(CACHE), unit="s")
            if (pd.Timestamp.now() - mtime).total_seconds() < max_age_hours * 3600:
                return df
    if cached_only:
        return pd.read_csv(CACHE, dtype={"code": str}) if os.path.exists(CACHE) else pd.DataFrame()
    df = build_cache()
    if not df.empty:
        df.to_csv(CACHE, index=False)
    elif os.path.exists(CACHE):
        return pd.read_csv(CACHE, dtype={"code": str})
    return df


def aggregate_sentiment(df: pd.DataFrame) -> pd.DataFrame:
    """按 code 聚合：新闻条数 + 平均情感。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=["code", "n_news", "mean_sentiment", "pos_ratio"])
    df = df.copy()
    df["senti_bin"] = np.sign(df["sentiment"].fillna(0))
    g = df.groupby("code")
    agg = pd.DataFrame({
        "n_news": g.size(),
        "mean_sentiment": g["sentiment"].mean(),
        "pos_ratio": g["senti_bin"].apply(lambda s: (s > 0).mean()),
    }).reset_index()
    return agg
