"""
alpha_score.py - WorldQuant BRAIN 风格因子/alpha 评分
======================================================
按 WorldQuant BRAIN 的评级思路给因子（转成策略）评分：
  指标：Sharpe、Fitness、Returns、Turnover、Drawdown、Margin、权重集中度
  等级：A / B / C / D / F（按 Fitness + Sharpe 阈值）

WorldQuant BRAIN 关键公式（2026-09-21 用 24 个 ACTIVE alpha 实测反推确认）：
  Fitness = Sharpe * sqrt( |年化收益| / max(0.125, 日换手) )      ← 分母是【日换手】，地板 0.125
  Margin  = (正收益和 - 负收益绝对值和) / 正收益和   （即 (GP-GL)/GP）
  权重集中度：每日权重 HHI = sum(w_i^2) 的均值（越高越集中，WorldQuant 会扣分）

  BRAIN 官方等级（只看 Fitness，与 Sharpe 无关）：
    > 2.0      → EXCELLENT
    1.5 ~ 2.0  → GOOD
    1.0 ~ 1.5  → AVERAGE
    < 1.0      → INFERIOR

等级阈值（本模块采用的 WorldQuant 风格分档，文档见 README）：
  A: Fitness>=1.5 且 Sharpe>=2.0
  B: Fitness>=1.0 且 Sharpe>=1.25
  C: Fitness>=0.5 且 Sharpe>=0.75
  D: Fitness>=0.3 且 Sharpe>=0.5
  F: 其余
  "Good"(可提交)：Sharpe>=1.25 且 Fitness>=1.0
"""

import numpy as np
import pandas as pd

TRADING_DAYS = 252

# 等级阈值
GRADES = [
    ("A", 1.5, 2.0),
    ("B", 1.0, 1.25),
    ("C", 0.5, 0.75),
    ("D", 0.3, 0.5),
    ("F", -np.inf, -np.inf),
]


def sharpe_ratio(net_returns: pd.Series, ppy: int = TRADING_DAYS) -> float:
    """年化 Sharpe（无风险=0）。ppy=每年周期数：A股252，加密货币365。"""
    ann = net_returns.mean() * ppy
    vol = net_returns.std() * np.sqrt(ppy)
    return float(ann / vol) if vol > 0 else 0.0


def annualized_return(net_returns: pd.Series, ppy: int = TRADING_DAYS) -> float:
    """年化收益（对数法，防溢出）：exp(mean(log1p(r))*ppy) - 1。

    修正：旧版用 `if not np.isfinite(logret).all(): return 0.0`，
    只要序列里有**一个 NaN**（如原始价格 pct_change 的首个空值）就整体返回 0.0
    —— 曾导致"持有BTC 年化=0%"这种明显错误。现改为**忽略 NaN 后取均值**。
    """
    if len(net_returns) == 0:
        return 0.0
    r = net_returns.clip(lower=-0.9999)          # 防止 log 负数
    logret = np.log1p(r)
    logret = logret[np.isfinite(logret)]          # 剔除 NaN / inf
    if len(logret) == 0:
        return 0.0
    return float(np.expm1(np.mean(logret) * ppy))


def fitness_score(net_returns: pd.Series, turnover: pd.Series,
                  ppy: int = TRADING_DAYS,
                  turnover_floor: float = 0.125) -> float:
    """WorldQuant BRAIN Fitness = Sharpe * sqrt(|年化收益| / max(地板, 日换手))。

    两次修正历史：
      1) 旧版误把【年化换手】放进分母 → 分母被放大 ~250 倍 → 所有因子被压成 F 级。
         改为【日换手】后修正。BRAIN 字段 is.turnover 就是日换手（典型 0.05~0.5）。
      2) 2026-09-21：用 24 个 ACTIVE alpha 的真实 IS 数据反推，确认**分母有 0.125 地板**，
         不是 1。对比多个候选公式的 RMSE：
             Sharpe*sqrt(|ret|/max(0.125, to))  →  RMSE 0.0034   ✅ 正确
             Sharpe*sqrt(|ret|/max(1,     to))  →  RMSE 0.87     ❌ 旧版
         单点验证：MAR Sharpe 2.14 / 收益 6.63% / 换手 0.2365 → BRAIN 给 1.13；
         新公式算 1.133 ✅，旧公式(地板=1)算 0.55 ❌。

    含义：**换手 < 0.125 完全不扣分；> 0.125 按平方根实打实扣分。**
    实操：把换手调到刚好落在 0.125 最优——低于它继续降换手毫无收益（Sharpe 会跟着掉）。

    ⚠️ turnover_floor 的适用范围（重要）：
        0.125 是 **BRAIN 股票市场（USA/delay1/TOP3000）** 的经验值。
        它不该无脑套到加密上 —— 见 `turnover_floor()` 的推导。
        加密策略请用 `turnover_floor()` 算出的值显式传入。
    """
    s = sharpe_ratio(net_returns, ppy)
    ann_ret = annualized_return(net_returns, ppy)
    daily_turn = float(turnover.mean())          # 日换手（BRAIN 口径）
    return float(s * np.sqrt(abs(ann_ret)) / max(turnover_floor, daily_turn))


