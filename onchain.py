"""
onchain.py - 链上 / 加密生态【日度聚合】数据（高信息密度、小体积）
====================================================================
原则（回应用户"电脑塞不下"）：只存【聚合层】，不存原始区块/tick。
下面全部免 API Key，落盘后总计 < 50 MB。

数据源：
  1. DefiLlama        总 TVL / 各链 TVL / 稳定币供应   → DeFi 资金面
  2. Blockchain.com   BTC 活跃地址/转账数/手续费/算力   → 链上活跃度
  3. Alternative.me   恐惧贪婪指数                      → 市场情绪
  4. CoinGecko(免费)   市值/成交量/开发与社区活跃度      → 基本面

存储：data/onchain/<name>.csv（date, value...）
"""

import os
import time
import json

import pandas as pd

DATA_DIR = os.path.join("data", "onchain")
os.makedirs(DATA_DIR, exist_ok=True)


def _get(url, params=None, tries=4, timeout=30):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418):
                time.sleep(2 + 2 * i); continue
            return {"__err__": r.status_code, "__body__": r.text[:160]}
        except Exception as e:
            if i == tries - 1:
                return {"__err__": "exc", "__body__": str(e)[:160]}
            time.sleep(1.5 + i)
    return None


def _save(name: str, df: pd.DataFrame) -> pd.DataFrame:
    p = os.path.join(DATA_DIR, f"{name}.csv")
    df.to_csv(p, index=False)
    try:
        df.to_parquet(p.replace(".csv", ".parquet"), index=False, compression="zstd")
    except Exception:
        pass
    return df


# ---------------------------------------------------------------------------
# 1) DefiLlama
# ---------------------------------------------------------------------------
def fetch_defillama_tvl() -> pd.DataFrame:
    """全市场 DeFi 总锁仓量（日度，2017 至今）。"""
    j = _get("https://api.llama.fi/v2/historicalChainTvl")
    if not isinstance(j, list):
        print("[onchain] defillama tvl err", j); return pd.DataFrame()
    df = pd.DataFrame(j)
    df["date"] = pd.to_datetime(df["date"], unit="s")
    df = df.rename(columns={"tvl": "total_tvl"})[["date", "total_tvl"]].sort_values("date")
    print(f"[onchain] defillama total_tvl rows={len(df)} {df['date'].min().date()}~{df['date'].max().date()}")
    return _save("defillama_tvl_total", df)


def fetch_defillama_chain(chain: str = "Ethereum") -> pd.DataFrame:
    j = _get(f"https://api.llama.fi/v2/historicalChainTvl/{chain}")
    if not isinstance(j, list):
        return pd.DataFrame()
    df = pd.DataFrame(j)
    df["date"] = pd.to_datetime(df["date"], unit="s")
    df = df.rename(columns={"tvl": f"tvl_{chain.lower()}"})[["date", f"tvl_{chain.lower()}"]].sort_values("date")
    print(f"[onchain] defillama {chain} tvl rows={len(df)}")
    return _save(f"defillama_tvl_{chain.lower()}", df)


def fetch_stablecoins() -> pd.DataFrame:
    """全市场稳定币总供应（日度）——衡量场内资金/购买力。"""
    j = _get("https://stablecoins.llama.fi/stablecoincharts/all")
    if not isinstance(j, list):
        print("[onchain] stablecoin err", j); return pd.DataFrame()
    rows = []
    for it in j:
        try:
            ts = int(it["date"])                      # 接口返回字符串时间戳
            circ = it.get("totalCirculatingUSD") or it.get("totalCirculating") or {}
            usd = float(circ.get("peggedUSD") or 0)
            rows.append({"date": pd.to_datetime(ts, unit="s").normalize(),
                         "stablecoin_mcap": usd})
        except Exception:
            continue
    df = pd.DataFrame(rows).sort_values("date")
    print(f"[onchain] stablecoin supply rows={len(df)} {df['date'].min().date()}~{df['date'].max().date()}")
    return _save("stablecoin_supply", df)


# ---------------------------------------------------------------------------
# 2) Blockchain.com（BTC 链上）
# ---------------------------------------------------------------------------
BC_METRICS = {
    "n-unique-addresses": "btc_active_addr",
    "n-transactions": "btc_tx_count",
    "hash-rate": "btc_hashrate",
    "transaction-fees-usd": "btc_fees_usd",
    "miners-revenue": "btc_miner_rev",
    "market-price": "btc_price",
    "estimated-transaction-volume-usd": "btc_tx_vol_usd",
    "difficulty": "btc_difficulty",
}


def fetch_blockchain_com(metrics=None) -> pd.DataFrame:
    metrics = metrics or BC_METRICS
    out = None
    for m, col in metrics.items():
        j = _get(f"https://api.blockchain.info/charts/{m}",
                 {"timespan": "all", "format": "json", "sampled": "false"})
        if not isinstance(j, dict) or "values" not in j:
            print(f"[onchain] bc {m} err"); continue
        df = pd.DataFrame(j["values"])
        df["date"] = pd.to_datetime(df["x"], unit="s").dt.normalize()
        df = df.rename(columns={"y": col})[["date", col]]
        df = df.groupby("date", as_index=False)[col].mean()
        out = df if out is None else out.merge(df, on="date", how="outer")
        print(f"[onchain] blockchain.com {m:38s} rows={len(df)}")
        time.sleep(0.4)
    if out is None:
        return pd.DataFrame()
    out = out.sort_values("date")
    return _save("btc_onchain", out)


# ---------------------------------------------------------------------------
# 3) 恐惧贪婪指数
# ---------------------------------------------------------------------------
def fetch_fear_greed() -> pd.DataFrame:
    j = _get("https://api.alternative.me/fng/", {"limit": 0, "format": "json"})
    if not isinstance(j, dict) or "data" not in j:
        print("[onchain] fng err", j); return pd.DataFrame()
    df = pd.DataFrame(j["data"])
    df["date"] = pd.to_datetime(pd.to_numeric(df["timestamp"]), unit="s").dt.normalize()
    df["fng"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[["date", "fng", "value_classification"]].sort_values("date")
    print(f"[onchain] fear&greed rows={len(df)} {df['date'].min().date()}~{df['date'].max().date()}")
    return _save("fear_greed", df)


if __name__ == "__main__":
    print("===== 抓取链上/生态日度聚合数据 =====")
    fetch_defillama_tvl()
    fetch_defillama_chain("Ethereum")
    fetch_stablecoins()
    fetch_blockchain_com()
    fetch_fear_greed()
    tot = sum(os.path.getsize(os.path.join(DATA_DIR, f)) for f in os.listdir(DATA_DIR))
    print(f"\n落盘目录 {DATA_DIR} 合计 {tot/1e6:.2f} MB")
