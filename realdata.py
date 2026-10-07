"""
realdata.py - 真实 A股数据连接（akshare）
========================================
把真实 A股行情接进回测框架，替换合成数据。数据无关设计下，
引擎与策略不用改，只需用本模块生成"日期 x 股票"的收盘价面板。

数据源（akshare）：
  主用腾讯 stock_zh_a_hist_tx（国内直连、抗风控、快）；
  东财 stock_zh_a_hist 作兜底（有时被临时限流）。

用法：
    from realdata import fetch_a_share_panel, load_a_share_panel
    panel = fetch_a_share_panel(symbols=DEFAULT_SYMBOLS, start='20220101', save_path='data/a_share_close.csv')
    panel = load_a_share_panel('data/a_share_close.csv')
"""

import time
import os

import numpy as np
import pandas as pd

# 抑制 akshare 里 tqdm 进度条刷屏（跑到 stderr，不需要）
try:
    import tqdm as _tqdm
    _orig = _tqdm.tqdm

    def _quiet(*a, **k):
        k["disable"] = True
        return _orig(*a, **k)
    _tqdm.tqdm = _quiet
except Exception:
    pass


def to_tx_symbol(code: str) -> str:
    """6 位代码 -> 腾讯带交易所前缀格式。6xxxxx=sh，0/3xxxxx=sz，8/4xxxxx=bj。"""
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


# 默认一批高流动性 A股（沪深大盘蓝筹，覆盖多行业），用于演示
DEFAULT_SYMBOLS = [
    "600519", "601318", "600036", "601398", "600030", "600276",  # 茅台/平安/招行/工行/中信/恒瑞
    "000858", "000651", "000333", "002594", "300750", "300059",  # 五粮液/格力/美的/比亚迪/宁德/东财
    "601888", "600900", "600887", "601012", "600585", "601166",  # 中免/长江电力/伊利/隆基/海螺/兴业
    "000002", "000001", "601857", "600028", "601988", "600048",  # 万科/平安银行/中石油/中石化/中行/保利
    "600309", "601088", "002415", "300015", "000725", "002230",  # 万华/中国神华/海康/爱尔/京东方/科大讯飞
    "600031", "601668", "600690", "000568", "002027", "300124",  # 三一/中国建筑/海尔/泸州老窖/分众/汇川
]


def fetch_a_share_daily(symbol: str, start: str = "20230101", end: str = None,
                        adjust: str = "qfq", retries: int = 4) -> pd.DataFrame:
    """
    拉单只 A股日线（前复权）。返回含 date/open/high/low/close 的 DataFrame。
    symbol: 6 位代码，如 '600519'。
    数据源优先级：新浪(一次全量,快) > 腾讯(分页) > 东财(兜底)。
    """
    import akshare as ak
    end = end or time.strftime("%Y%m%d")
    tx = to_tx_symbol(symbol)
    for attempt in range(1, retries + 1):
        # 新浪源（主，一次返回全量历史）
        try:
            raw = ak.stock_zh_a_daily(symbol=tx, start_date=start,
                                      end_date=end, adjust=adjust)
            if raw is not None and not raw.empty:
                df = raw[["date", "open", "high", "low", "close"]].copy()
                df["volume"] = raw["volume"] if "volume" in raw.columns else np.nan
                df["date"] = pd.to_datetime(df["date"])
                df["symbol"] = symbol
                return df[["symbol", "date", "open", "high", "low", "close", "volume"]]
        except Exception:
            pass
        # 腾讯源（备）
        try:
            raw = ak.stock_zh_a_hist_tx(symbol=tx, start_date=start,
                                        end_date=end, adjust=adjust)
            if raw is not None and not raw.empty:
                df = raw.copy()
                df["volume"] = df["volume"] if "volume" in df.columns else np.nan
                df["date"] = pd.to_datetime(df["date"])
                df["symbol"] = symbol
                return df[["symbol", "date", "open", "high", "low", "close", "volume"]]
        except Exception:
            pass
        # 东财源（兜底）
        try:
            raw = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=start, end_date=end, adjust=adjust)
            if raw is None or raw.empty:
                return pd.DataFrame()
            df = raw.rename(columns={
                "日期": "date", "股票代码": "code",
                "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
                "成交量": "volume", "成交额": "amount",
            })
            df["date"] = pd.to_datetime(df["date"])
            df["symbol"] = symbol
            return df[["symbol", "date", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            if attempt < retries:
                time.sleep(1.0 * attempt)
                continue
            print(f"[warn] {symbol} failed after {retries} tries: {type(e).__name__}")
            return pd.DataFrame()


def fetch_a_share_panel(symbols=None, start: str = "20230101", end: str = None,
                        adjust: str = "qfq", pause: float = 0.3,
                        save_path: str = None) -> pd.DataFrame:
    """
    抓取多只 A股日线，合并成"日期 x 股票"的收盘价面板。
    symbols: 股票代码列表，默认 DEFAULT_SYMBOLS。
    save_path: 若给则存成 CSV（宽表）。
    """
    symbols = symbols or DEFAULT_SYMBOLS
    frames = []
    for i, sym in enumerate(symbols, 1):
        df = fetch_a_share_daily(sym, start=start, end=end, adjust=adjust)
        if not df.empty:
            frames.append(df.set_index("date")["close"].rename(sym))
        if i % 10 == 0:
            print(f"[fetch] {i}/{len(symbols)}")
        time.sleep(pause)  # 控制频率，避免被限流
    if not frames:
        raise RuntimeError("no A-share data fetched")
    panel = pd.concat(frames, axis=1)
    panel = panel.sort_index()
    panel = panel.dropna(axis=1, how="all")
    if save_path:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        panel.to_csv(save_path)
        print(f"[save] {panel.shape[1]} stocks x {panel.shape[0]} days -> {save_path}")
    return panel


def load_a_share_panel(path: str) -> pd.DataFrame:
    """从宽表 CSV 读回面板（index=日期, columns=股票代码）。"""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df
