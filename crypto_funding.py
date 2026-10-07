"""
crypto_funding.py - 加密永续合约衍生数据（资金费率 / 持仓量 / 多空比）
=====================================================================
数据源：Binance USDT-M 合约公开接口（免 Key）
  /fapi/v1/fundingRate                     资金费率历史（每 8h 一次）
  /futures/data/openInterestHist           持仓量历史
  /futures/data/globalLongShortAccountRatio 全市场多空账户比

用途：资金费率是最经典的加密 alpha 来源——费率高=多头拥挤→反向；
      同时也是 carry 策略的核心变量。

存储：data/crypto/funding/<SYMBOL>.csv   列: time, fundingRate
      data/crypto/oi/<SYMBOL>.csv        列: time, openInterest, oiValue
"""

import os
import time

import numpy as np
import pandas as pd

FAPI = "https://fapi.binance.com"
DEFAULT_START_MS = 1546300800000      # 2019-01-01，Binance 永续早期起点
FDIR = os.path.join("data", "crypto", "funding")
ODIR = os.path.join("data", "crypto", "oi")
os.makedirs(FDIR, exist_ok=True)
os.makedirs(ODIR, exist_ok=True)


def _get(path, params, tries=5):
    import requests
    for i in range(tries):
        try:
            r = requests.get(FAPI + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(2 + 2 * i); continue
            return {"__err__": r.status_code, "__body__": r.text[:200]}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": "exc", "__body__": str(e)[:160]}
            time.sleep(1.5 + i)
    return None


def fetch_funding(symbol: str, start_ms: int = 0, verbose: bool = True) -> pd.DataFrame:
    """全量抓取某合约的资金费率历史（8h 一次）。

    注意：Binance 在 startTime=0 时会**忽略该参数并返回最新 N 条**，
    因此必须传一个真实的历史起点，再向前翻页。默认 2019-01-01。
    """
    if not start_ms:
        start_ms = DEFAULT_START_MS
    rows, cur = [], start_ms
    while True:
        j = _get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1000, "startTime": cur})
        if j is None:
            break
        if isinstance(j, dict) and "__err__" in j:
            if verbose: print(f"[fund] {symbol} err {j['__err__']} {j.get('__body__','')[:70]}")
            return pd.DataFrame()
        if not j:
            break
        rows.extend(j)
        if len(j) < 1000:
            break
        cur = int(j[-1]["fundingTime"]) + 1
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df = df[["time", "fundingRate"]].drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


def save_funding(symbol: str, refetch: bool = False) -> int:
    p = os.path.join(FDIR, f"{symbol}.csv")
    prev = pd.read_csv(p, parse_dates=["time"]) if (os.path.exists(p) and not refetch) else None
    start = 0
    if prev is not None and len(prev):
        start = int(prev["time"].max().timestamp() * 1000) - 3 * 24 * 3600 * 1000
    df = fetch_funding(symbol, start_ms=start)
    if df.empty:
        return 0 if prev is not None else -1
    if prev is not None and len(prev):
        df = pd.concat([prev, df], ignore_index=True).drop_duplicates("time").sort_values("time")
    df.to_csv(p, index=False)
    return len(df)


def fetch_oi(symbol: str, period: str = "1d", limit: int = 500, verbose: bool = True) -> pd.DataFrame:
    """抓持仓量历史（接口最多回溯 30 天，只能拿到近期）。period: 5m/15m/1h/4h/1d"""
    j = _get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})
    if isinstance(j, dict) and "__err__" in j:
        if verbose: print(f"[oi] {symbol} err {j['__err__']}")
        return pd.DataFrame()
    if not j:
        return pd.DataFrame()
    df = pd.DataFrame(j)
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms")
    for c in ("sumOpenInterest", "sumOpenInterestValue"):
        if c in df.columns: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["time", "sumOpenInterest", "sumOpenInterestValue"]].sort_values("time")


def fetch_all_funding(symbols=None, verbose: bool = True):
    from crypto_data import UNIVERSE
    symbols = symbols or UNIVERSE
    out = {}
    for s in symbols:
        n = save_funding(s)
        p = os.path.join(FDIR, f"{s}.csv")
        info = {"rows": int(n) if n > 0 else 0}
        if os.path.exists(p):
            d = pd.read_csv(p, parse_dates=["time"])
            if len(d):
                info = {"rows": int(len(d)), "start": str(d["time"].min().date()),
                        "end": str(d["time"].max().date())}
        out[s] = info
        if verbose:
            print(f"[fund] {s:10s} rows={info.get('rows')} {info.get('start','')}~{info.get('end','')}", flush=True)
    return out


def funding_daily(symbols=None) -> pd.DataFrame:
    """把 8h 资金费率聚合成【日度】面板：当日累计资金费率（date × symbol）。"""
    from crypto_data import UNIVERSE
    symbols = symbols or UNIVERSE
    cols = {}
    for s in symbols:
        p = os.path.join(FDIR, f"{s}.csv")
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p, parse_dates=["time"]).set_index("time")["fundingRate"]
        daily = d.resample("1D").sum()          # 当日累计（一天3次）
        cols[s] = daily.rename(s)
    if not cols:
        return pd.DataFrame()
    return pd.concat(cols.values(), axis=1).sort_index()


if __name__ == "__main__":
    print("== 测试 BTC 资金费率 ==")
    n = save_funding("BTCUSDT", refetch=True)
    d = pd.read_csv(os.path.join(FDIR, "BTCUSDT.csv"), parse_dates=["time"])
    print("rows", len(d), d["time"].min(), "->", d["time"].max())
    print(d.tail(3).to_string(index=False))
