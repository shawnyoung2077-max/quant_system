"""
crypto_data.py - 加密货币行情数据模块（平台新增"加密货币量化"板块）
=====================================================================
数据源：Binance 公开 REST（免 API Key）
  GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000&startTime=...
特点：加密货币历史短 → **全量历史全部落盘**，一次抓完可永久复用。

存储：
  data/crypto/<SYMBOL>_<interval>.csv   列: time, open, high, low, close, volume
  data/crypto/_meta.json                抓取元信息（各币种各周期行数/时间范围）

用法：
    from crypto_data import fetch_all, build_panel, list_available
    fetch_all()                       # 全量抓取（首次）
    close = build_panel("1d")         # 日期×币种 收盘价面板（可直接喂给 factor/alpha_score）
"""

import os
import json
import time

import numpy as np
import pandas as pd

BASE = "https://api.binance.com"
# ⚠️ 2026 实测：api.binance.com / api1.binance.com 在本机【连接超时】（被墙），
#    但 Binance 官方公开数据镜像 data-api.binance.vision 可用（HTTP 200）。
#    两者 /api/v3/klines 的返回结构完全一致，可直接互换。
#    这里按顺序尝试，第一个成功的会被记住并复用，避免每次都等超时。
BASE_FALLBACKS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api2.binance.com",
]
_ACTIVE_BASE = None
DATA_DIR = "data/crypto"
os.makedirs(DATA_DIR, exist_ok=True)
META = os.path.join(DATA_DIR, "_meta.json")

# 主流币种 universe（USDT 计价）
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "ATOMUSDT", "ETCUSDT", "XLMUSDT", "ALGOUSDT", "VETUSDT",
    "FILUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
    "SUIUSDT", "TIAUSDT", "SEIUSDT", "RUNEUSDT", "AAVEUSDT", "UNIUSDT",
]

# 默认抓取的周期：日线 + 小时线（全量）；分钟级数据量太大，按需另抓
DEFAULT_INTERVALS = ["1d", "1h"]


def _candidates():
    """优先用上次成功的 base，其次是默认顺序。"""
    if _ACTIVE_BASE:
        return [_ACTIVE_BASE] + [b for b in BASE_FALLBACKS if b != _ACTIVE_BASE]
    return list(BASE_FALLBACKS)


def _get(path: str, params: dict, tries: int = 5):
    """
    带 base 回退的 GET。

    ⚠️ 回退逻辑的存在原因：api.binance.com 在部分地区不可达（本机实测连接超时），
       而官方的 data-api.binance.vision 可达。没有回退的话，
       整个抓取流程会以"超时"这种最没有信息量的方式失败。
    一旦某个 base 成功，就记住它（模块级 _ACTIVE_BASE），
    避免后续每次请求都先等一次超时。
    """
    import requests
    global _ACTIVE_BASE
    bases = _candidates()
    for i in range(tries):
        for b in bases:
            try:
                r = requests.get(b + path, params=params, timeout=30)
                if r.status_code == 200:
                    _ACTIVE_BASE = b
                    return r.json()
                if r.status_code in (429, 418):
                    time.sleep(2 + i * 2)
                    break                       # 限速：换下一轮重试，不换 base
                if r.status_code in (451, 403):
                    continue                    # 地域封锁：换下一个 base
                # 400 等：参数/交易对不存在（与 base 无关，直接返回）
                return {"__err__": r.status_code, "__body__": r.text[:200]}
            except Exception as e:
                last = str(e)
                continue                        # 连接失败：换下一个 base
        if i == tries - 1:
            return {"__err__": "exc", "__body__": last[:200] if 'last' in dir() else ""}
        time.sleep(1.5 + i)
    return None


