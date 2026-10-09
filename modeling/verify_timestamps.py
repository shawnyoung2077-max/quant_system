"""verify_timestamps.py - 独立验证「取价时点是否已在开赛之后」
背景：数据脚本按 end_date - lead_days 取价，而 end_date ≠ 开赛时间。
      若 end_date 晚于开赛（结算有滞后），则 lead_days=3 的取价可能已在赛中/赛后。
本脚本用市场元数据里的 game_start_time 直接核对。

⚠️ 本脚本保留在仓库里（不再用完即删）—— 上一次复核的临时脚本没留档，
   导致对方无法复算。这是流程问题，已修正。
"""
import sys, json, glob, os
sys.path.insert(0, r"D:\26050\Documents\quant_system")
sys.stdout.reconfigure(errors="replace")
import numpy as np, pandas as pd

ROOT = r"D:\26050\Documents\quant_system"
MARKETS = r"D:\26050\Documents\polymarket_sports\data\markets"
PRICES = r"D:\26050\Documents\polymarket_sports\data\prices"

df = pd.read_csv(os.path.join(ROOT, "modeling", "historical_rows.csv"),
                 keep_default_na=False, low_memory=False)
df["price"] = pd.to_numeric(df.price, errors="coerce")
df["outcome"] = pd.to_numeric(df.outcome, errors="coerce")
ids = set(df.market_id.astype(str))
print("建模表 %d 行 / %d 市场" % (len(df), len(ids)))

# ---- 读市场元数据：end_date / game_start_time / closed_time ----
meta = {}
for p in glob.glob(os.path.join(MARKETS, "*.json")):
    try:
        ms = json.loads(open(p, encoding="utf-8").read())
    except Exception:
        continue
    if not isinstance(ms, list):
        ms = [ms]
    for m in ms:
        k = str(m.get("market_id"))
        if k in ids and k not in meta:
            meta[k] = {"end": m.get("end_date"), "start": m.get("game_start_time"),
                       "closed": m.get("closed_time"),
                       "toks": m.get("clob_token_ids")}
print("取到元数据 %d / %d" % (len(meta), len(ids)))
has_start = sum(1 for v in meta.values() if v.get("start"))
print("  其中含 game_start_time 的: %d (%.1f%%)"
      % (has_start, has_start / max(len(meta), 1) * 100))

df["end_date"] = pd.to_datetime(df.market_id.astype(str).map(
    {k: v["end"] for k, v in meta.items()}), errors="coerce", utc=True)
df["game_start"] = pd.to_datetime(df.market_id.astype(str).map(
    {k: v["start"] for k, v in meta.items()}), errors="coerce", utc=True)
df["closed_time"] = pd.to_datetime(df.market_id.astype(str).map(
    {k: v["closed"] for k, v in meta.items()}), errors="coerce", utc=True)

# ---- 取价时点 = end_date - lead_days ----
df["quote_time"] = df.end_date - pd.to_timedelta(df.lead_days, unit="D")

t3 = df[df.lead_days == 3].copy()
n_all = len(t3)
ok = t3[t3.game_start.notna() & t3.end_date.notna()]
print()
print("=" * 92)
print("1. lead_days=3 的取价时点 vs 实际开赛时间")
print("=" * 92)
print("  lead_days=3 的记录数              : %d" % n_all)
print("  其中可核验（有 game_start_time）   : %d (%.1f%%)"
      % (len(ok), len(ok) / n_all * 100))
print("  无法核验（缺开赛时间）             : %d" % (n_all - len(ok)))

after = ok[ok.quote_time >= ok.game_start]
print()
print("  ★ 取价时点 >= 开赛时间（赛中/赛后）: %d  (占可核验的 %.2f%%)"
      % (len(after), len(after) / len(ok) * 100))