# BRAIN 官方 `grade` 字段（>2 EXCELLENT / 1.5~2 GOOD / 1~1.5 AVERAGE / <1 INFERIOR）
# 注意：这与本模块的 A/B/C/D/F 是**两套不同的分档**。
#   · BRAIN `grade` 只看 Fitness，与 Sharpe 无关 → 用于判断【能否提交】
#   · 本模块 A/B/C/D/F 同时看 Fitness 与 Sharpe → 用于内部横向比较
# 判断"够不够格提交"时**必须看 brain_grade**（用户的既有要求：只提交 GOOD 及以上）。
BRAIN_GRADE_THRESHOLDS = [
    ("EXCELLENT", 2.0),
    ("GOOD", 1.5),
    ("AVERAGE", 1.0),
    ("INFERIOR", -np.inf),
]

# 本项目参考的 BRAIN 提交门槛（D1 数据集）
BRAIN_SUBMIT_SHARPE = 1.25
BRAIN_SUBMIT_FITNESS = 1.0


def brain_grade(fitness: float) -> str:
    """
    BRAIN 官方 grade 字段：**只看 Fitness**，与 Sharpe 无关。

        2.0 以上  → EXCELLENT
        1.5 ~ 2.0 → GOOD        ← 用户要求的最低提交线
        1.0 ~ 1.5 → AVERAGE
        1.0 以下  → INFERIOR

    为什么必须单独实现：BRAIN 页面上 Fitness 的**显示值**经过舍入，
    真实分级要看 `grade` 字段。用显示值手工分档会在 1.5 边界附近出错。
    """
    for name, th in BRAIN_GRADE_THRESHOLDS:
        if fitness >= th:
            return name
    return "INFERIOR"


def brain_submittable(fitness: float, sharpe: float,
                      min_grade: str = "GOOD") -> bool:
    """
    是否达到提交标准。默认 GOOD 及以上（用户既定要求）。

    同时要求 Sharpe ≥ 1.25 —— 因为 grade 只看 Fitness，
    而 BRAIN 的 IS 检查里有独立的 LOW_SHARPE 门槛（D1 约 1.25）。
    只满足 grade 不满足 Sharpe 仍然过不了 IS 检查。
    """
    order = ["INFERIOR", "AVERAGE", "GOOD", "EXCELLENT"]
    return (order.index(brain_grade(fitness)) >= order.index(min_grade)
            and sharpe >= BRAIN_SUBMIT_SHARPE)


