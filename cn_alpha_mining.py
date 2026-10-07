import numpy as np, pandas as pd
import run as R
from fundamentals import build_factor_panels
from data_ohlcv import load_ohlcv_panels
from backtest import portfolio_backtest, buy_and_hold
from research import cross_section_zscore, winsorize
from alpha_score import score_factor

prices = R.load_prices()
avail = [c for c in prices.columns if c in __import__("data_ohlcv").list_available()]

# 财务因子面板
fund = build_factor_panels(prices, symbols=list(prices.columns),
                           factors=["bm","size","roe","investment"], progress=False, cached_only=True)
# 量价字段
o = load_ohlcv_panels(symbols=list(prices.columns))
dollar_vol = (o["volume"]*o["close"]).reindex(prices.index).reindex(prices.columns,axis=1)
ret5 = prices.pct_change(5)

def zh(s):  # 横截面zscore
    return cross_section_zscore(winsorize(s))

ALPHAS = {
  "小盘(small)":      fund["size"],                       # 负size=小盘，rank取最小
  "价值(bm)":         fund["bm"],
  "流动性(dollar_vol)": dollar_vol,
  "小盘+价值组合":      zh(fund["size"])*0.5 + zh(fund["bm"])*0.5,
  "小盘+流动性":        zh(fund["size"])*0.5 + zh(dollar_vol)*0.5,
  "价值+盈利":          zh(fund["bm"])*0.5 + zh(fund["roe"])*0.5,
  "反转20日":           ret5,
}
print("="*70)
print("中国A股 alpha 候选簇（BRAIN标准评分，含成本/持有）")
print("="*70)
results=[]
for name, f in ALPHAS.items():
    f = f.reindex(prices.index).reindex(prices.columns, axis=1)
    sc = score_factor(prices, f, top_pct=0.2, long_only=True, hold=10)
    results.append((name, sc))
    print("\n【%s】grade=%s  sharpe=%.2f fitness=%.3f ret=%.2f%% turn=%.1f" % (
        name, sc["grade"], sc["sharpe"], sc["fitness"], sc["returns"]*100, sc["turnover"]))
print("\n"+"="*70)
print("达标(GOOD: Sharpe≥1.25 且 Fitness≥1.0)的:")
for name,sc in results:
    if sc["sharpe"]>=1.25 and sc["fitness"]>=1.0:
        print("  【%s】sharpe=%.2f fitness=%.3f" % (name, sc["sharpe"], sc["fitness"]))
