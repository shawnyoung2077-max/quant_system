# 数据字典 — Polymarket 体育市场历史校准数据集

生成脚本：`build_modeling_data.py`
原始来源：Polymarket 官方 API（gamma-api）抓取的 **894,913 个已结算市场**，
经 `dataset_all.csv` 筛选出有价格序列的 **22,051 个市场 / 74,242 行**。

---

## 主表 `historical_rows.csv`（74,242 行 × 30 列，15.9 MB）

**一行 = 一个市场 × 一个观察时点**。同一市场在 6 个时点各一行，所以行数 ≈ 市场数 × 平均时点数。

### 原始字段（来自 Polymarket API）

| 列 | 类型 | 含义 |
|---|---|---|
| `market_id` | str | 市场唯一 ID（对应 Polymarket conditionId / gamma id） |
| `league` | str | 联赛代码（如 `soccer_finland_veikkausliiga` 之类，可能为空） |
| `league_name` | str | 联赛显示名（如 `Veikkausliiga`、`World Cup`） |
| `question` | str | 市场问题原文（**注意：这列含逗号/引号，读入时用标准 CSV 解析**） |
| `sports_market_type` | str | 市场类型，见下方「类型说明」。**空类型在 CSV 里是空串** |
| `neg_risk` | int | 是否 negRisk 多结果市场（1/0）。多结果事件会拆成多个二元市场 |
| `volume` | float | **该市场生命周期累计成交额（USD）**。⚠️ 不是当时的盘口深度 |
| `n_points` | int | 该市场有多少个价格观测点（价格序列长度） |
| `lead_days` | int | 距结算天数 ∈ {1, 3, 7, 14, 21, 30} |
| `price` | float | **该时点的价格 = 市场隐含概率**（YES 口径，0~1） |
| `outcome` | int | **最终结果**：1 = YES 结算为真，0 = 假 |
| `final_price` | float | 结算时价格（≈ outcome，可能有微小滑点） |
| `drift` | float | 价格变动量 |

### 派生特征（本脚本生成）

| 列 | 含义 |
|---|---|
| `league_total_volume` | 该联赛所有市场累计成交额之和 |
| `league_median_mkt_vol` | 该联赛**中位每市场成交额**（比总额更少受市场数量影响） |
| `league_market_count` | 该联赛的市场数 |
| `league_vol_quartile` | 按 `league_total_volume` 的联赛四分位：**1 = 最冷，4 = 最热** |
| `league_is_cold` | `league_vol_quartile == 1`（见下方 ⚠️ 样本量警告） |
| `league_medmkt_quartile` | 按 `league_median_mkt_vol` 的联赛四分位 |
| `league_is_cold_medmkt` | `league_medmkt_quartile == 1` |
| `mkt_vol_quartile` | **市场级**成交额四分位：1 = 最小额，4 = 最大额（各档约 18,560 行，样本均衡） |
| `mkt_vol_decile` | 市场级成交额十分位 1~10 |
| `price_band` | 价格分档：`0-.05` / `.05-.10` / `.10-.15` / `.15-.20` / `.20-.30` / `.30-.50` / `.50-.70` / `.70-.90` / `.90-1.0` |
| `calib_err` | **`outcome − price`**（实际胜率 − 隐含概率）。>0 = 市场**低估**，<0 = **高估** |
| `abs_calib_err` | `calib_err` 的绝对值（错价强度） |
| `edge_no` | **买 NO 的每股毛期望** = `price − outcome` = `−calib_err` |
| `edge_yes` | **买 YES 的每股毛期望** = `outcome − price` = `calib_err` |
| `ret_no_on_capital` | 买 NO 的**占投入资金**收益率 = `edge_no / (1 − price)` |
| `ret_yes_on_capital` | 买 YES 的占投入资金收益率 = `edge_yes / price` |
| `is_moneyline_like` | 是否为胜负类市场（`moneyline` / `child_moneyline` / `first_half_moneyline` / 空） |

---

## ⚠️ 读入时的三个坑（会导致静默错误）

1. **空类型会被读成 NaN。**
   `sports_market_type` 在 CSV 里是**空串**，但 pandas 默认把 `""` 解析为 `NaN`。
   于是 `df["sports_market_type"].isin(["moneyline", ""])` 会**静默漏掉 14,920 行**。
   → 正确做法：`df["sports_market_type"] = df["sports_market_type"].fillna("")`
   或在 `read_csv` 时不要 `keep_default_na=True`。

2. **`ret_no_on_capital` 有 3 个 NaN**（price 恰为 1，除零）。可安全丢弃。

3. **`volume` 是市场生命周期累计量，不是当时的盘口深度。**
   用它当"流动性"代理时要注意：一个成交额 $10M 的市场，在你建仓那一刻
   可能只有几千美元的盘口。**实测订单簿前 10 档深度中位仅约 $1,900。**
   所以 `volume` 更适合当"关注度/研究强度"的代理，不适合当容量代理。

---

## `sports_market_type` 类型说明

| 类型 | 行数 | 说明 |
|---|---|---|
| `moneyline` | 32,742 | **胜负类（1X2 的每一腿）** — 主要研究对象 |
| *(空)* | 14,920 | 多为对阵式胜负市场，**也应归入胜负类** |
| `totals` | 11,283 | 总分大小 |
| `soccer_exact_score` | 5,875 | **正确比分** — 价格不是 1X2 概率，⚠️ 不要与 h2h 混用 |
| `spreads` | 4,010 | 让分 |
| `soccer_halftime_result` | 1,215 | 半场结果 |
| `soccer_team_totals` | 1,203 | 单队总进球 |
| `both_teams_to_score` | 757 | 两队都进球 |
| `first_half_totals` | 716 | 上半场总分 |
| `child_moneyline` | 503 | 子市场胜负 |
| `total_corners` | 338 | 角球总数 |
| `soccer_first_to_score` | 169 | 首个进球 |

---

## 辅助表

### `league_summary.csv`（179 行）

| 列 | 含义 |
|---|---|
| `league_name` | 联赛名 |
| `n_rows` / `n_markets` | 行数 / 市场数 |
| `total_volume` | 联赛累计成交额 |
| `mean_price` / `mean_outcome` | 平均隐含概率 / 平均实际胜率 |
| `bias` | `mean_outcome − mean_price` |
| `se` / `t` | bias 的标准误与 t 值 |
| `reliability` | **分档后偏差平方的样本数加权**（比单一 bias 更严格的错价指标） |
| `is_cold` | 是否成交额后 50% |

### `price_band_summary.csv`（75 行）

按 `lead_days × price_band × is_cold` 汇总，含 `n` / `mean_price` / `mean_outcome` /
`bias` / `edge_no` / `se` / `t`。

### `summary.json`

总体摘要（行数、市场数、联赛数、各类型计数、中位成交额等）。

---

## ⚠️ 样本量警告（**最重要的一条**）

**"冷门联赛"的分层分析天然低样本。**

| 分组 | 行数 | 占比 |
|---|---|---|
| `league_is_cold == 1` | **119** | 0.16% |
| `league_is_cold_medmkt == 1` | 924 | 1.2% |
| `mkt_vol_quartile == 1` | 18,563 | 25% |

原因：**冷门联赛之所以"冷门"，一部分就是因为它们市场少。**

⇒ 若要做"冷门联赛"的分层建模，**要么接受极低样本，要么改用 `mkt_vol_quartile`**
（市场级成交额分位，样本均衡）。
两者的实证结论方向相反，见 `HANDOFF.md` 的「未决问题」。
