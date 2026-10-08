# polytrade — Polymarket 冷门竞技市场的纸面交易与前瞻校准系统

> **一句话**：7×24 自动记录 Polymarket 冷门联赛的价格与结果，
> 用真实前瞻数据回答「错价存不存在、多大、朝哪边」，并据此反推所需启动资金。

---

## 为什么这样设计（重要的设计决策，不是随手写的）

### 决策 1：先记录【观测】，再事后计算任何策略

我原本想直接按一个规则下注。但**我自己的历史研究给出了矛盾的方向**：

| 样本 | 0.10-0.15 档 edge | 含义 |
|---|---|---|
| "全 6 时点都有价格"受控样本（2768 个） | **+0.0161** | 买 NO |
| 单时点全样本（16527 个） | **−0.0270** | 买 YES |

**符号都反了。** 在这种情况下锁定一个方向下注，就是在赌我的样本选择没出错。

所以系统分两层：

| 层 | 表 | 作用 |
|---|---|---|
| **观测日志** | `observations` | 记录每个合格市场的价格+结果。**不含任何方向决策** ⇒ 不会被我的偏见污染 ⇒ 任何策略规则都能事后在同一份数据上回测 |
| **纸面账本** | `bets` | 按显式写死的规则模拟下注，给出一个具体赚亏数字 |

**为什么必须记观测而不只记下注**：只记下注，我就只观测到"我决定下注的那一小部分"。
而**没下注的那些恰恰是判断"我的规则选对了吗"的对照组**。

### 决策 2：观测要宽收，下注才用窄规则

初版把"价格档 0.10-0.15"**同时**用在观测和下注上，结果 229 个合格市场只记录到 **8 个** ——
那个档只占价格轴的 5%，而市场大量聚集在 0.30-0.50（勘察实测 830/2482）。

改成宽收后：观测 **689 个/天**。**存储成本可忽略，而"当时没记录"是无法补救的。**

### 决策 3：下注档必须够宽，否则永远到不了显著

初版下注档 0.10-0.15 → 只有 7 个候选；几周也攒不到十几注，
而达到 t>2 需要 865 个已结算市场。已放宽到 0.05-0.35。

---

## 结构

```
polytrade/
├── config.py    全部可调参数（集中一处，便于事后审计"当时用什么规则跑的"）
├── db.py        SQLite 存储层（observations / bets / runs）
├── scan.py      抓 Polymarket → 筛选（含漏斗诊断）→ 写观测 → 建纸面下注
├── settle.py    查结算结果 → 算 P&L（含官方费率公式）
├── report.py    校准分析 + P&L + 启动资金反推
└── run.py       主入口（供定时任务调用）
```

## 用法

```bash
python -m polytrade.run                # 完整：扫描 + 结算 + 报告
python -m polytrade.run --dry          # 只写观测，不下注
python -m polytrade.run --scan-only
python -m polytrade.run --settle-only
python -m polytrade.run --report-only
```

## 成本模型（官方公式，非假设）

```
fee = C × feeRate × p × (1 − p)      C=份额数, p=成交价
体育 taker feeRate = 0.05；Maker = 0
```

成交价含跨越价差：
- 买 YES → 付 `ask`
- 买 NO → 付 `1 − bid`

## P&L

```
shares = stake / buy_price
fee    = shares × feeRate × buy_price × (1 − buy_price)
gross  = shares × 1  (赢) 或 0  (输)
pnl    = gross − stake − fee
```

⚠️ **必须区分"未结算"和"结算为 0"** —— 两者 `outcomePrices` 都是 0 开头，
但前者不该被当成亏损。判据用 `closed` 字段。

## 启动资金模型

```
年利润 = C·k·n − C·r_f − F
保本 ⇒ C_min = F / (k·n − r_f)

k = 每次交易净收益率, n = 年周转次数 = 365/持有天数
r_f = 美债年化, F = 年固定成本(AI 订阅等)
```

**分母 (k·n − r_f) 里没有 C**：
- `k·n > r_f` → 加大资金能摊薄固定成本（资金**能**解决）
- `k·n ≤ r_f` → **任何资金量都不盈利**（资金**不能**解决）

---

## 定时任务

```
任务名: polytrade_scan    每 2 小时
```

注册时踩过的坑（已处理，别重复踩）：

| 坑 | 症状 | 处理 |
|---|---|---|
| `/RL HIGHEST` 需要管理员 | `ERROR: Access is denied.` | 去掉该参数 |
| **`DisallowStartIfOnBatteries=true`** | 任务卡在 `Queued`，永不执行 | 用 `New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries` |
| `[TimeSpan]::MaxValue` 作重复时长 | `Duration:P99999999DT23H59M59S` 报错 | 省略 `-RepetitionDuration` |
| 计划任务 PATH 极简 | python 找不到 | .bat 里用绝对路径 |
| .bat 编码 | 中文乱码/崩溃 | 写 GBK |
| **`Logon Mode: Interactive only`** | 注销后不跑 | 见下方"已知限制" |

---

## 已知限制（诚实列出）

1. **`Logon Mode: Interactive only`** —— 任务只在用户登录时运行。
   要真正 7×24（注销也跑），需要在"任务计划程序"GUI 里
   勾选"不管用户是否登录都运行"并**输入密码**（schtasks 存凭据需要密码，
   agent 无法代替用户输入）。

2. **observations 每个市场每天只保留最后一次快照**（`UNIQUE(market_id, snap_date)`）。
   所以每 2 小时跑一次不会累积盘中价格序列，只保留当日最后值。
   要盘中序列需要改 UNIQUE 约束或加新表。

3. **外部参照价（Pinnacle de-vig）尚未接入** —— `ref_fair` 列恒为 NULL。
   接入后才能真正做"参照价 vs 市场价"检验。
   ⚠️ 实测 `api.pinnacle.com` / `www.pinnacle.com` **连接被重置**（不可达），
   需走第三方聚合（`api.the-odds-api.com` 可达但需 API key）。

4. **`n_new_obs` 计数会误导** —— 因为是 upsert，重复运行同一天会重复计数。
   实际新市场数要看 `COUNT(DISTINCT market_id)`。

---

## 当前状态

见 `polytrade_out/report_latest.txt`。
