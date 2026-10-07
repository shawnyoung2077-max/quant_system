"""
test_core.py - 核心正确性测试

用标准库 unittest 编写（零额外依赖），pytest 也可直接运行：
    python -m unittest discover -s tests -v
    pytest tests/ -v

重点覆盖三类「会静默出错」的问题：
    1. 前视偏差（look-ahead bias）—— 回测最常见的致命错误
    2. 交易成本方向 —— 成本必须让收益变差，不能变好
    3. 无信号数据上不应产生虚假 alpha

关于本项目指标定义的两点说明（测试已按此编写）：
    · max_drawdown 返回**正数**，表示"从峰值跌了百分之多少"
    · sharpe_ratio 用**几何年化收益** (prod^(252/n)-1)，因此不满足尺度不变性
      —— 这是几何年化的固有性质，不是缺陷
"""
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import make_sample_prices                      # noqa: E402
from strategies import (                                  # noqa: E402
    cross_sectional_momentum,
    cross_sectional_mean_reversion,
    time_series_momentum,
)
from backtest import portfolio_backtest, single_asset_backtest, buy_and_hold  # noqa: E402
from metrics import (                                     # noqa: E402
    summary, annualized_return, sharpe_ratio, max_drawdown, cumulative_returns,
)

# 零成本回测参数（与 backtest.portfolio_backtest 的签名一致）
ZERO_COST = dict(commission_bps=0.0, slippage_bps=0.0)
COST = dict(commission_bps=10.0, slippage_bps=5.0)


def _predictive_corr(prices: pd.DataFrame, window: int = 20) -> float:
    """过去 window 日累计收益 与 次日收益 的相关系数（衡量可预测性）"""
    rets = prices.pct_change()
    past = rets.rolling(window).sum().shift(1).values.ravel()
    fut = rets.shift(-1).values.ravel()
    mask = np.isfinite(past) & np.isfinite(fut)
    if mask.sum() < 100:
        return 0.0
    return float(np.corrcoef(past[mask], fut[mask])[0, 1])


class TestDataGenerator(unittest.TestCase):
    """数据生成器"""

    def test_shape_and_index(self):
        px = make_sample_prices(n_assets=10, n_days=100, seed=1)
        self.assertEqual(px.shape, (100, 10))
        self.assertTrue(px.index.is_monotonic_increasing)
        self.assertTrue((px.values > 0).all(), "价格必须为正")

    def test_reproducible(self):
        a = make_sample_prices(n_assets=5, n_days=50, seed=7)
        b = make_sample_prices(n_assets=5, n_days=50, seed=7)
        pd.testing.assert_frame_equal(a, b)

    def test_signal_zero_is_plain_random_walk(self):
        """signal=0 时不应有任何可预测的横截面结构"""
        px = make_sample_prices(n_assets=40, n_days=900, seed=3, signal=0.0)
        c = _predictive_corr(px)
        self.assertLess(abs(c), 0.15, "无信号数据不应有可预测性，实测 corr=%.4f" % c)

    def test_signal_creates_predictability(self):
        """signal>0 时，过去收益应能预测未来收益（动量）"""
        px = make_sample_prices(n_assets=40, n_days=900, seed=3, signal=0.2)
        c = _predictive_corr(px)
        self.assertGreater(c, 0.02, "植入动量信号后应有正的可预测性，实测 corr=%.4f" % c)


class TestNoLookahead(unittest.TestCase):
    """★ 前视偏差检测 —— 回测框架最常见的致命错误

    做法：把「未来」的价格改掉，检查「过去」产生的权重是否变化。
    如果变了，说明信号用了未来信息。
    """

    def _check(self, weight_fn, lookback, tail=100):
        px1 = make_sample_prices(n_assets=20, n_days=400, seed=11)
        px2 = px1.copy()
        px2.iloc[-tail:] = px2.iloc[-tail:] * 3.0   # 篡改最后 tail 天

        w1 = weight_fn(px1, lookback)
        w2 = weight_fn(px2, lookback)

        cutoff = len(px1) - tail - lookback
        self.assertGreater(cutoff, 0, "样本太短，无法测试")
        pd.testing.assert_frame_equal(
            w1.iloc[:cutoff], w2.iloc[:cutoff],
            check_exact=False, atol=1e-12,
            obj="横截面权重在修改未来数据后发生变化 -> 存在前视偏差")

    def test_momentum_no_lookahead(self):
        self._check(lambda p, lb: cross_sectional_momentum(p, lookback=lb, top_pct=0.2), 60)

    def test_reversion_no_lookahead(self):
        self._check(lambda p, lb: cross_sectional_mean_reversion(p, lookback=lb, top_pct=0.2), 5)

    def test_ts_momentum_no_lookahead(self):
        px1 = make_sample_prices(n_assets=5, n_days=400, seed=11)
        px2 = px1.copy()
        px2.iloc[-100:] = px2.iloc[-100:] * 3.0
        s1 = time_series_momentum(px1.iloc[:, 0], lookback=63, hold=10)
        s2 = time_series_momentum(px2.iloc[:, 0], lookback=63, hold=10)
        cutoff = len(px1) - 100 - 63
        pd.testing.assert_series_equal(
            s1.iloc[:cutoff], s2.iloc[:cutoff], check_exact=False, atol=1e-12,
            obj="时序动量信号在修改未来数据后发生变化 -> 存在前视偏差")


