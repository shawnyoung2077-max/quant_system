# 协作交接说明：Codex ↔ DeepSeek（2026-10-15）

给任何一个接手的人（人或模型）看的第一份文件。
目的是：**谁做了什么、什么被验证过、什么还没验证、下一步归谁**，一目了然。

---

## 1. 本次协作的分工

| 提交 | 谁 | 内容 |
| --- | --- | --- |
| `3c8411c` | DeepSeek | 代理自愈缺口（委托交互式任务）、GBK stdout 崩溃、settle 死磕 |
| `ab3b176` | **Codex** | 策略候选先于宽扫抓取、结算去重与预算、移除空类型 |
| （本文件所在提交） | DeepSeek | 核心候选数量守卫、简报分级修正、实验判定规则登记 |

DeepSeek 在提交 Codex 的改动**之前**独立复核过，未做任何修改直接落盘，
以免后续编辑覆盖掉 Codex 的工作。

---

## 2. Codex 改动的复核结果（DeepSeek 独立复核，不是转述）

**结论：改动属实、方向正确、可以合入。**

- `118 tests OK`、`compileall exit=0`、`git diff --check exit=0` —— 复现无误。
- 曾怀疑一个致命问题：`settle.py` 调用 `assert_network(tries=1, timeout=...)`，
  而该函数签名原本是 `assert_network(verbose=False)` —— 若签名没改会是 `TypeError`，
  且被外层 `except Exception` 静默吞掉 ⇒ **结算永远跳过**。
  **实测：Codex 已同步改签名为 `(verbose=False, tries=2, timeout=15)`，调用成功。** 虚惊一场。
- 直接打真实 gamma API 验证核心参数确实生效：
  LaLiga tag=780 用核心全参数返回 **63 个候选，63/63 全部落在窗口内**；
  `sports_market_types=moneyline` 精确过滤（50/50 全为 moneyline）。

---

## 3. 复核中发现、由 DeepSeek 补掉的（本次提交）

### 3.1 核心抓取「静默变空」会让系统一切正常地停止下注 ★最严重

实测 gamma 的行为：

```
sports_market_types=moneyline -> n=20（正常）
sports_market_types=zzz       -> n=0   （不是报错，是空列表）
zzzparam=1（未知参数名）       -> 与不传一样（被静默忽略）
```

而 `fetch_core` 把「空列表」与「正常抓到底」返回成同一个 `issue=None`，
于是 `FETCH_CORE_COMPLETE` 照样为 `True`、简报一切正常 —— **但永远没有下注候选**。

补法：新增 `C.CORE_MIN_CANDIDATES`（默认 20，故意定得很低以免假警报），
核心候选低于阈值时记 `core_empty` 并**禁止本轮下注**。
理由：只剩几个候选还照常下注，等于在一个**有偏的小子集**上下注，比不下注更糟。

> 这个阈值只用来抓「整块塌掉」的灾难，**不是覆盖率达标线**。
> 正常一天核心候选是几百到上千个。

### 3.2 简报告警分级（恢复了被删掉的理由）

上一版把 note 里任何内容都判成 `[警告]`。但 `page_cap`（实测每轮 50+ 联赛打满）
和 `broad_snapshot_partial` 是**每轮必然出现**的 ⇒ 简报每轮报警。
被删掉的那段原注释恰好就在警告这件事：**「告警变成噪音、人开始无视它，
而那正是静默少采能藏两天的土壤」**。

现在的分级（原则：**只有「决策样本本身不可信」才是严重**）：

| 等级 | 触发 | 含义 |
| --- | --- | --- |
| 严重 | `core_incomplete` / `core_empty` | 下注候选抓取残缺，本轮的「没下注」不能当策略信号读 |
| 警告 | `core_api_errors` | 核心抓取遇 API 错误（可能偶发） |
| 提示 | `page_cap` / `broad_snapshot_partial` / `time_budget` | 只影响宽口径观测完整度，不影响下注决策 |

### 3.3 把三条 API 硬约束写进 `config.py`

