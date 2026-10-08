"""
test_tail_risk.py - 尾部压力测试的回归测试

重点锁定本次修掉的两个真实 bug（都会被"看起来合理"的结果掩盖）：

  bug 1  s3_frozen_spot 的 gap_pct 被【按天重复计了 days 次】
         → 30 天 −20% 的组合算出 +600% 收益、区间年化 8.4e+30
         → 正确：一次性跳变只计【一天】

  bug 2  reverse_stress 的二分方向写反
         → 所有目标都返回边界值 −1.0（年化 −36500%），看起来"有结果"但全错
         → 正确：费率越负 → 亏损越大 ⇒ got(mid) < target 时应把费率往上调

这两类 bug 的共同特征：**不报错、不崩，只是给出一个荒谬但格式正确的数字。**
所以必须有断言把它们钉死。

运行：
    python -m unittest tests.test_tail_risk -v
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tail_risk as TR   # noqa: E402


def _panel(vals, cols, T, idx):
    if np.ndim(vals) == 0:
        vals = np.full((T, len(cols)), float(vals))
    return pd.DataFrame(vals, index=idx, columns=cols)


class TestFrozenSpotOneTimeShock(unittest.TestCase):
    """bug 1：一次性跳变只能计一次"""

    def setUp(self):
        self.T = 200
        self.cols = ["A", "B"]
        self.idx = pd.bdate_range("2024-01-01", periods=self.T)
        # 价格恒定 → 若 gap_pct 走"按日重复"分支，extra 会随 days 线性放大
        self.spot = _panel(100.0, self.cols, self.T, self.idx)
        self.fund = _panel(1e-4, self.cols, self.T, self.idx)
        self.dv = _panel(1e9, self.cols, self.T, self.idx)
        # 等权，Σw = 1
        self.W = pd.DataFrame(np.full((self.T, 2), 0.5),
                              index=self.idx, columns=self.cols)

    def test_gap_is_applied_once_regardless_of_days(self):
        """★ 同一个跳变，冻结 5 天 vs 60 天，裸空贡献必须完全一样。"""
        out5 = TR.s3_frozen_spot(self.spot, self.fund, self.W, self.dv,
                                 days=5, gap_pct=-0.20)
        out60 = TR.s3_frozen_spot(self.spot, self.fund, self.W, self.dv,
                                  days=60, gap_pct=-0.20)
        self.assertAlmostEqual(float(out5["naked"].sum()), float(out60["naked"].sum()),
                               places=12,
                               msg="一次性跳变不能随冻结天数重复累加")
        # 跳变 −20%、Σw=1 → 裸空贡献应为 +0.20（空头受益）
        self.assertAlmostEqual(float(out5["naked"].sum()), 0.20, places=12)

    def test_gap_sign_is_short_exposure(self):
        """现货腿冻结 + 永续空头 ⇒ 净暴露 = −w ⇒ 跌赚涨亏。"""
        down = TR.s3_frozen_spot(self.spot, self.fund, self.W, self.dv,
                                 days=30, gap_pct=-0.40)
        up = TR.s3_frozen_spot(self.spot, self.fund, self.W, self.dv,
                               days=30, gap_pct=0.40)
        self.assertGreater(float(down["naked"].sum()), 0.0)
        self.assertLess(float(up["naked"].sum()), 0.0)

    def test_no_gap_uses_daily_returns_not_repeated_gap(self):
        """不给 gap_pct 时应按【日收益】累计，量级是正常的（不会爆炸）。"""
        o = TR.s3_frozen_spot(self.spot, self.fund, self.W, self.dv, days=30)
        # 价格恒定 → 日收益 0 → 裸空贡献 0
        self.assertAlmostEqual(float(o["naked"].sum()), 0.0, places=12)


class TestReverseStressDirection(unittest.TestCase):
    """bug 2：二分方向"""

    def setUp(self):
        self.T = 400
        self.cols = ["A", "B", "C"]
        self.idx = pd.bdate_range("2023-01-02", periods=self.T)
        self.fund = _panel(3e-4, self.cols, self.T, self.idx)
        self.dv = _panel(1e9, self.cols, self.T, self.idx)
        self.mask = pd.DataFrame(True, index=self.idx, columns=self.cols)
        self.W = pd.DataFrame(np.full((self.T, 3), 1 / 3.0),
                              index=self.idx, columns=self.cols)

    def test_loss_is_monotone_in_rate(self):
        """前置性质：费率越负，区间亏损越大（二分法成立的前提）。"""
        losses = []
        for rate in (-1e-4, -5e-4, -2e-3, -8e-3):
            o = TR.s1_parametric(self.fund, self.W, self.mask, self.dv, rate, 21)
            s = int(len(o) * 0.55)
            losses.append(float(o["net"].iloc[s:s + 21].sum()))
        self.assertEqual(losses, sorted(losses, reverse=True),
                         "费率越负亏损应越大；顺序错了说明模型有问题")

    def test_reverse_stress_hits_targets_not_boundary(self):
        """★ 反解出的费率必须【真的】实现目标亏损，而不是卡在边界。"""
        df = TR.reverse_stress(self.fund, self.W, self.mask, self.dv,
                               base_ann=0.10, days=21,
                               targets=(-0.05, -0.10))
        for _, row in df.iterrows():
            rate = float(row["需要日费率"])
            self.assertGreater(rate, -0.5,
                               "费率卡在边界(-1.0 附近)说明二分方向反了")
            target = float(row["目标区间亏损"].strip("%")) / 100.0
            o = TR.s1_parametric(self.fund, self.W, self.mask, self.dv, rate, 21)
            s = int(len(o) * 0.55)
            got = float(o["net"].iloc[s:s + 21].sum())
            self.assertAlmostEqual(got, target, delta=abs(target) * 0.35,
                                   msg="反解费率未能实现目标亏损 %.3f（实际 %.4f）"
                                       % (target, got))

    def test_looser_target_needs_less_negative_rate(self):
        """亏 20% 需要的费率必须比亏 5% 更负。"""
        df = TR.reverse_stress(self.fund, self.W, self.mask, self.dv,
                               base_ann=0.10, days=21,
                               targets=(-0.05, -0.20))
        r5 = float(df.iloc[0]["需要日费率"])
        r20 = float(df.iloc[1]["需要日费率"])
        self.assertLess(r20, r5)


class TestCreditEvent(unittest.TestCase):
    """信用事件的赔率计算"""

    def setUp(self):
        self.T = 50
        self.cols = ["A"]
        self.idx = pd.bdate_range("2024-01-01", periods=self.T)
        self.fund = _panel(1e-4, self.cols, self.T, self.idx)
        self.dv = _panel(1e9, self.cols, self.T, self.idx)
        self.W = pd.DataFrame(1.0, index=self.idx, columns=self.cols)

    def test_gross_notional_is_twice_capital(self):
        """毛名义 = 2×本金（现货+永续两条腿），全损 = 200% 本金。"""
        df = TR.s3_credit_event(self.fund, self.W, self.dv, base_ann=0.12,
                                loss_pcts=(1.0,))
        self.assertEqual(df.iloc[0]["占本金损失"], "200%")

    def test_full_loss_is_unrecoverable(self):
        """损失 ≥ 本金 ⇒ 无法回本（复利无法恢复）。"""
        df = TR.s3_credit_event(self.fund, self.W, self.dv, base_ann=0.12,
                                loss_pcts=(0.5, 1.0))
        self.assertEqual(df.iloc[1]["回本年数"], "无法回本")

    def test_loss_in_years_of_income(self):
        """100% 本所损失 = 200% 本金 = 200%/12% ≈ 16.7 年收益。"""
        df = TR.s3_credit_event(self.fund, self.W, self.dv, base_ann=0.12,
                                loss_pcts=(1.0,))
        yrs = float(df.iloc[0]["等于多少年 carry 收益"])
        self.assertAlmostEqual(yrs, 2.0 / 0.12, places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