def fetch_klines(symbol: str, interval: str = "1d", start_ms: int = 0,
                 verbose: bool = True) -> pd.DataFrame:
    """分页抓取某交易对某周期的**全量**K线。返回 DataFrame(time,open,high,low,close,volume)。"""
    rows = []
    cur = start_ms
    while True:
        j = _get("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                    "limit": 1000, "startTime": cur})
        if j is None:
            break
        if isinstance(j, dict) and "__err__" in j:
            if verbose:
                print(f"[crypto] {symbol} {interval} err: {j['__err__']} {j.get('__body__','')[:80]}")
            return pd.DataFrame()
        if not j:
            break
        rows.extend(j)
        if len(j) < 1000:
            break
        cur = int(j[-1][0]) + 1     # 下一批从最后一根之后开始
        time.sleep(0.25)            # 温和限速
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close",
                                     "volume", "close_time", "qav", "trades",
                                     "tbbav", "tbqav", "ignore"])
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ("open", "high", "low", "close", "volume", "qav", "trades"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["time", "open", "high", "low", "close", "volume", "qav", "trades"]]
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


def cache_path(symbol: str, interval: str) -> str:
    return os.path.join(DATA_DIR, f"{symbol}_{interval}.csv")


def save_symbol(symbol: str, interval: str, refetch: bool = False) -> int:
    """抓取并落盘；已存在且非 refetch 则跳过（增量补新数据）。返回新增行数。"""
    p = cache_path(symbol, interval)
    prev = None
    if os.path.exists(p) and not refetch:
        prev = pd.read_csv(p, parse_dates=["time"])
    start_ms = 0
    if prev is not None and len(prev):
        start_ms = int(prev["time"].max().timestamp() * 1000) - 3 * 24 * 3600 * 1000  # 回退3天防漏
    df = fetch_klines(symbol, interval, start_ms=start_ms)
    if df.empty:
        return 0 if prev is not None else -1
    if prev is not None and len(prev):
        df = pd.concat([prev, df], ignore_index=True)
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    df.to_csv(p, index=False)
    return len(df)


def fetch_all(symbols=None, intervals=None, refetch: bool = False, verbose: bool = True):
    """抓取整个 universe（全量历史）。写 _meta.json。"""
    symbols = symbols or UNIVERSE
    intervals = intervals or DEFAULT_INTERVALS
    meta = {}
    if os.path.exists(META):
        try: meta = json.load(open(META, encoding="utf-8"))
        except Exception: meta = {}
    for sym in symbols:
        for itv in intervals:
            n = save_symbol(sym, itv, refetch=refetch)
            p = cache_path(sym, itv)
            info = {"rows": int(n) if n > 0 else 0}
            if os.path.exists(p):
                d = pd.read_csv(p, parse_dates=["time"])
                if len(d):
                    info = {"rows": int(len(d)), "start": str(d["time"].min().date()),
                            "end": str(d["time"].max().date())}
            meta[f"{sym}_{itv}"] = info
            if verbose:
                print(f"[crypto] {sym:10s} {itv:3s} rows={info.get('rows')} "
                      f"{info.get('start','')}~{info.get('end','')}", flush=True)
    json.dump(meta, open(META, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return meta


def list_available(interval: str = None):
    """列出已缓存的数据文件。"""
    out = {}
    for f in sorted(os.listdir(DATA_DIR)):
        if not f.endswith(".csv") or f.startswith("_"):
            continue
        stem = f[:-4]
        parts = stem.rsplit("_", 1)
        if len(parts) != 2: continue
        sym, itv = parts
        if interval and itv != interval: continue
        out.setdefault(itv, []).append(sym)
    return out


def load_symbol(symbol: str, interval: str = "1d") -> pd.DataFrame:
    p = cache_path(symbol, interval)
    if not os.path.exists(p):
        return pd.DataFrame()
    return pd.read_csv(p, parse_dates=["time"]).set_index("time")


def build_panel(interval: str = "1d", field: str = "close", symbols=None) -> pd.DataFrame:
    """把各币种拼成 时间×币种 面板（可直接喂给 factor / alpha_score）。"""
    av = list_available(interval).get(interval, [])
    symbols = symbols or av
    cols = {}
    for sym in symbols:
        df = load_symbol(sym, interval)
        if df.empty or field not in df.columns:
            continue
        s = df[field]
        s = s[~s.index.duplicated(keep="last")]
        cols[sym] = s.rename(sym)
    if not cols:
        return pd.DataFrame()
    p = pd.concat(cols.values(), axis=1).sort_index()
    p = p.astype(float)
    return p


if __name__ == "__main__":
    fetch_all()