因为它们是**静默的隐式耦合**，后人一「简化」就会炸：

1. `child_moneyline` / `first_half_moneyline` 作为过滤值实测 **n=0**，是**死值**
   （字段可能出现该取值，但不是合法的过滤取值）。
   历史上有 6 笔注的 market_type 是 `child_moneyline`（$12）——
   这些市场以后不会再被抓到、也就不会再下注。
2. `end_date_min/max` **单独使用会 HTTP 500**；只有与 `sports_market_types`
   同时传才正常。所以 `fetch_core` 里那两行不是冗余，**删不得**。
3. **未知参数名会被静默忽略** ⇒ 任何新加的过滤参数都必须用真实 API 验证
   它**确实改变了返回集**，不能「加上了就以为生效了」。

---

## 4. 交给 Codex 的待办（按价值排序）

- [ ] **12:05 那次运行的实测核对**：看 `runs.note` 有没有 `core_empty`、
      `core_candidates` 是多少、`bet_candidates` 与改动前（1512）相比如何。
      这是核心抓取在真实全量下的第一次验证 —— **在此之前不要宣称改动已生效**。
- [ ] **考虑给宽扫也加页数预算**：现在核心 + 宽扫是两遍 473 个 tag，
      请求数近乎翻倍。若观测数明显下降，需要在 `FETCH_TIME_BUDGET_SEC`
      与覆盖率之间重新取舍，并把结论写进 `runs.note`。
- [ ] **观测历史不可当全集**：10-09 只有 1,010 个、10-11 只有 2,533 个观测，
      而 10-15 是 14,833 个。任何基于历史观测的统计都必须先声明这一点。
- [ ] 技术债：`modeling/model_calibration.py` 里 `lead_days == 3` 写死；
      `brier_skill_cluster_bootstrap_95ci` 命名与实现不符。
- [ ] （较大）**并跑一条基准线**：无条件买同档 YES。
      没有它就无法区分「策略有用」与「市场整体变了」，
      而这会直接削弱最终判定结论的强度（见 `EXPERIMENT_stopping_rule.md` 第 5 节）。

---

## 5. 交给用户拍板的（不要自行决定）

- **要不要提高敞口上限以加快样本积累**：`MAX_EXPOSURE_PCT` 10% = $300，
  当前已占用 $266 —— 瓶颈已经从「没机会」变成「等结算」。
  代价是回撤风险近似线性上升，与「稳健不求暴富」冲突。
- 或 `STAKE` 由 $2 降到 $1：日波动 1.29% → 0.91%，可容纳更多并发，
  但验证同样需要更多笔数。
- 是否要按 `docs/EXPERIMENT_stopping_rule.md` 的规则执行到底（含「到点必须下结论」）。

---

## 6. 本仓库现有的事故复盘（都带实测数据）

| 文档 | 主题 |
| --- | --- |
| `DEBUG_clash_singleton_session0.md` | 点 Clash 图标无窗口：会话 0 拉 GUI 抢走单例锁；以及后续改为「委托交互式任务」 |
| `DEBUG_run_timeout_30min.md` | 整轮被计划任务 30 分钟硬杀；`match.run_match` 的 O(N×M) 无上限循环 |
| `DEBUG_proxy_outage_36h.md` | 代理静默死 36 小时 |
| `DEBUG_settlement_gamma_api.md` | 结算恒为 0（gamma 隐式 `closed=false`） |
| `DEBUG_league_coverage.md` | 联赛覆盖被 `tags[:200]` 静默截断 |
| `EXPERIMENT_stopping_rule.md` | **能不能赚钱的预先登记判定规则** |
| `FINDING_evidence_transportability.md` | 证据可迁移性 |
| `FINDING_drawdown_25pct.md` | 「25% 回撤」是分母错觉 |
| `FINDING_position_sizing.md` | 仓位与日波动 |

**共同教训（出现 5 次以上）**：这个系统几乎每一次严重故障都是**静默**的 ——
失败与成功看起来一样。所以本仓库的约定是：
任何"少做了某件事"都必须落库、必须在打开简报时就看得见。
