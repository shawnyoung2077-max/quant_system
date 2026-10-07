"""_verify_doc3.py - 核对核心反常现象：legacy 口径下 min_f 扫描是否真的"越筛越差" """
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
spot = build_panel("1d", "close"); dv = load_liquidity("1d", "qav")
fund = CF.funding_daily(); basis = basis_panel("1d")
idx = spot.index.intersection(fund.index).intersection(basis.index)
spot, dv, fund, basis = (spot.loc[idx], dv.reindex(idx).reindex(columns=spot.columns),
                         fund.loc[idx], basis.reindex(idx).reindex(columns=spot.columns))
mask = filter_universe(spot, dv, top_n=25, min_dv_usd=3e6)

L = []
def p(s): L.append(s)

p("=" * 104)
p("核心反常现象核对：min_f 扫描 × 两种口径（reb28 / top30 / 2x / 完整建模）")
p("=" * 104)
p("%-12s | %-38s | %-38s" % ("min_f", "legacy（旧口径，含 bug）", "incremental（修正后）"))
p("%-12s | %9s %8s %9s %9s | %9s %8s %9s %9s" %
  ("", "Sharpe", "年化", "fund", "basis", "Sharpe", "年化", "fund", "basis"))
p("-" * 104)

res = {}
for min_f in (-1.0, 0.0, 5e-5, 1e-4, 5e-4):
    row = {}
    for mode in ("legacy", "incremental"):
        kw = dict(top_n=30, min_f=min_f, reb=28, leverage=2.0,
                  fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
        out, nl = carry_v3(spot, fund, mask, basis=basis, dollar_volume=dv,
                           basis_mode=mode, **kw)
        n = out["net"]
        row[mode] = (sharpe_ratio(n, PPY), annualized_return(n, PPY),
                     float(out["funding"].sum()), float(out["basis"].sum()))
    res[min_f] = row
    p("%-12s | %9.2f %7.1f%% %9.4f %9.4f | %9.2f %7.1f%% %9.4f %9.4f" %
      (min_f, row["legacy"][0], row["legacy"][1] * 100, row["legacy"][2], row["legacy"][3],
       row["incremental"][0], row["incremental"][1] * 100,
       row["incremental"][2], row["incremental"][3]))

p("")
seq = [res[m]["legacy"][0] for m in (-1.0, 0.0, 5e-5, 1e-4, 5e-4)]
p("legacy 的 min_f 序列:  " + " -> ".join("%.2f" % s for s in seq))
p("incremental 是否单调递减（符合直觉）？ " +
  " -> ".join("%.2f" % res[m]["incremental"][0] for m in (-1.0, 0.0, 5e-5, 1e-4, 5e-4)))

p("")
p("=" * 104)
p("配对消融：紧筛(5e-4) 相对 不筛(0.0) 的差异来自哪里？")
p("=" * 104)
for mode in ("legacy", "incremental"):
    d_sh = res[5e-4][mode][0] - res[0.0][mode][0]
    d_f = res[5e-4][mode][2] - res[0.0][mode][2]
    d_b = res[5e-4][mode][3] - res[0.0][mode][3]
    p("  %-12s ΔSharpe=%+7.2f   Δ资金费P&L=%+.4f   Δ基差P&L=%+.4f" % (mode, d_sh, d_f, d_b))
p("  -> 看 Δ基差P&L：legacy 下紧筛会【丢掉】一大块基差收益，这才是反常的来源")

with open(r"D:\26050\Documents\quant_system\docs\verify\out_3_minf_anomaly.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
print("ok")
