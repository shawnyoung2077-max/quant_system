"""
crypto_perp.py - 永续合约行情（用于基差 basis = perp/spot - 1）
================================================================
数据源：Binance USDT-M 合约 /fapi/v1/klines（免 Key）
存储：data/crypto/perp/<SYMBOL>_<interval>.csv
体积很小：30 币 × 日线 ≈ 数 MB
"""

import os
import time

import pandas as pd

FAPI = "https://fapi.binance.com"
PDIR = os.path.join("data", "crypto", "perp")
os.makedirs(PDIR, exist_ok=True)
DEFAULT_START_MS = 1560000000000      # 2019-06，永续日线足够


def _get(path, params, tries=5):
    import requests
    for i in range(tries):
        try:
            r = requests.get(FAPI + path, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(3 + 3 * i); continue
            return {"__err__": r.status_code, "__body__": r.text[:120]}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": "exc", "__body__": str(e)[:120]}
            time.sleep(2 + i)
    return None


def fetch_perp(symbol: str, interval: str = "1d", start_ms: int = None,
               verbose: bool = True) -> pd.DataFrame:
    start_ms = start_ms or DEFAULT_START_MS
    rows, cur = [], start_ms
    while True:
        j = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                     "limit": 1000, "startTime": cur})
        if j is None:
            break
        if isinstance(j, dict) and "__err__" in j:
            if verbose: print(f"[perp] {symbol} err {j['__err__']} {j.get('__body__','')[:60]}")
            return pd.DataFrame()
        if not j:
            break
        rows.extend(j)
        if len(j) < 1000:
            break
        cur = int(j[-1][0]) + 1
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume",
                                     "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"])
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    for c in ("open", "high", "low", "close", "volume", "qav"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["time", "open", "high", "low", "close", "volume", "qav"]].drop_duplicates("time").sort_values("time")


def save_perp(symbol: str, interval: str = "1d", refetch: bool = False) -> int:
    p = os.path.join(PDIR, f"{symbol}_{interval}.csv")
    prev = pd.read_csv(p, parse_dates=["time"]) if (os.path.exists(p) and not refetch) else None
    start = DEFAULT_START_MS
    if prev is not None and len(prev):
        start = int(prev["time"].max().timestamp() * 1000) - 3 * 24 * 3600 * 1000
    df = fetch_perp(symbol, interval, start_ms=start)
    if df.empty:
        return 0 if prev is not None else -1
    if prev is not None and len(prev):
        df = pd.concat([prev, df], ignore_index=True).drop_duplicates("time").sort_values("time")
    df.to_csv(p, index=False)
    return len(df)


def fetch_all(interval: str = "1d", symbols=None, verbose: bool = True):
    from crypto_data import UNIVERSE
    symbols = symbols or UNIVERSE
    out = {}
    for s in symbols:
        n = save_perp(s, interval)
        p = os.path.join(PDIR, f"{s}_{interval}.csv")
        info = {"rows": int(n) if n > 0 else 0}
        if os.path.exists(p):
            d = pd.read_csv(p, parse_dates=["time"])
            if len(d):
                info = {"rows": int(len(d)), "start": str(d["time"].min().date()),
                        "end": str(d["time"].max().date())}
        out[s] = info
        if verbose: print(f"[perp] {s:10s} rows={info.get('rows')} {info.get('start','')}~{info.get('end','')}", flush=True)
    return out


def perp_close_panel(interval: str = "1d") -> pd.DataFrame:
    import glob
    cols = {}
    for f in glob.glob(os.path.join(PDIR, f"*_{interval}.csv")):
        sym = os.path.basename(f)[: -len(f"_{interval}.csv")]
        d = pd.read_csv(f, parse_dates=["time"]).set_index("time")
        s = d["close"]; s = s[~s.index.duplicated(keep="last")]
        s.index = s.index.normalize()
        s = s.groupby(level=0).last()
        cols[sym] = s.rename(sym)
    return pd.concat(cols.values(), axis=1).sort_index() if cols else pd.DataFrame()


def basis_panel(interval: str = "1d") -> pd.DataFrame:
    """基差 = 永续收盘 / 现货收盘 − 1。"""
    from crypto_data import build_panel
    spot = build_panel(interval, "close")
    perp = perp_close_panel(interval)
    common = spot.index.intersection(perp.index)
    cols = spot.columns.intersection(perp.columns)
    return (perp.loc[common, cols] / spot.loc[common, cols] - 1.0)


if __name__ == "__main__":
    fetch_all()