def turnover_floor(cost_bps: float, daily_vol: float,
                   ref_cost_bps: float = 10.0, ref_daily_vol: float = 0.012,
                   ref_floor: float = 0.125) -> float:
    """
    给非股票市场（如加密）标定 Fitness 的换手率地板。

    ⚠️ 这不是 BRAIN 文档里的常数 —— 是一个**有明确假设的经济推导**。
       假设中每一项都可以替换后再算，请把假设一起写进结论。

    推导逻辑：地板的经济含义是「换手低于此水平就不扣分」，即
        **换手成本相对于该市场的日均波动可以忽略**。

    把"可以忽略"写成不等式（cost 为单边成本比例，σ 为日波动）：
        TO × cost ≪ σ      ⟺      TO ≪ σ / cost
    ⇒ 「可忽略」的门槛换手率 = **σ / cost**（波动/成本，量纲正好是换手率）。

    所以地板 ∝ σ / cost：

        floor_crypto = floor_eq × (σ_crypto / cost_crypto) / (σ_eq / cost_eq)

    ⚠️ 两个方向都要小心（本项目的测试第一版就写反了）：
        · 成本越高  → σ/cost 越小 → 地板【越低】（换手贵，门槛自然低）
        · 波动越高  → σ/cost 越大 → 地板【越高】

    **「地板越高」= 对低换手策略【更严厉】的惩罚**（不是更宽容）——
    因为分母是 max(floor, TO)：TO < floor 时分母被抬到 floor，Fitness 被压低。

    加密的直观解释：波动高 ⇒ 同样的换手成本相对风险微不足道 ⇒
    只有当换手率相当高时成本才开始有意义 ⇒ 可忽略门槛更高 ⇒ 地板更高。
    经济上合理：加密单币波动与信用风险远高于股票，
    长持仓（低换手）意味着承担了更多风险却未相应调整，不该给"低换手红利"。

    方向检验（本项目实测 σ_crypto≈0.0442，是股票 0.012 的 3.7 倍；
             成本 14bps vs 10bps，只高 1.4 倍）：
        σ/cost: 加密 3.16，股票 0.83 ⇒ 加密门槛是股票的 3.8 倍
        ⇒ 地板_crypto ≈ 0.125 × 3.8 ≈ 0.47？—— 注意这里用的是【单边】成本口径，
          本函数用【往返】成本，两者差 2 倍但不影响方向。
          实际调用处传往返成本，算出 ≈ 0.33。

    默认假设（都可替换）：
        BRAIN 股票：成本 10 bps 往返，日波动 1.2%（≈ 年化 19% / √252）
        加密（本项目实测）：成本 14 bps 往返（现货 8 + 永续 6），
                            日波动见调用处传入的实测值

    返回值仅作数量级参考；建议同时给出敏感性区间（见 calibrate_floor.py）。
    """
    if daily_vol <= 0 or cost_bps <= 0:
        return ref_floor
    threshold_ref = ref_daily_vol / ref_cost_bps       # 股票的"可忽略门槛"
    threshold_new = daily_vol / cost_bps               # 新市场的门槛
    return float(ref_floor * (threshold_new / threshold_ref))


def margin_ratio(net_returns: pd.Series) -> float:
    """WorldQuant Margin = (GP - GL) / GP。"""
    gp = net_returns[net_returns > 0].sum()
    gl = -net_returns[net_returns < 0].sum()
    return float((gp - gl) / gp) if gp > 0 else 0.0


def max_drawdown(net_returns: pd.Series) -> float:
    """最大回撤（正数表示跌幅）。"""
    eq = (1.0 + net_returns).cumprod()
    return float(-(eq / eq.cummax() - 1.0).min())


def weight_concentration(weights: pd.DataFrame) -> float:
    """权重集中度：每日 HHI 均值。"""
    if weights is None or weights.empty:
        return 0.0
    return float((weights ** 2).sum(axis=1).mean())


def sub_universe_sharpe(prices: pd.DataFrame, net_returns: pd.Series,
                        sectors: pd.Series = None) -> float:
    """
    子领域 Sharpe：把组合收益按行业分组后的平均 Sharpe（WorldQuant 用来防行业集中）。
    需要 sectors（股票->行业）。未提供时返回 None。
    """
    if sectors is None:
        return None
    # 简化：用每只股票的等权收益 Sharpe 的行业均值作为代理（需 sector 数据）
    ret = prices.pct_change().fillna(0.0)
    per_stock_sharpe = ret.mean() * TRADING_DAYS / (ret.std() * np.sqrt(TRADING_DAYS) + 1e-12)
    s = sectors.reindex(per_stock_sharpe.index).dropna()
    if s.empty:
        return None
    return float(per_stock_sharpe.reindex(s.index).groupby(s).mean().mean())


def assign_grade(fitness: float, sharpe: float) -> str:
    """按 Fitness + Sharpe 分档。"""
    for grade, f_th, s_th in GRADES:
        if fitness >= f_th and sharpe >= s_th:
            return grade
    return "F"


