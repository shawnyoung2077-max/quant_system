# 联赛覆盖缺陷：扫描器看不到 Premier League

日期：2026-10-10
影响：实盘 100% 押在证据基础之外的联赛，导致第一轮结果无法解释。
状态：已修复。抓取市场数 7,012 → 36,088（5.1×）。

---

## 1. 症状

实盘 56 笔已结算注全部落在冷门联赛（荷乙 22、法乙 10、巴拉圭 12、卡塔尔 6…），
而这些联赛在支撑策略的历史数据集里几乎不存在（荷乙本档 **0 行**）。

一开始我以为是"策略选了冷门联赛"，准备加白名单过滤。
但先做了一次覆盖度检查，发现**大联赛根本没被观测到**：

```
Premier League      观测 0 行
LaLiga              观测 54 行（全是 LaLiga2）
Bundesliga          观测 86 行（全是 Austria / Basketball / Handball）
Ligue 1             观测 0 行
Eredivisie          观测 0 行
UEFA Europa League  观测 0 行
MLS / Liga MX       观测 0 行
```

这不是市场事实 —— Polymarket 上这些联赛显然有市场。

## 2. 根因 A：联赛列表被按"任意顺序"截断

`scan.py`：

```python
tags = tags[:C.MAX_LEAGUE_TAGS]        # MAX_LEAGUE_TAGS = 200
```

catalog 共 **473** 个联赛，这个切片取的是**数组前 200 个**，
而数组顺序 ≈ 收录时间，**没有任何优先级含义**
（`ordering` 字段只有 `home`/`away`，不是排序权重）。

结果：

| 抓到了 | 漏掉了 |
|---|---|
| UFL、Plunket Shield、National T20 Cup、ECS Switzerland、Maharani Trophy、JCL T10、Pondicherry PL、NCAA Lacrosse | **Premier League(#466)、UEFA Europa League(#450)、MLS(#423)、Serie A(#436)、Liga MX(#223)、A-League Men(#361)、UFC、NHL、NBA** |

修复：`MAX_LEAGUE_TAGS = 0` 表示抓全部。耗时 124 秒，周期内可接受。

## 3. 根因 B：单 tag 只取首页，漏掉 5/6

修复 A 之后，每个大联赛都恰好返回 **100** 条 —— 正好卡在 `MARKETS_PER_TAG` 上。

实测：

```
GET /markets?limit=100&tag_id=306   -> 100 条
GET /markets?limit=500&tag_id=306   -> 100 条   ← 完全相同
GET /markets?limit=100&offset=0..500&tag_id=306 -> 600 个不同市场
```

**单次请求最多返回 100 条**（limit=500 无效），但 Premier League 一个 tag 就有 600+ 个未结算市场。
即 offset 分页有效、limit 无效。

修复：`MAX_PAGES_PER_TAG = 3`，**按需翻页** —— 某页返回不足 100 条即说明到底，立即停止。

## 4. 效果

| | 修复前 | 修复后 |
|---|---|---|
| 抓到市场 | 7,012 | **36,088** |
| 下注候选 | 72 | **272** |
| 落在 1/3/7 天窗口内的候选 | 4 | **43** |
| 单次运行耗时 | 103 s | 288 s |
| 7 天档样本 | **从未有过** | 已产生 5 笔 |

修好后立刻产生了证据内样本（`valid` 线）：A-League Men、Brasileirão Série A、LaLiga2。

## 5. 教训

**"没有数据"和"数据是 0"看起来一模一样。**

这两个 bug 都不会报错，都会让系统"正常运行"，
只是把观测集合悄悄换成了一批无关的联赛。
排查的第一步不该是问"策略对不对"，而是问
**"我的数据里到底有没有我要找的东西？"** ——
也就是拿一个**已知应该存在的对象**（Premier League）去查它在不在。

这与结算那个 bug 是同一类：`docs/DEBUG_settlement_gamma_api.md`。
当时的教训是"拿已知答案的样本去手测链路"，这里是"拿已知存在的对象去测覆盖率"。
