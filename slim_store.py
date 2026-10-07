"""
slim_store.py - 统一"瘦身"存储层（Parquet 优先，CSV 兼容）
============================================================
设计目标（回应用户"数据太大塞不下"）：
  1. 只保留【高信息密度】层：日线 + 小时线 + 日度聚合指标（原始 tick/1m/区块一律不落地）
  2. 落盘统一用 Parquet：同数据体积约缩 3~10 倍、读取快 10 倍
  3. **向后兼容**：读取时优先 .parquet，没有则回退 .csv —— 现有代码不用改

用法：
    from slim_store import to_parquet, read_table, compact_dir, footprint
    to_parquet("data/a_share_close.csv")          # 生成同名 .parquet
    df = read_table("data/a_share_close")         # 自动选 parquet/csv
    compact_dir("data/crypto", purge=False)       # 批量转换
    footprint("data")                             # 统计体积
"""

import os
import glob

import pandas as pd

# 让 vendor/ 里的 pyarrow 可用（与 crypto_exec 同一套独立依赖目录）
import sys as _sys
_v = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_v) and _v not in _sys.path:
    _sys.path.insert(0, _v)


def _stem(path: str) -> str:
    for ext in (".parquet", ".csv"):
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


def to_parquet(csv_path: str, purge: bool = False) -> dict:
    """把单个 CSV 转成 Parquet。purge=True 时转换成功后删除原 CSV。"""
    if not os.path.exists(csv_path):
        return {"ok": False, "reason": "not found"}
    pq = _stem(csv_path) + ".parquet"
    try:
        df = pd.read_csv(csv_path)
        # 尝试把第一列解析成时间索引
        first = df.columns[0]
        try:
            df[first] = pd.to_datetime(df[first])
        except Exception:
            pass
        df.to_parquet(pq, index=False, compression="zstd")
        s_csv = os.path.getsize(csv_path)
        s_pq = os.path.getsize(pq)
        if purge:
            os.remove(csv_path)
        return {"ok": True, "csv": s_csv, "parquet": s_pq,
                "ratio": round(s_csv / max(s_pq, 1), 2), "path": pq}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def read_table(path_noext: str, **kw) -> pd.DataFrame:
    """优先读 .parquet，不存在则读 .csv。"""
    pq, csv = path_noext + ".parquet", path_noext + ".csv"
    if os.path.exists(pq):
        return pd.read_parquet(pq, **kw)
    if os.path.exists(csv):
        return pd.read_csv(csv, **kw)
    raise FileNotFoundError(path_noext + "(.parquet/.csv)")


def compact_dir(d: str, purge: bool = False, verbose: bool = True) -> dict:
    """批量转换目录下所有 CSV。"""
    files = glob.glob(os.path.join(d, "**", "*.csv"), recursive=True)
    tot_csv = tot_pq = 0
    ok = fail = 0
    for f in files:
        r = to_parquet(f, purge=purge)
        if r.get("ok"):
            ok += 1; tot_csv += r["csv"]; tot_pq += r["parquet"]
        else:
            fail += 1
            if verbose: print(f"  skip {os.path.basename(f)}: {r.get('reason')}")
    res = {"files": len(files), "ok": ok, "fail": fail,
           "csv_MB": round(tot_csv / 1e6, 1), "parquet_MB": round(tot_pq / 1e6, 1),
           "ratio": round(tot_csv / max(tot_pq, 1), 2)}
    if verbose:
        print(f"[compact] {d}: {ok}/{len(files)} 成功  {res['csv_MB']}MB -> {res['parquet_MB']}MB "
              f"(压缩 {res['ratio']}x)")
    return res


def footprint(root: str = "data") -> dict:
    """统计体积（按扩展名）。"""
    out = {}
    for f in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if not os.path.isfile(f):
            continue
        ext = os.path.splitext(f)[1].lower() or "(noext)"
        out[ext] = out.get(ext, 0) + os.path.getsize(f)
    out = {k: round(v / 1e6, 1) for k, v in sorted(out.items(), key=lambda x: -x[1])}
    out["_total_MB"] = round(sum(v for k, v in out.items() if not k.startswith("_")), 1)
    return out


if __name__ == "__main__":
    print("== 转换前 ==")
    print(footprint("data"))
    for d in ("data/crypto", "data/fundamentals", "data/ohlcv", "data/analyst",
              "data/news", "data/stocks"):
        if os.path.isdir(d):
            compact_dir(d, purge=False, verbose=False)
    # A股主面板（单文件）
    if os.path.exists("data/a_share_close.csv"):
        print("a_share_close:", to_parquet("data/a_share_close.csv", purge=False))
    print("\n== 转换后 ==")
    print(footprint("data"))