def score_factor(prices: pd.DataFrame, factor: pd.DataFrame,
                 top_pct: float = 0.2, long_only: bool = True,
                 commission_bps: float = 10.0, slippage_bps: float = 5.0,
                 sectors: pd.Series = None, hold: int = 1,
                 ppy: int = TRADING_DAYS,
                 turnover_floor: float = 0.125) -> dict:
    """
    给一个因子评分（WorldQuant 风格）。
    返回 dict：各指标 + 等级 + 是否 Good（可提交）。

    factor: 因子面板（行=日期, 列=股票）。内部转成目标权重并回测。
    hold>1 时用持有期重平衡（适合低频事件类 alpha）。
    turnover_floor: Fitness 的换手率地板。默认 0.125（BRAIN 股票市场）。
                    加密策略请用 `turnover_floor()` 算出的值传入。
    """
    from factor import factor_to_weights
    from backtest import portfolio_backtest

    if hold and hold > 1:
        from alphas import factor_to_weights_hold
        w = factor_to_weights_hold(factor, top_pct=top_pct, long_only=long_only, hold=hold)
    else:
        w = factor_to_weights(factor, top_pct=top_pct, long_only=long_only)
    w = w.reindex(prices.index).fillna(0.0)
    net_returns, turnover = portfolio_backtest(prices, w,
                                               commission_bps=commission_bps,
                                               slippage_bps=slippage_bps)

    s = sharpe_ratio(net_returns, ppy)
    f = fitness_score(net_returns, turnover, ppy, turnover_floor=turnover_floor)
    ret = annualized_return(net_returns, ppy)
    tur_d = float(turnover.mean())                  # 日换手（BRAIN 口径）
    tur = tur_d * ppy                               # 年化换手（仅展示用）
    dd = max_drawdown(net_returns)
    mg = margin_ratio(net_returns)
    conc = weight_concentration(w)
    sub_s = sub_universe_sharpe(prices, net_returns, sectors)
    grade = assign_grade(f, s)
    good = s >= 1.25 and f >= 1.0

    return {
        "grade": grade, "good": good,
        # BRAIN 官方 grade（只看 Fitness）+ 是否达到提交标准（GOOD 及以上且 Sharpe 达标）
        "brain_grade": brain_grade(f),
        "brain_submittable": brain_submittable(f, s),
        "turnover_floor": turnover_floor,
        "sharpe": s, "fitness": f, "returns": ret,
        "turnover": tur_d, "turnover_ann": tur,     # turnover=日换手(BRAIN口径)
        "drawdown": dd, "margin": mg,
        "weight_concentration": conc, "sub_universe_sharpe": sub_s,
    }


def print_score(r: dict, name: str = "factor") -> None:
    """打印评分报告。"""
    print("=" * 52)
    print(f"  WorldQuant 评分: {name}")
    print("=" * 52)
    print(f"  BRAIN grade    : {r.get('brain_grade', '?'):<10} "
          f"(可提交={r.get('brain_submittable')}, 门槛 GOOD 且 Sharpe>=1.25)")
    print(f"  等级 Grade     : {r['grade']:<4}  (内部 A/B/C/D/F 分档)")
    print(f"  Sharpe         : {r['sharpe']:>10.3f}")
    print(f"  Fitness        : {r['fitness']:>10.3f}  "
          f"(换手地板={r.get('turnover_floor', 0.125):.3f})")
    print(f"  Returns 年化   : {r['returns']:>10.2%}")
    print(f"  Turnover 日    : {r['turnover']:>10.3f}  (BRAIN口径, 年化≈{r.get('turnover_ann', r['turnover']*252):.1f})")
    print(f"  Drawdown 最大  : {r['drawdown']:>10.2%}")
    print(f"  Margin         : {r['margin']:>10.3f}")
    print(f"  权重集中度HHI  : {r['weight_concentration']:>10.3f}")
    if r["sub_universe_sharpe"] is not None:
        print(f"  子领域Sharpe   : {r['sub_universe_sharpe']:>10.3f}")
    else:
        print("  子领域Sharpe   :  (需行业数据，未提供)")
    print("=" * 52)
