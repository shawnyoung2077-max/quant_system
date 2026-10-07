"""_verify_doc2.py - 分年度贡献 + 尾部拆解 + 参数稳定性（标注口径）"""
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

def go(top_n, reb, mode="legacy", trim=None):
    kw = dict(top_n=top_n, min_f=5e-5, reb=reb, leverage=2.0,
              fee_spot_bps=8, fee_perp_bps=6, capital=1e7, cap_k=0.10)
    out, nl = carry_v3(spot, fund, mask, basis=basis, dollar_volume=dv,
                       basis_mode=mode, **kw)
    n = out["net"].copy()
    if trim:
        k = int(len(n) * trim)
        n = n.drop(n.nlargest(k).index)   # 剔除最赚的 k 天
    return n

p("=" * 96)
p("G. 尾部拆解（legacy 口径, reb28/top30）")
p("=" * 96)
n0 = go(30, 28, "legacy")
p("  原 Sharpe=%.2f   剔除最赚5%%后 Sharpe=%.2f   剔除最赚1%%后=%.2f" %
  (sharpe_ratio(n0, PPY), sharpe_ratio(go(30, 28, "legacy", 0.05), PPY),
   sharpe_ratio(go(30, 28, "legacy", 0.01), PPY)))

p("")
p("=" * 96)
p("H. 参数稳定性：reb 扫描（legacy 口径, top30）")
p("=" * 96)
vals = []
for reb in (14, 28, 40):
    s = sharpe_ratio(go(30, reb, "legacy"), PPY); vals.append(s)
    p("  reb=%2d  Sharpe=%.2f" % (reb, s))
p("  范围/均值 = %.0f%%" % ((max(vals) - min(vals)) / np.mean(vals) * 100))

p("")
p("=" * 96)
p("I. 分年度贡献（incremental 修正后, reb28/top30）")
p("=" * 96)
n = go(30, 28, "incremental")
tot = float(n.sum())
cum = 0.0
for y, g in n.groupby(n.index.year):
    p("  %d  Sharpe=%6.2f  年化=%7.2f%%  P&L贡献=%6.2f%%" %
      (y, sharpe_ratio(g, PPY), annualized_return(g, PPY) * 100, float(g.sum()) / tot * 100))
    if y in (2020, 2021):
        cum += float(g.sum())
p("  2020+2021 合计 P&L 贡献 = %.1f%%" % (cum / tot * 100))
p("  2022 起累计 P&L 贡献 = %.1f%%" % ((tot - cum) / tot * 100))

p("")
p("=" * 96)
p("J. legacy 口径下的分年度（对照）")
p("=" * 96)
nl = go(30, 28, "legacy"); totl = float(nl.sum()); cuml = 0.0
for y, g in nl.groupby(nl.index.year):
    p("  %d  Sharpe=%6.2f  P&L贡献=%6.2f%%" % (y, sharpe_ratio(g, PPY), float(g.sum()) / totl * 100))
    if y in (2020, 2021):
        cuml += float(g.sum())
p("  2020+2021 合计 P&L 贡献 = %.1f%%" % (cuml / totl * 100))

p("")
p("=" * 96)
p("K. 波动比（BTC = 60.4% 年化）")
p("=" * 96)
for mode in ("legacy", "incremental"):
    nn = go(30, 28, mode)
    p("  %-12s 波动=%.2f%%  波动/BTC=%.1f%%" %
      (mode, float(nn.std() * np.sqrt(PPY)) * 100, float(nn.std() * np.sqrt(PPY)) / 0.604 * 100))

with open(r"D:\26050\Documents\quant_system\docs\verify\out_2_robustness.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
print("ok")
