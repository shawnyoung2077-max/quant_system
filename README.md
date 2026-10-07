# quant_system

**数据无关的量化研究框架** —— 覆盖数据采集 → 因子挖掘 → 回测 → 组合构建 → 实盘执行 → 多市场适配。

> **同一套研究流程，适配三个结构完全不同的市场：**
> A股因子选股 · 加密资金费套利 · 预测市场有效性检验

自写代码 **约 5,300 行**（46 个核心模块，不含第三方 SDK）。

---

## 为什么是"数据无关"

核心引擎不关心数据来源，只要求一张 `日期 × 资产` 的价格面板。
接入新市场时，只需替换数据模块，回测/因子/指标层完全复用。

**这个设计在本项目里被验证了三次** —— 我把它分别接到了 A股、加密、Polymarket
三个数据结构和交易机制完全不同的市场上，上层代码没有改动。

---

## 核心能力

| 层 | 能力 |
|---|---|
| **数据** | A股（腾讯/东财双源兜底）、美股、加密（多交易所）、预测市场；断点续传、全市场批量抓取 |
| **因子** | 算子库 + IC / RankIC / ICIR / 分位收益评价；因子库持久化 |
| **回测** | 向量化引擎，含交易成本、换手约束、权重漂移 |
| **绩效** | 年化收益/波动、夏普、最大回撤、Calmar、胜率、滚动指标 |
| **验证** | Walk-Forward、参数网格、稳健性检验、样本内外拆分 |
| **组合** | 市场中性、行业中性、Fama-French 因子回归 |
| **执行** | 加密实盘下单、资金费套利执行、状态管理 |
| **展示** | Flask 看板 + 静态 HTML 报告 |

---

## 架构

```
                    ┌─────────────────────────────┐
                    │      数据层 (可替换)         │
                    │  realdata / crypto_data /   │
                    │  polymarket / fetch_market  │
                    └──────────────┬──────────────┘
                                   ↓  日期 × 资产 面板
                    ┌─────────────────────────────┐
                    │        因子层                │
                    │  factor / alpha_score /     │
                    │  alpha_library / field_screen│
                    └──────────────┬──────────────┘
                                   ↓  因子值
                    ┌─────────────────────────────┐
                    │        回测层                │
                    │  backtest / metrics /       │
                    │  market_neutral / ff_factors│
                    └──────────────┬──────────────┘
                                   ↓  净值 / 绩效
                    ┌─────────────────────────────┐
                    │      验证与执行层            │
                    │  robustness / reproduce /   │
                    │  carry_exec / crypto_exec   │
                    └─────────────────────────────┘
```

---

## 模块清单

### 数据层
| 文件 | 说明 |
|---|---|
| `realdata.py` | 真实 A股数据连接（腾讯/东财双源兜底） |
| `fetch_market.py` | 全市场批量抓取（沪深300+中证500，10年，断点续传） |
| `data.py` / `data_ohlcv.py` / `fetch_ohlcv.py` | 通用数据层与 OHLCV 转换 |
| `fundamentals.py` / `analyst_data.py` / `news_data.py` | 基本面、分析师、新闻数据 |
| `crypto_data.py` / `crypto_perp.py` / `crypto_funding.py` | 加密行情、永续、资金费 |
| `onchain.py` | 链上数据 |

### 因子层
| 文件 | 说明 |
|---|---|
| `factor.py` | 因子挖掘：算子 + IC/ICIR/分位收益评价 |
| `alpha_score.py` | Alpha 评分体系 |
| `alpha_library.py` | 因子库（SQLite 持久化） |
| `field_screen.py` | 字段/因子初步筛选 |

### 回测与绩效
| 文件 | 说明 |
|---|---|
| `backtest.py` | 向量化回测引擎（含成本、换手、权重漂移） |
| `metrics.py` | 绩效指标：年化/波动/夏普/回撤/Calmar/胜率 |
| `analysis.py` | 可视化：净值、回撤、滚动夏普 |
| `market_neutral.py` | 市场中性组合构建 |
| `ff_factors.py` | Fama-French 因子 |

### 验证
| 文件 | 说明 |
|---|---|
| `robustness.py` | 稳健性检验 |
| `stats.py` | 统计检验 |
| `reproduce.py` | 结果复现 |

