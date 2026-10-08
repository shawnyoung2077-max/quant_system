"""
test_neutral.py - BRAIN 式组合后处理原语的回归测试

锁定的两个【数学性质】（本模块实测发现，不是理论猜测）：
  性质 1  在等权组合上，「截断 → 归一化」是恒等变换，与 cap 取值无关。
  性质 2  cap < 1/n 时「Σ|w| = 1 且 |w_i| ≤ cap」不可行；此时必须按 cap 缩总暴露。

以及分组中性化的三条语义：
  demean  组内去均值 → 每组净暴露 0（⇒ 全组合 Σw = 0，天然 dollar-neutral）
  pair    组内多空配对 → 只留组内相对排序
  rank    组内秩变换 → 抗极值

运行：
    python -m unittest tests.test_neutral -v
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import neutral as NT   # noqa: E402


def _df(vals, cols, index=None):
    return pd.DataFrame([vals] * 3 if np.ndim(vals) == 1 else vals,
                        index=index or pd.RangeIndex(3), columns=cols)


class TestTruncateNormalize(unittest.TestCase):

    def test_truncate_caps_absolute_value_keeping_sign(self):
        w = _df([0.6, -0.5, 0.2], ["A", "B", "C"])
        t = NT.truncate_weights(w, 0.3)
        self.assertAlmostEqual(t["A"].iloc[0], 0.3)
        self.assertAlmostEqual(t["B"].iloc[0], -0.3)   # 符号必须保留
        self.assertAlmostEqual(t["C"].iloc[0], 0.2)    # 未超限不动

    def test_normalize_divides_by_gross_not_net(self):
        """多空组合 Σw=0，除以 Σ|w| 才安全；除以 Σw 会爆炸。"""
        w = _df([0.5, -0.5], ["A", "B"])
        n = NT.normalize_weights(w)
        self.assertAlmostEqual(float(n.abs().sum(axis=1).iloc[0]), 1.0)
        self.assertAlmostEqual(float(n.sum(axis=1).iloc[0]), 0.0)

    def test_equal_weight_truncate_then_normalize_is_identity(self):
        """★ 性质 1：等权组合上 截断→归一化 是恒等变换，与 cap 无关。"""
        eq = _df([0.25, 0.25, 0.25, 0.25], ["A", "B", "C", "D"])
        for cap in (0.30, 0.25, 0.10, 0.05, 0.02):
            got = NT.brain_pipeline(eq, max_weight=cap)
            np.testing.assert_allclose(got.to_numpy(), eq.to_numpy(), atol=1e-12,
                                       err_msg="cap=%.2f 时等权组合应不变" % cap)

    def test_brain_order_does_not_bound_final_weight(self):
        """★ BRAIN 顺序（截断后归一化）**不保证**最终权重 ≤ cap。"""
        w = _df([0.4, 0.3, 0.2, 0.1], ["A", "B", "C", "D"])
        out = NT.brain_pipeline(w, max_weight=0.25)
        self.assertGreater(float(out.abs().to_numpy().max()), 0.25,
                           "截断后归一化会把权重重新放大，上限被突破是预期行为")

    def test_strict_cap_actually_bounds(self):
        """strict 版必须真的满足 |w| ≤ cap。"""
        w = _df([0.4, 0.3, 0.2, 0.1], ["A", "B", "C", "D"])
        for cap in (0.30, 0.25, 0.20):
            out = NT.cap_and_normalize(w, cap)
            self.assertLessEqual(float(out.abs().to_numpy().max()), cap + 1e-9,
                                 "cap=%.2f 未被满足" % cap)

    def test_infeasible_cap_reduces_gross_not_violates_cap(self):
        """★ 性质 2：cap < 1/n 时不可行 → 缩总暴露（留现金），绝不违反 cap。"""
        eq = _df([0.25, 0.25, 0.25, 0.25], ["A", "B", "C", "D"])   # n=4
        cap = 0.05                                                  # < 1/4
        self.assertFalse(NT.is_feasible(gross=1.0, n_names=4, max_weight=cap))
        out = NT.cap_and_normalize(eq, cap)
        self.assertLessEqual(float(out.abs().to_numpy().max()), cap + 1e-9)
        self.assertAlmostEqual(float(out.abs().sum(axis=1).iloc[0]), 4 * cap, places=9,
                               msg="不可行时毛暴露应恰为 n·cap = 0.20")

    def test_feasible_cap_keeps_full_gross(self):
        eq = _df([0.25, 0.25, 0.25, 0.25], ["A", "B", "C", "D"])
        self.assertTrue(NT.is_feasible(gross=1.0, n_names=4, max_weight=0.25))
        out = NT.cap_and_normalize(eq, 0.25)
        self.assertAlmostEqual(float(out.abs().sum(axis=1).iloc[0]), 1.0, places=9)


class TestGroupNeutralize(unittest.TestCase):

    def setUp(self):
        self.w = _df([0.4, 0.3, 0.2, 0.1], ["A", "B", "C", "D"])
        self.g = _df([0, 0, 1, 1], ["A", "B", "C", "D"])

    def test_demean_makes_each_group_net_zero(self):
        out = NT.neutralize_groups(self.w, self.g, mode="demean")
        self.assertAlmostEqual(float(out[["A", "B"]].sum(axis=1).iloc[0]), 0.0)
        self.assertAlmostEqual(float(out[["C", "D"]].sum(axis=1).iloc[0]), 0.0)
        self.assertAlmostEqual(float(out.sum(axis=1).iloc[0]), 0.0,
                               msg="组内去均值 ⇒ 全组合 Σw = 0（天然 dollar-neutral）")

    def test_demean_preserves_within_group_order(self):
        out = NT.neutralize_groups(self.w, self.g, mode="demean")
        self.assertGreater(out["A"].iloc[0], out["B"].iloc[0])
        self.assertGreater(out["C"].iloc[0], out["D"].iloc[0])

    def test_pair_is_long_short_within_group(self):
        out = NT.neutralize_groups(self.w, self.g, mode="pair")
        self.assertGreater(out["A"].iloc[0], 0)
        self.assertLess(out["B"].iloc[0], 0)
        self.assertAlmostEqual(out["A"].iloc[0], -out["B"].iloc[0], places=12)
        self.assertAlmostEqual(float(out.sum(axis=1).iloc[0]), 0.0)

    def test_rank_is_bounded_and_monotone(self):
        out = NT.neutralize_groups(self.w, self.g, mode="rank")
        self.assertGreaterEqual(float(out.to_numpy().min()), -1.0)
        self.assertLessEqual(float(out.to_numpy().max()), 1.0)
        self.assertGreater(out["A"].iloc[0], out["B"].iloc[0])

    def test_unassigned_names_get_zero_weight(self):
        """组标签为 NaN 的名字不能保留权重（否则中性化不完整）。"""
        g = self.g.copy()
        g["D"] = np.nan
        out = NT.neutralize_groups(self.w, g, mode="demean")
        self.assertEqual(float(out["D"].iloc[0]), 0.0)

    def test_bad_mode_raises(self):
        with self.assertRaises(ValueError):
            NT.neutralize_groups(self.w, self.g, mode="nope")


class TestBetaNeutralize(unittest.TestCase):

    def test_long_only_has_no_short_leg_so_scale_is_one(self):
        """★ long-only 时 k 恒为 1 → beta 中性化是恒等变换。"""
        w = _df([0.4, 0.3, 0.2, 0.1], ["A", "B", "C", "D"])
        betas = _df([2.0, 1.5, 0.5, 0.3], ["A", "B", "C", "D"])
        out = NT.neutralize_beta(w, betas)
        np.testing.assert_allclose(out.to_numpy(), w.to_numpy(), atol=1e-12)

    def test_long_short_gets_short_leg_scaled(self):
        w = _df([0.5, 0.5, -0.25, -0.25], ["A", "B", "C", "D"])
        betas = _df([2.0, 2.0, 0.5, 0.5], ["A", "B", "C", "D"])
        out = NT.neutralize_beta(w, betas)
        net_beta = float((out * betas).sum(axis=1).iloc[0])
        self.assertAlmostEqual(net_beta, 0.0, places=9,
                               msg="多空组合的净 beta 应被压到 0")


class TestCryptoGrouping(unittest.TestCase):

    def test_sector_panel_assigns_known_coins(self):
        idx = pd.bdate_range("2024-01-01", periods=3)
        cols = ["BTCUSDT", "ETHUSDT", "ARBUSDT", "OPUSDT", "UNIUSDT", "AAVEUSDT"]
        lab = NT.sector_panel(cols, idx, min_names=2)
        self.assertEqual(lab["BTCUSDT"].iloc[0], lab["ETHUSDT"].iloc[0])   # 都是 L1
        self.assertEqual(lab["ARBUSDT"].iloc[0], lab["OPUSDT"].iloc[0])    # 都是 L2
        self.assertFalse(np.isnan(lab["BTCUSDT"].iloc[0]))

    def test_single_member_sector_is_dropped(self):
        """只有 1 个成员的板块无法"组内中性化"，必须置 NaN。"""
        idx = pd.bdate_range("2024-01-01", periods=3)
        cols = ["BTCUSDT", "ETHUSDT", "DOGEUSDT"]     # Meme 只有 DOGE
        lab = NT.sector_panel(cols, idx, min_names=2)
        self.assertTrue(np.isnan(lab["DOGEUSDT"].iloc[0]))

    def test_size_buckets_are_ordered_by_adv(self):
        idx = pd.bdate_range("2024-01-01", periods=60)
        cols = ["LOW", "MID", "HIGH"]
        adv = pd.DataFrame({"LOW": 1e5, "MID": 1e6, "HIGH": 1e7}, index=idx)
        lab = NT.size_buckets(adv, n=3, min_names=1)
        self.assertLess(lab["LOW"].iloc[-1], lab["MID"].iloc[-1])
        self.assertLess(lab["MID"].iloc[-1], lab["HIGH"].iloc[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