class TestBacktestEngine(unittest.TestCase):
    """回测引擎"""

    def setUp(self):
        self.px = make_sample_prices(n_assets=10, n_days=300, seed=5)

    def test_output_shape(self):
        w = cross_sectional_momentum(self.px, lookback=20, top_pct=0.3)
        ret, to = portfolio_backtest(self.px, w)
        self.assertEqual(len(ret), len(self.px))
        self.assertEqual(len(to), len(self.px))

    def test_costs_reduce_returns(self):
        """★ 交易成本必须让收益变差（如果反而变好，说明成本符号写反了）"""
        w = cross_sectional_momentum(self.px, lookback=20, top_pct=0.3)
        ret0, _ = portfolio_backtest(self.px, w, **ZERO_COST)
        ret1, _ = portfolio_backtest(self.px, w, **COST)
        self.assertLessEqual(
            float((1 + ret1.fillna(0)).prod()), float((1 + ret0.fillna(0)).prod()) + 1e-12,
            "加入成本后累计收益不应提高")

    def test_zero_cost_near_zero_return_on_no_signal(self):
        """无信号 + 零成本 -> 多空组合收益应接近 0"""
        px = make_sample_prices(n_assets=30, n_days=700, seed=9, signal=0.0)
        w = cross_sectional_momentum(px, lookback=60, top_pct=0.2)
        ret, _ = portfolio_backtest(px, w, **ZERO_COST)
        self.assertLess(abs(float(ret.fillna(0).mean())), 0.003,
                        "无信号数据不应产生显著收益")

    def test_buy_and_hold_matches_asset(self):
        single = self.px.iloc[:, 0]
        bh = buy_and_hold(self.px.iloc[:, [0]])
        expected_total = float(single.iloc[-1] / single.iloc[0])
        self.assertAlmostEqual(float((1 + bh.fillna(0)).prod()), expected_total, places=6)

    def test_single_asset_backtest(self):
        close = self.px.iloc[:, 0]
        pos = time_series_momentum(close, lookback=20, hold=5)
        ret, to = single_asset_backtest(close, pos)
        self.assertEqual(len(ret), len(close))
        self.assertTrue(np.isfinite(ret.fillna(0).values).all())


class TestMetrics(unittest.TestCase):
    """绩效指标（注意本项目的定义约定）"""

    def test_zero_returns(self):
        r = pd.Series([0.0] * 252)
        self.assertAlmostEqual(annualized_return(r), 0.0, places=10)
        self.assertAlmostEqual(max_drawdown(cumulative_returns(r)), 0.0, places=10)

    def test_constant_positive_return(self):
        r = pd.Series([0.001] * 252)
        self.assertGreater(annualized_return(r), 0.0)
        self.assertAlmostEqual(summary(r)["total_return"], (1.001 ** 252) - 1, places=8)

    def test_max_drawdown_is_positive_magnitude(self):
        """本项目约定：最大回撤返回正数（跌了百分之多少）"""
        rng = np.random.default_rng(0)
        r = pd.Series(rng.normal(0.0005, 0.01, 500))
        dd = max_drawdown(cumulative_returns(r))
        self.assertGreaterEqual(dd, 0.0, "回撤应为非负幅度")
        self.assertLessEqual(dd, 1.0, "回撤幅度不应超过 100%")

    def test_drawdown_known_case(self):
        """已知序列：1 -> 0.5 -> 0.8，最大回撤应为 0.5"""
        nav = pd.Series([1.0, 0.5, 0.8])
        self.assertAlmostEqual(max_drawdown(nav), 0.5, places=10)

    def test_sharpe_sign_follows_mean_return(self):
        rng = np.random.default_rng(1)
        r = pd.Series(rng.normal(0.0, 0.01, 800))
        self.assertGreater(sharpe_ratio(r + 0.002), 0.0, "正均值应给出正夏普")
        self.assertLess(sharpe_ratio(r - 0.002), 0.0, "负均值应给出负夏普")

    def test_summary_keys(self):
        rng = np.random.default_rng(2)
        r = pd.Series(rng.normal(0.0003, 0.01, 300))
        s = summary(r)
        for k in ("total_return", "annualized_return", "annualized_vol",
                  "sharpe", "max_drawdown", "calmar", "win_rate",
                  "profit_factor", "n_days"):
            self.assertIn(k, s)


class TestHonesty(unittest.TestCase):
    """★ 诚实性测试：框架不应在无信号数据上"造出" alpha"""

    def test_no_signal_no_alpha(self):
        px = make_sample_prices(n_assets=40, n_days=1200, seed=21, signal=0.0)
        for name, fn in [("mom", lambda p: cross_sectional_momentum(p, lookback=60, top_pct=0.2)),
                         ("rev", lambda p: cross_sectional_mean_reversion(p, lookback=5, top_pct=0.2))]:
            w = fn(px)
            ret, _ = portfolio_backtest(px, w)
            sh = summary(ret.fillna(0))["sharpe"]
            self.assertLess(sh, 1.0,
                            "[%s] 在纯随机数据上夏普 %.2f —— 管线可能在造 alpha" % (name, sh))

    def test_planted_signal_is_detected(self):
        px = make_sample_prices(n_assets=40, n_days=1200, seed=21, signal=0.18)
        w = cross_sectional_momentum(px, lookback=252, top_pct=0.2)
        ret, _ = portfolio_backtest(px, w)
        sh = summary(ret.fillna(0))["sharpe"]
        self.assertGreater(sh, 0.5, "植入信号后夏普应显著为正，实测 %.2f" % sh)


if __name__ == "__main__":
    unittest.main(verbosity=2)
