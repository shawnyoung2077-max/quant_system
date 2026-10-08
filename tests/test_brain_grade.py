"""
test_brain_grade.py - BRAIN grade / 提交门槛 / 换手地板的回归测试

锁定的三个容易搞错的点：
  1. BRAIN 的 `grade` 字段【只看 Fitness】，与 Sharpe 无关；
     而"能否提交"还要额外过 Sharpe 门槛（IS 检查里的 LOW_SHARPE）。
  2. 地板只影响【低换手】策略：TO > floor 时地板【完全不起作用】。
     （本项目的 calibrate_floor.py 第一版就把这个方向搞反了。）
  3. 边界值：Fitness 恰好 1.5 / 2.0 时的分档归属。

运行：
    python -m unittest tests.test_brain_grade -v
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alpha_score import (brain_grade, brain_submittable, fitness_score,   # noqa: E402
                         turnover_floor, BRAIN_SUBMIT_SHARPE)


def _series(daily_mu, daily_vol, n=2000, seed=0):
    idx = pd.bdate_range("2019-01-01", periods=n)
    rng = np.random.default_rng(seed)
    # 用手工构造的序列，便于精确控制 mean/std
    r = pd.Series(rng.normal(daily_mu, daily_vol, n), index=idx)
    r = (r - r.mean()) / r.std() * daily_vol + daily_mu
    return r


class TestBrainGradeBoundaries(unittest.TestCase):

    def test_thresholds(self):
        self.assertEqual(brain_grade(3.0), "EXCELLENT")
        self.assertEqual(brain_grade(2.0), "EXCELLENT")   # 边界含
        self.assertEqual(brain_grade(1.99), "GOOD")
        self.assertEqual(brain_grade(1.5), "GOOD")        # 边界含
        self.assertEqual(brain_grade(1.49), "AVERAGE")
        self.assertEqual(brain_grade(1.0), "AVERAGE")     # 边界含
        self.assertEqual(brain_grade(0.99), "INFERIOR")
        self.assertEqual(brain_grade(-5.0), "INFERIOR")

    def test_grade_ignores_sharpe(self):
        """★ BRAIN grade 只看 Fitness —— 低 Sharpe 也可能 GOOD。"""
        self.assertEqual(brain_grade(1.7), "GOOD")

    def test_submittable_requires_both(self):
        """★ 但"可提交"必须同时过 Fitness 与 Sharpe 两个门槛。"""
        # Fitness 够 GOOD，Sharpe 不够
        self.assertFalse(brain_submittable(1.7, BRAIN_SUBMIT_SHARPE - 0.1))
        # 两个都够
        self.assertTrue(brain_submittable(1.7, BRAIN_SUBMIT_SHARPE))
        # Sharpe 够但 Fitness 只是 AVERAGE
        self.assertFalse(brain_submittable(1.2, 3.0))

    def test_excellent_passes(self):
        self.assertTrue(brain_submittable(2.5, 2.0))


class TestTurnoverFloorDirection(unittest.TestCase):
    """★ 地板只惩罚低换手；高换手与地板无关。"""

    def setUp(self):
        self.r = _series(0.0004, 0.01)

    def _f(self, to, floor):
        return fitness_score(self.r, pd.Series(to, index=self.r.index),
                             ppy=252, turnover_floor=floor)

    def test_high_turnover_ignores_floor(self):
        """TO > 所有地板时，不同地板给出完全相同的 Fitness。"""
        for to in (1.0, 2.0, 5.0):
            vals = [self._f(to, fl) for fl in (0.125, 0.33, 0.5)]
            self.assertAlmostEqual(vals[0], vals[1], places=12)
            self.assertAlmostEqual(vals[1], vals[2], places=12)

    def test_low_turnover_is_penalised_by_higher_floor(self):
        """TO < floor 时，地板越高 Fitness 越低（是被惩罚，不是被宽容）。"""
        f_low = self._f(0.05, 0.125)
        f_high = self._f(0.05, 0.33)
        self.assertGreater(f_low, f_high,
                           "更高的地板应当【降低】低换手策略的 Fitness")

    def test_floor_equals_turnover_at_boundary(self):
        """TO == floor 时，地板恰好不起作用。"""
        self.assertAlmostEqual(self._f(0.125, 0.125), self._f(0.125, 0.05),
                               places=12)


class TestTurnoverFloorCalibration(unittest.TestCase):
    """floor ∝ σ / cost（可忽略门槛 = 波动/成本，量纲正是换手率）"""

    def test_higher_cost_lowers_floor(self):
        """成本越高 → σ/cost 越小 → 可忽略门槛越低 → 地板越低。"""
        self.assertLess(turnover_floor(30.0, 0.02), turnover_floor(10.0, 0.02))

    def test_higher_vol_raises_floor(self):
        """波动越高 → σ/cost 越大 → 门槛越高 → 地板越高（对低换手更严）。"""
        self.assertGreater(turnover_floor(14.0, 0.05), turnover_floor(14.0, 0.01))

    def test_crypto_vol_raises_floor_vs_equity(self):
        """★ 加密日波动(≈4.4%)远高于股票(≈1.2%) ⇒ 地板应高于 0.125。"""
        f = turnover_floor(14.0, 0.0442)
        self.assertGreater(f, 0.125,
                           "加密地板应高于 BRAIN 股票的 0.125（中性假设下 ≈0.33）")
        self.assertAlmostEqual(f, 0.3286, places=2)

    def test_degenerate_inputs_fall_back(self):
        self.assertEqual(turnover_floor(14.0, 0.0), 0.125)
        self.assertEqual(turnover_floor(0.0, 0.02), 0.125)


class TestFitnessFormula(unittest.TestCase):
    """Fitness 公式本身（地板固定时）"""

    def setUp(self):
        self.r = _series(0.0004, 0.01)

    def test_sharpe_is_scale_invariant_but_fitness_is_not(self):
        """
        ⚠️ 一个容易误以为成立的性质：**Fitness 不是尺度不变的。**

        Sharpe 确实严格尺度不变（分子分母同时 ×k）。
        但 annualized_return 用的是**几何**平均 `exp(mean(log1p(r))·ppy)-1`，
        把收益整体放大 k 倍后，几何年化【不是** k 倍**】，而是非线性增长。
        所以 Fitness = Sharpe·sqrt(|ret|/TO) 只随尺度单调上升，不成比例。

        这条值得单独测：如果哪天有人把 annualized_return 改成算术平均，
        这个测试会失败 —— 而那正是一个需要被注意的行为变更。
        """
        from alpha_score import sharpe_ratio, annualized_return
        to = pd.Series(0.5, index=self.r.index)
        f1 = fitness_score(self.r, to, 252)
        f2 = fitness_score(self.r * 3, to, 252)

        # Sharpe 严格不变
        self.assertAlmostEqual(sharpe_ratio(self.r, 252),
                               sharpe_ratio(self.r * 3, 252), places=10)
        # 几何年化不是 3 倍
        self.assertNotAlmostEqual(annualized_return(self.r * 3, 252),
                                  3 * annualized_return(self.r, 252), places=6)
        # Fitness 单调上升，但明显小于 sqrt(3) 倍
        self.assertGreater(f2, f1)
        self.assertLess(f2 / f1, np.sqrt(3))

    def test_zero_turnover_uses_floor(self):
        """零换手时地板接管分母，不应出现除零。"""
        f = fitness_score(self.r, pd.Series(0.0, index=self.r.index), 252)
        self.assertTrue(np.isfinite(f))


if __name__ == "__main__":
    unittest.main(verbosity=2)
