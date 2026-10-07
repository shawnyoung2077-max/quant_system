"""
_verify_doc_numbers.py - 为 docs/DEBUG_carry_basis.md 逐条核对数字（防止文档里写错）
"""
import sys
sys.path.insert(0, r"D:\26050\Documents\quant_system")
import numpy as np, pandas as pd
from alpha_score import sharpe_ratio, annualized_return
import crypto_funding as CF
from crypto_data import build_panel
from crypto_perp import basis_panel
from market_neutral import filter_universe, load_liquidity
from carry_v3 import carry_v3

PPY = 365
spot = build_panel("1d", "close")
dv = load_liquidity("1d", "qav")
fund = CF.funding_daily()
basis = basis_panel("1d")
idx = spot.index.intersection(fund.index).intersection(basis.index)
spot, dv, fund, basis = (spot.loc[idx], dv.reindex(idx).reindex(columns=spot.columns),
                         fund.loc[idx], basis.reindex(idx).reindex(columns=spot.columns))
mask = filter_universe(spot, dv, top_n=25, min_dv_usd=3e6)

lines = []
def p(s):
    lines.append(s)

p("[data] %d天 × %d币  %s~%s" % (spot.shape[0], spot.shape[1], idx.min().date(), idx.max().date()))
p("")
p("=" * 100)
p("A. legacy vs incremental（含基差，完整建模）")
p("=" * 100)
p("%-22s %-13s %8s %8s %8s %8s %8s %10s %10s" %
  ("配置", "口径", "Sharpe", "年化", "波动", "MaxDD", "OS", "fund_part", "basis_part"))

def mdd(n):
    c = (1 + n).cumprod()
    return float((1 - c / c.cummax()).max())

def run(top_n, reb, basis_mode):
    kw = dict(top_n=top_n, min_f=5e-5, reb=reb, leverage=2.0,
              fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
    out, nl = carry_v3(spot, fund, mask, basis=basis, dollar_volume=dv,
                       basis_mode=basis_mode, **kw)
    n = out["net"]
    cut = int(len(n) * 0.6)
    return dict(sharpe=sharpe_ratio(n, PPY), ann=annualized_return(n, PPY),
                vol=float(n.std() * np.sqrt(PPY)), mdd=mdd(n),
                OS=sharpe_ratio(n.iloc[cut:], PPY),
                fund=float(out["funding"].sum()), bas=float(out["basis"].sum()),
                cost=float(out["cost"].sum()), net=float(n.sum()), _n=n)

for top_n, reb in ((15, 14), (30, 28)):
    for mode in ("legacy", "incremental"):
        r = run(top_n, reb, mode)
        p("%-22s %-13s %8.2f %7.1f%% %7.2f%% %7.2f%% %8.2f %10.4f %10.4f" %
          ("reb%d/top%d" % (reb, top_n), mode, r["sharpe"], r["ann"] * 100,
           r["vol"] * 100, r["mdd"] * 100, r["OS"], r["fund"], r["bas"]))

p("")
p("=" * 100)
p("B. 纯资金费（basis=None）—— 文档 6.4 节的表")
p("=" * 100)
p("%-22s %8s %8s %8s %8s %8s" % ("配置", "Sharpe", "年化", "波动", "IS", "OS"))

def run_f(top_n, reb):
    kw = dict(top_n=top_n, min_f=5e-5, reb=reb, leverage=2.0,
              fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
    out, nl = carry_v3(spot, fund, mask, basis=None, dollar_volume=dv, **kw)
    n = out["net"]
    cut = int(len(n) * 0.6)
    return (sharpe_ratio(n, PPY), annualized_return(n, PPY),
            float(n.std() * np.sqrt(PPY)), sharpe_ratio(n.iloc[:cut], PPY),
            sharpe_ratio(n.iloc[cut:], PPY))

for top_n, reb in ((15, 14), (20, 28)):
    s, a, v, isr, osr = run_f(top_n, reb)
    p("%-22s %8.2f %7.1f%% %7.2f%% %8.2f %8.2f" %
      ("reb%d/top%d" % (reb, top_n), s, a * 100, v * 100, isr, osr))

p("")
p("=" * 100)
p("C. min_f 扫描（incremental 修正后）—— 文档 6.3 节的表")
p("=" * 100)
p("%-14s %10s %10s" % ("min_f", "Sharpe", "年化"))
for min_f in (-1.0, 0.0, 5e-5, 1e-4, 5e-4):
    kw = dict(top_n=30, min_f=min_f, reb=28, leverage=2.0,
              fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
    out, nl = carry_v3(spot, fund, mask, basis=basis, dollar_volume=dv,
                       basis_mode="incremental", **kw)
    n = out["net"]
    p("%-14s %10.2f %9.1f%%" % (min_f, sharpe_ratio(n, PPY), annualized_return(n, PPY) * 100))

p("")
p("=" * 100)
p("D. 资金费前视：同日 / 滞后1 / 滞后2（纯资金费）")
p("=" * 100)
for lag in (0, 1, 2):
    f = fund.shift(lag).fillna(0.0) if lag else fund
    kw = dict(top_n=15, min_f=5e-5, reb=14, leverage=2.0,
              fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
    out, nl = carry_v3(spot, f, mask, basis=None, dollar_volume=dv, **kw)
    p("  滞后%d天: Sharpe=%.2f" % (lag, sharpe_ratio(out["net"], PPY)))

p("")
p("=" * 100)
p("E. 资金费数据本身 / 波动比 / BTC 基准")
p("=" * 100)
fs = fund.stack()
p("  日均资金费=%.6f (≈年化%.1f%%)  为正比例=%.1f%%" %
  (fs.mean(), fs.mean() * PPY * 100, (fs > 0).mean() * 100))
btc = spot["BTCUSDT"].pct_change().fillna(0)
bv = float(btc.std() * np.sqrt(PPY))
p("  BTC: Sharpe=%.2f 年化=%.1f%% 波动=%.1f%%" %
  (sharpe_ratio(btc, PPY), annualized_return(btc, PPY) * 100, bv * 100))
r = run(30, 28, "incremental")
p("  carry(legacy) 波动/BTC 波动 = %.1f%%" % (run(30, 28, "legacy")["vol"] / bv * 100))
p("  carry(fixed)  波动/BTC 波动 = %.1f%%" % (r["vol"] / bv * 100))

p("")
p("=" * 100)
p("F. 分年度 Sharpe（incremental）")
p("=" * 100)
n = r["_n"]
for y, grp in n.groupby(n.index.year):
    p("  %d: Sharpe=%6.2f  年化=%7.2f%%" %
      (y, sharpe_ratio(grp, PPY), annualized_return(grp, PPY) * 100))

with open(r"D:\26050\Documents\quant_system\docs\verify\out_1_core.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("written")