lead_lag = (ok.end_date - ok.game_start).dt.total_seconds() / 86400
print("  end_date − 开赛 的滞后（天）: 中位 %.2f  均值 %.2f  min %.2f  max %.2f"
      % (lead_lag.median(), lead_lag.mean(), lead_lag.min(), lead_lag.max()))
print("  滞后 > 3 天的记录（3天前取价必然在赛后）: %d"
      % int((lead_lag > 3).sum()))

# ---- 逐 lead_days ----
print()
print("=" * 92)
print("2. 各 lead_days 的赛中/赛后取价占比")
print("=" * 92)
print("  %-8s %10s %12s %14s" % ("lead", "可核验", "赛后取价", "占比"))
print("  " + "-" * 52)
for L in sorted(df.lead_days.unique()):
    s = df[(df.lead_days == L) & df.game_start.notna() & df.end_date.notna()]
    if len(s) == 0:
        continue
    a = (s.quote_time >= s.game_start).sum()
    print("  %-8d %10d %12d %13.2f%%" % (L, len(s), a, a / len(s) * 100))

# ---- 用 OOS 信号的 market_id 交叉核对 ----
print()
print("=" * 92)
print("3. 交叉核对：OOS 的 2981 笔信号里有多少是赛后取价")
print("=" * 92)
P = os.path.join(ROOT, "modeling", "model_results", "oos_predictions.csv")
try:
    oos = pd.read_csv(P)
    oos["side"] = oos["side"].astype(str)
    sig = oos[(oos.lead_days == 3) & (oos.side != "SKIP")].copy()
    sig["game_start"] = pd.to_datetime(sig.market_id.astype(str).map(
        {k: v["start"] for k, v in meta.items()}), errors="coerce", utc=True)
    sig["end_date"] = pd.to_datetime(sig.market_id.astype(str).map(
        {k: v["end"] for k, v in meta.items()}), errors="coerce", utc=True)
    sig["quote_time"] = sig.end_date - pd.to_timedelta(3, unit="D")
    ver = sig[sig.game_start.notna() & sig.end_date.notna()]
    post = ver[ver.quote_time >= ver.game_start]
    print("  信号总数                       : %d" % len(sig))
    print("  可核验                         : %d" % len(ver))
    print("  无法核验（缺开赛时间）          : %d" % (len(sig) - len(ver)))
    print("  ★ 赛中/赛后取价                : %d (%.2f%%)"
          % (len(post), len(post) / max(len(ver), 1) * 100))
    print()
    # 剔除后重算净 edge
    def realized(s):
        px = np.where(s.side == "YES", s.price, 1 - s.price)
        pay = np.where(s.side == "YES", s.outcome, 1 - s.outcome)
        return (pay - px) - s.estimated_cost
    for nm, s in (("全部信号", sig), ("剔除赛后取价", ver[ver.quote_time < ver.game_start]),
                  ("仅可核验", ver)):
        r = realized(s)
        se = r.std() / np.sqrt(len(r))
        print("  %-16s n=%-5d 净edge=%+.5f  SE=%.5f  t=%+.2f"
              % (nm, len(s), r.mean(), se, r.mean() / se))
    print()
    # 低价档重算（对方特别要求）
    print("  低价档(.10-.30)重算:")
    for nm, s in (("全部", sig), ("剔除赛后", ver[ver.quote_time < ver.game_start])):
        lon = s[(s.price >= .10) & (s.price < .30)]
        if len(lon) < 20:
            continue
        r = realized(lon)
        print("    %-10s n=%-5d 净edge=%+.5f  t=%+.2f"
              % (nm, len(lon), r.mean(), r.mean() / (r.std() / np.sqrt(len(r)))))
        mx = (lon.side == "YES")
        print("               买YES %d / 买NO %d   毛偏差(实际−价格)=%+.4f"
              % (mx.sum(), (~mx).sum(), lon.outcome.mean() - lon.price.mean()))
except Exception as e:
    print("  跳过（%s）" % str(e)[:80])