### 执行与展示
| 文件 | 说明 |
|---|---|
| `carry_exec.py` / `crypto_exec.py` | 实盘执行 |
| `webapp.py` | Flask 看板 |
| `dashboard.html` / `crypto_dashboard.html` | 静态报告 |

### 研究脚本
`run.py`（统一运行器）· `research.py` · `alphas.py` · `strategies.py` ·
`cn_alpha_mining.py` · `crypto_mine.py` · `carry_v3.py`

### 预测市场模块
见 [`polymarket/README.md`](polymarket/README.md) —— 独立子模块，含数据采集、
建模表构建、分档校准分析、受控对照验证。**含一个完整的负面研究结论。**

---

## 三个市场

| 市场 | 策略方向 | 数据 | 状态 |
|---|---|---|---|
| **A股** | 因子选股（动量/规模/价值/非流动性） | 腾讯/东财，10年全市场 | 已回测，含 IC 筛选 |
| **加密** | 资金费套利（carry）、时序动量 | 多交易所 REST/WebSocket | 已回测 + 实盘执行模块 |
| **预测市场** | 市场有效性检验 / 定价偏差 | Polymarket Gamma + CLOB API | 已完成研究，**结论：不构成可交易机会** |

---

## 研究成果

仓库内含 13 个研究输出文件与 20+ 张图表：

| 文件 | 内容 |
|---|---|
| `output_cn_ic_screen.csv` | A股因子 IC/ICIR 筛选（4 个因子 × 多个持有期） |
| `output_cn_illiq.csv` | 非流动性因子网格（含样本内外拆分的 Sharpe/Fitness） |
| `output_cn_grid.csv` | A股参数网格搜索 |
| `output_carry_v3.csv` | 加密资金费套利网格（28 组参数，**含 IS/OS 拆分**） |
| `output_carry_grid.csv` / `output_carry_liquidation.csv` | 资金费套利参数敏感性 |
| `output_crypto_grid.csv` / `output_crypto_grid2.csv` | 加密策略网格 |
| `output_crypto_tsmom.csv` | 加密时序动量（含波动率目标） |
| `output_onchain_grid.csv` | 链上数据因子网格 |
| `output/` `output_real/` `output_dash/` | 净值曲线、回撤曲线、滚动夏普（PNG） |

**方法论上的一致要求**：所有网格搜索都记录**样本外结果**，不做单点最优参数选择。

---

## 一个负面结果（值得单独说）

在 Polymarket 上我检验了「预测市场是否存在系统性定价偏差」：

```
样本:   894,913 个已结算市场 / $211 亿累计成交
方法:   分档校准 —— 对比「隐含概率」与「实际结算频率」
```

**初版分析显示中档价格有 +5% ~ +9% 的偏差。** 但进一步检查发现问题：

> 不同观察时点的市场集合完全不同（30 天前只有 15% 的市场有价格），
> **初版的"偏差"是样本构成差异造成的假象。**

**修正后（只保留全部时点都有价格的同一批市场）：偏差在 ±1% 以内，符号随机跳动。**

唯一残留的信号是「价格 0.10-0.15 的合约被高估约 2~3%」——
**但 2~3% 不足以覆盖平台费率（rate 0.05）与买卖价差。**

**结论：不做。**

详见 [`polymarket/README.md`](polymarket/README.md)。

---

## 快速开始

```bash
pip install -r requirements.txt

# 用合成数据跑通全流程（脱机可跑）
python run_demo.py

# 真实 A股数据回测（需联网）
python run_real.py

# 统一运行器
python run.py --help

# 预测市场模块
python -m polymarket.fetch catalog
python -m polymarket.validate
```

---

## 依赖

见 `requirements.txt`。核心为 `numpy` / `pandas` / `requests` / `matplotlib`，
A股数据用 `akshare`，加密用 `ccxt`。

---

## 目录说明

```
quant_system/
├── *.py                 核心模块（46 个）
├── polymarket/          预测市场子模块
├── configs/             研究配置（JSON）
├── data/                数据缓存（gitignore）
├── output*/             图表输出
├── vendor/              第三方 SDK（gitignore）
└── requirements.txt
```

---

## 说明

- 本项目为个人量化研究学习项目，所有代码为自写（`vendor/` 下的第三方 SDK 除外）
- 所有策略均做了样本外验证，**不做单点最优参数选择**
- 加密实盘相关代码仅为研究与技术验证，API 密钥通过环境变量或 gitignore 配置读取，
  **不硬编码在源码中**
