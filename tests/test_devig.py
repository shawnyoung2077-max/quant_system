"""
test_devig.py - de-vig 的回归测试
============================================================================
锁定的 bug 类型（本项目已犯过两次同类错误）：
  **二分法方向写反** —— 不报错、不崩，只是给出荒谬但格式正确的数字。

具体案例：
  power 法求 k 使 Σ p_i^k = 1。
  因为 p_i ∈ (0,1)，p_i^k 随 k 增大而减小；
  而 k=1 时 Σp = overround > 1，所以根在 k > 1 一侧。
  初版写成 `if v>1: hi=mid`，方向反了，收敛到 k≈5，
  把热门队（odds=1.57）的概率算成 **0.9929** —— 荒谬但"看起来像个概率"。

运行：
    python -m unittest tests.test_devig -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polytrade.refodds import devig, overround, league_matches   # noqa: E402


class TestLeagueMatching(unittest.TestCase):
    """
    ★ 联赛名匹配：宁可漏，绝不能错。

    实测过的两个真实故障（都发生过）：
      · 漏匹配: 白名单 "League of Ireland Premier" vs 实际 "League of Ireland Premier Division"
                → 精确比较会静默跳过一个确实有 18 个市场的联赛
      · 假匹配: "K League 1" vs "K League 2"（不同级别）
                "Austria Bundesliga" vs "Basketball Bundesliga"（足球 vs 篮球）
                → 用"有交集"判断会全部误匹配，
                  下游会拿【别的联赛】的赔率算公允价，产生完全错误的数据

    假匹配远比漏匹配危险 —— 这些断言就是为了把它钉死。
    """

    FALSE_CASES = [
        ("K League 1", "K League 2"),                 # 不同级别
        ("J1 League", "J2 League"),
        ("Austria Bundesliga", "Basketball Bundesliga"),  # 足球 vs 篮球
        ("LaLiga", "LaLiga2"),
        ("Danish Superliga", "Swiss Super League"),
        ("Veikkausliiga", "Virslīga"),
    ]
    TRUE_CASES = [
        ("League of Ireland Premier", "League of Ireland Premier Division"),
        ("Veikkausliiga", "Veikkausliiga"),
        ("Danish Superliga", "Danish Superliga"),
        ("K League 1", "K League 1"),
        ("Austria Bundesliga", "Austria Bundesliga"),
    ]

    def test_no_false_positives(self):
        for a, b in self.FALSE_CASES:
            self.assertFalse(league_matches(a, b),
                             "%r 不应匹配 %r（假匹配会产生错误数据）" % (a, b))
            self.assertFalse(league_matches(b, a), "对称性失败: %r/%r" % (a, b))

    def test_no_false_negatives(self):
        for a, b in self.TRUE_CASES:
            self.assertTrue(league_matches(a, b),
                            "%r 应匹配 %r（漏匹配会静默丢机会）" % (a, b))

    def test_digits_distinguish_divisions(self):
        """数字必须保留 —— 剥掉后 K League 1/2 会互相误匹配。"""
        self.assertFalse(league_matches("K League 1", "K League 2"))
        self.assertTrue(league_matches("K League 1", "K League 1"))

    def test_empty_names_fall_back_to_exact(self):
        self.assertTrue(league_matches("", ""))
        self.assertFalse(league_matches("", "Something"))


class TestDevig(unittest.TestCase):

    def test_all_methods_sum_to_one(self):
        for o in ([1.57, 5.63, 4.45], [1.10, 15.0, 9.0], [2.90, 2.95, 3.10]):
            for m in ("multiplicative", "power", "shin"):
                q = devig(o, m)
                self.assertAlmostEqual(sum(q), 1.0, places=9,
                                       msg="%s 未归一化: %s" % (m, q))

    def test_power_matches_hand_calculation(self):
        """★ 锁定 power 法的结构性错误：必须与独立求根一致。"""
        from scipy.optimize import brentq
        o = [1.57, 5.63, 4.45]
        p = np.array([1.0 / x for x in o])
        k = brentq(lambda kk: float(np.sum(p ** kk)) - 1.0, 1.0, 10.0)
        expect = (p ** k) / (p ** k).sum()
        got = devig(o, "power")
        np.testing.assert_allclose(got, expect, atol=1e-9,
                                   err_msg="power 法与独立求根不一致")

    def test_favourite_prob_is_sane(self):
        """★ 热门队概率必须 < 1 且 > 冷门队 —— 初版这里算出 0.99。"""
        o = [1.57, 5.63, 4.45]          # 主队是热门
        for m in ("multiplicative", "power", "shin"):
            q = devig(o, m)
            self.assertLess(q[0], 0.95, "%s: 热门概率 %.4f 过高" % (m, q[0]))
            self.assertGreater(q[0], q[1])
            self.assertGreater(q[0], q[2])
            self.assertTrue(all(0 < x < 1 for x in q))

    def test_extreme_favourite_stays_below_one(self):
        for m in ("multiplicative", "power", "shin"):
            q = devig([1.05, 20.0, 15.0], m)
            self.assertLess(q[0], 1.0, "%s 给出了 >= 1 的概率" % m)

    def test_ordering_preserved(self):
        """de-vig 不能改变赔率的大小顺序。"""
        o = [2.72, 2.53, 3.50]
        for m in ("multiplicative", "power", "shin"):
            q = devig(o, m)
            # 赔率越小 → 概率越大
            order_odds = np.argsort(o)
            order_prob = np.argsort(q)[::-1]
            self.assertEqual(list(order_odds), list(order_prob),
                             "%s 改变了顺序" % m)

    def test_methods_agree_when_overround_is_small(self):
        """抽水很小时三种方法应几乎一致（这是它们都合理的前提）。"""
        o = [2.90, 2.95, 3.10]
        self.assertLess(overround(o) - 1.0, 0.01)
        qs = [devig(o, m) for m in ("multiplicative", "power", "shin")]
        spread = max(max(q[i] for q in qs) - min(q[i] for q in qs) for i in range(3))
        self.assertLess(spread, 0.002, "小抽水下三种方法分歧过大: %.5f" % spread)

    def test_methods_diverge_when_overround_is_large(self):
        """★ 抽水大时三种方法会显著分歧 —— 这是策略必须知道的风险。"""
        o = [1.10, 15.0, 9.0]
        self.assertGreater(overround(o) - 1.0, 0.05)
        qs = [devig(o, m) for m in ("multiplicative", "power", "shin")]
        spread = max(q[0] for q in qs) - min(q[0] for q in qs)
        self.assertGreater(spread, 0.03,
                           "大抽水下分歧应显著；实测 %.4f" % spread)
        # 这条断言的意义：分歧(>3pp) 比我们追的 edge(2~3pp) 还大
        # ⇒ 策略必须要求信号在三种方法下都成立

    def test_bad_odds_rejected(self):
        self.assertIsNone(devig([1.0, 5.0, 4.0], "multiplicative"))
        self.assertIsNone(devig([0.5, 5.0, 4.0], "power"))

    def test_bad_method_raises(self):
        with self.assertRaises(ValueError):
            devig([1.5, 4.0, 3.0], "nope")


if __name__ == "__main__":
    unittest.main(verbosity=2)
