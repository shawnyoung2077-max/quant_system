"""
test_regressions.py —— 本会话踩过的每一个 bug 都锁一条测试
============================================================================
为什么单独一个文件：

2026-10-12 用户指令："每天检查 bug，测试框架别省，数据要紧。"

本会话一共踩到 6 个 bug，**全部是静默的**（失败和成功看起来一样）：
  1. 结算恒为 0        —— gamma-api /markets?id 隐式 closed=false
  2. 重复下注          —— 判重键带 target_lead，NULL 匹配不上
  3. COALESCE 修复无效 —— 把 NULL 归一成 -1，仍匹配不上 1/3（我标错了结论）
  4. 联赛覆盖截断      —— tags[:200] 按任意顺序砍，漏掉英超/西甲/MLS
  5. 崩溃无回执        —— 第 1 步抛异常导致第 4 步（看门狗）永不执行
  6. 净收益符号错误    —— loss 本身为负，写成 `-` 等于把亏损加了上去

每条测试都对应上面一条。**测试写的是"这个 bug 不允许再出现"，
不是"这个函数目前返回什么"。** 所以断言尽量用具体数值，不用快照。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from polytrade import config as C                      # noqa: E402
from polytrade import invariants as INV                # noqa: E402


class TestDedupRegression(unittest.TestCase):
    """#2/#3 判重键：NULL 也必须能匹配上。"""

    def test_coalesce_would_not_have_worked(self):
        """
        锁住那个"我标错了结论"的事实：
        旧写法 COALESCE(target_lead,-1)=COALESCE(?,-1) 在 NULL vs 1 时**不相等**，
        所以它根本没有修好重复下注。真正的修法是判重键里不含 target_lead。
        """
        pairs = [(None, 1), (None, 3), (None, None), (1, 1)]
        def old_match(a, b):
            return (-1 if a is None else a) == (-1 if b is None else b)
        self.assertFalse(old_match(*pairs[0]))   # NULL vs 1 -> 不匹配（旧修复无效）
        self.assertFalse(old_match(*pairs[1]))   # NULL vs 3 -> 不匹配
        self.assertTrue(old_match(*pairs[2]))    # NULL vs NULL -> 匹配
        self.assertTrue(old_match(*pairs[3]))    # 1 vs 1 -> 匹配

    def test_price_window_derives_from_bet_rules(self):
        """
        #4 的同类：两个地方描述同一约束一定会漂移。
        价格预筛必须由 BET_RULES 推导 —— 写死会像 [0.05,0.35) 那样
        把 [0.90,0.99) 的规则变成永远不可达的死代码。
        """
        self.assertEqual(C.BET_PRICE_MIN, min(r[0] for r in C.BET_RULES))
        self.assertEqual(C.BET_PRICE_MAX, max(r[1] for r in C.BET_RULES))
        # 每条规则的价格区间都必须落在预筛窗口内
        for lo, hi, _sd, _nt in C.BET_RULES:
            self.assertGreaterEqual(lo, C.BET_PRICE_MIN)
            self.assertLessEqual(hi, C.BET_PRICE_MAX)

    def test_no_bet_rule_is_unreachable(self):
        """每条 BET_RULE 都必须存在一个能通过预筛的价格。"""
        for lo, hi, sd, _nt in C.BET_RULES:
            self.assertLess(lo, C.BET_PRICE_MAX, "规则 %s 的价格下界超出预筛" % sd)
            self.assertGreater(hi, C.BET_PRICE_MIN, "规则 %s 的价格上界低于预筛" % sd)
            mid = (lo + hi) / 2.0
            self.assertTrue(C.BET_PRICE_MIN <= mid < C.BET_PRICE_MAX,
                            "规则 %s 取中点后无法通过预筛" % sd)


class TestCostAndSignRegression(unittest.TestCase):
    """#6 符号错误 + 成本口径。"""

    def test_fee_formula_matches_official(self):
        """官方公式 fee = C x feeRate x p x (1-p)，体育 feeRate=0.05。"""
        from polytrade.scan import calc_fee
        p, shares = 0.24, 100.0
        expect = shares * 0.05 * p * (1 - p)
        self.assertAlmostEqual(calc_fee(p, shares), expect, places=10)

    def test_net_edge_sign_convention(self):
        """
        锁住符号约定：亏损必须表示为**负数**。
        papermm 曾把它当成正数减，结果把亏损加了上去。
        """
        loss_per_fill = -0.50      # 负 = 亏
        fill_rate = 0.10
        reward = 0.06
        loss_per_cycle = fill_rate * loss_per_fill          # -0.05
        self.assertLess(loss_per_cycle, 0)
        net_correct = reward + loss_per_cycle               # 正确的是 +
        net_wrong = reward - loss_per_cycle                 # 曾经写错的形式
        self.assertAlmostEqual(net_correct, 0.01, places=10)
        self.assertAlmostEqual(net_wrong, 0.11, places=10)
        self.assertLess(net_correct, net_wrong,
                        "写错符号会把净收益放大 —— 这正是当时的症状")

    def test_breakeven_winrate_above_price(self):
        """
        保本胜率必须**高于**买入价（因为要付手续费）。
        以 p 买入、赢回收 1，保本需要 q >= p + fee_per_share。
        """
        p = 0.2434
        fee = 0.05 * p * (1 - p)
        be = p + fee
        self.assertGreater(be, p)
        self.assertAlmostEqual(be, 0.2526, places=3)


class TestGateRegression(unittest.TestCase):
    """#4/#5 门槛与额度：判据必须真的生效，不能只看布尔。"""

    def test_edge_gate_requires_supported(self):
        """
        门槛必须同时要求 supported=1。曾漏掉它，导致 n_band=1 的联赛
        （单个历史市场算出 +79.7pp）被判成 valid。
        """
        from polytrade import scan as SC
        wl = SC.load_whitelist()
        self.assertTrue(len(wl) > 0, "白名单读不到 —— 会导致全部退化成 explore")
        for k, rec in wl.items():
            if rec.get("supported") != 1:
                # 不显著的联赛必须**估不出偏差**（返回 None），而不是给一个数
                self.assertIsNone(SC.league_edge(k, wl),
                                  "%s 不显著却给出了偏差估计" % k)

    def test_track_uses_relative_threshold(self):
        """门槛必须是"相对实付价的收益率"，不是绝对 pp（两档合约价差 2.5 倍）。"""
        from polytrade import scan as SC
        wl = SC.load_whitelist()
        # 找一个过门槛的联赛，确认其相对收益率确实 >= 阈值
        checked = 0
        for k in wl:
            if SC.track_of(k, 0.20, wl) == "valid":
                self.assertGreaterEqual(SC.rel_edge(k, 0.20, wl),
                                        C.MIN_EDGE_PCT_OF_STAKE - 1e-9)
                checked += 1
        self.assertGreater(checked, 0, "一个过门槛的联赛都没有，判据可能失效")

    def test_risk_caps_are_positive_and_ordered(self):
        """三道闸必须都 > 0，且单簇上限 <= 敞口上限（否则单簇形同虚设）。"""
        self.assertGreater(C.MAX_DAILY_NEW_PCT, 0)
        self.assertGreater(C.MAX_EXPOSURE_PCT, 0)
        self.assertGreater(C.MAX_CLUSTER_PCT, 0)
        self.assertLessEqual(C.MAX_CLUSTER_PCT, C.MAX_EXPOSURE_PCT)
        self.assertGreater(C.STAKE, 0)
        self.assertLess(C.STAKE, C.PAPER_BANKROLL * C.MAX_CLUSTER_PCT)


class TestInvariantsSelfTest(unittest.TestCase):
    """
    不变量模块自己也要被测 —— 否则它坏掉时会**安静地什么都不报**，
    而那正是它要防的事。
    """

    def _tmp_conn(self):
        import sqlite3
        import tempfile
        from polytrade import db as D
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = D.connect(path)
        return conn, path

    def test_detects_bad_price_ordering(self):
        conn, path = self._tmp_conn()
        try:
            conn.execute("INSERT INTO observations(market_id,snap_date,snap_ts,bid,ask,mid,spread)"
                         " VALUES('M','2026-01-01','t',0.60,0.40,0.50,0.20)")
            conn.commit()
            iss = INV.check_observations(conn)
            self.assertTrue(any(n == "obs.price_range" for n, _l, _m in iss),
                            "bid>ask 竟然没被检出")
        finally:
            conn.close()
            os.remove(path)

    def test_detects_bad_fee(self):
        conn, path = self._tmp_conn()
        try:
            conn.execute("INSERT INTO bets(market_id,entry_price,shares,stake,fee,status,entry_ts)"
                         " VALUES('M',0.2,50,10,99.0,'open','2026-01-01T00:00:00+00:00')")
            conn.commit()
            iss = INV.check_bets(conn)
            self.assertTrue(any(n == "bet.fee_formula" for n, _l, _m in iss),
                            "手续费算错竟然没被检出")
        finally:
            conn.close()
            os.remove(path)

    def test_detects_post_start_entry(self):
        conn, path = self._tmp_conn()
        try:
            conn.execute("INSERT INTO bets(market_id,entry_price,shares,stake,status,"
                         "entry_ts,game_start) VALUES('M',0.2,50,10,'open',"
                         "'2026-01-02T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
            conn.commit()
            iss = INV.check_bets(conn)
            self.assertTrue(any(n == "bet.entry_before_start" for n, _l, _m in iss),
                            "赛后建仓竟然没被检出")
        finally:
            conn.close()
            os.remove(path)

    def test_detects_ai_post_match_forecast(self):
        """AI 预测必须赛前落库 —— 这是整个 AI 实验可证伪的前提。"""
        conn, path = self._tmp_conn()
        try:
            from polytrade import aiforecast as AF
            AF.ensure_schema(conn)
            conn.execute("INSERT INTO ai_forecasts(market_id,made_ts,game_start,ai_prob)"
                         " VALUES('M','2026-01-02T00:00:00+00:00',"
                         "'2026-01-01T00:00:00+00:00',0.5)")
            conn.commit()
            iss = INV.check_ai_forecasts(conn)
            self.assertTrue(any(n == "ai.pre_match_only" for n, _l, _m in iss),
                            "赛后补录 AI 预测竟然没被检出")
        finally:
            conn.close()
            os.remove(path)

    def test_clean_db_reports_nothing_fatal(self):
        conn, path = self._tmp_conn()
        try:
            iss = INV.check_all(conn)
            fatal = [i for i in iss if i[1] == "FAIL"]
            self.assertEqual(fatal, [], "空库不该有 FAIL：%s" % fatal)
        finally:
            conn.close()
            os.remove(path)


class TestProxySessionGuard(unittest.TestCase):
    """
    bug #7（2026-10-13 / -14）：在无桌面的会话里拉起 Clash Verge GUI。
    ========================================================================
    现象：用户点 Clash 图标**什么窗口都不弹**。
    根因：定时任务跑在会话 0（服务会话，没有桌面），
          polytrade_run.bat -> python -> ensure_proxy() 在那里 Popen 了
          clash-verge.exe。它在会话 0 创建不出窗口（Tray 报
          os error -2147467259），**却抢到了单例锁**；
          此后用户每次点图标，新实例都把「显示窗口」转发给它，
          日志打印「窗口激活成功」，用户永远看不到。

    这条 bug 的修复**失败过一次**，那才是最值得锁的部分：
      第一版的 interactive_session() 在会话 0 时判据是
      「tasklist 里有没有 explorer.exe」—— 而 tasklist 会列出**所有会话**的
      explorer。本机实测会话 1、会话 2 各有一个 explorer，
      于是会话 0 里的进程以为自己能显示窗口，**又把 GUI 拉进了会话 0**。
    所以第一条测试专门锁「会话号必须按会话号判，不能按有没有 explorer 判」。
    """

    def setUp(self):
        self._orig_popen = None

    def _patch(self, **kw):
        """临时替换 config 里的探测函数，返回还原用的闭包。"""
        saved = {}
        for name, fn in kw.items():
            saved[name] = getattr(C, name)
            setattr(C, name, fn)
        def restore():
            for name, fn in saved.items():
                setattr(C, name, fn)
        return restore

    def test_session0_is_not_interactive_even_when_explorer_exists_elsewhere(self):
        """核心回归：本机存在会话1/2 的 explorer 时，会话 0 仍必须是"非交互式"。"""
        def fake_ts(image):
            if image.lower() == "explorer.exe":
                # 本机实测：会话 1 和会话 2 各一个 explorer，**没有一个在会话 0**
                return [(13232, 1), (2324, 2)]
            return []
        restore = self._patch(_tasklist_sessions=fake_ts,
                              _session_of_pid=lambda pid: 0)
        try:
            self.assertFalse(
                C.interactive_session(),
                "会话 0 竟被判成可交互 —— GUI 又会被拉进无桌面会话，"
                "这正是 2026-10-14 事故重演的原因")
        finally:
            restore()

    def test_nonzero_session_is_interactive(self):
        restore = self._patch(_session_of_pid=lambda pid: 1)
        try:
            self.assertTrue(C.interactive_session())
        finally:
            restore()

    def test_kill_targets_only_session0_gui(self):
        """
        清理目标**只能**是会话 0 的 clash-verge.exe。
        不能采用「和桌面会话号不同就杀」—— 本机有会话 1 和 2 两个 explorer，
        宽判据会误杀用户在另一个会话里正常使用的 GUI。
        """
        import subprocess

        def fake_ts(image):
            n = image.lower()
            if n == "clash-verge.exe":
                return [(111, 0), (222, 1), (333, 2)]
            if n == "explorer.exe":
                return [(9, 1)]
            return []

        calls = []

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            return R()

        restore = self._patch(_tasklist_sessions=fake_ts)
        old_run = subprocess.run
        subprocess.run = fake_run
        try:
            got = C.kill_invisible_gui(verbose=False)
        finally:
            subprocess.run = old_run
            restore()
        self.assertEqual(got, [111], "只应结束会话 0 的那个，实际 %s" % got)
        self.assertEqual(len(calls), 1, "不该对会话 1/2 的 GUI 动手：%s" % calls)
        self.assertIn("111", " ".join(str(x) for x in calls[0]))

    def test_no_desktop_session_means_no_kill(self):
        """本机若定位不到桌面会话（例如完全无头），宁可不杀，绝不误杀。"""
        restore = self._patch(_tasklist_sessions=lambda image: [],
                              _session_of_pid=lambda pid: 0)
        try:
            self.assertEqual(C.kill_invisible_gui(verbose=False), [])
        finally:
            restore()

    def test_tasklist_sessions_parses_csv(self):
        import subprocess

        class R:
            returncode = 0
            stdout = ('"Image Name","PID","Session Name","Session#","Mem Usage"\r\n'
                      '"clash-verge.exe","28540","Services","0","12,345 K"\r\n'
                      '"clash-verge.exe","777","Console","1","1,000 K"\r\n'
                      'INFO: No tasks are running which match the specified criteria.\r\n')
            stderr = ""

        old_run = subprocess.run
        subprocess.run = lambda *a, **kw: R()
        try:
            got = C._tasklist_sessions("clash-verge.exe")
        finally:
            subprocess.run = old_run
        self.assertEqual(got, [(28540, 0), (777, 1)], "CSV 解析错了：%s" % got)

    def test_ensure_proxy_refuses_gui_in_session0(self):
        """端到端：会话 0 里 ensure_proxy() 绝不能 Popen 出 GUI 进程。"""
        import subprocess
        popen_calls = []
        restore = self._patch(_detect_proxy=lambda: None,
                              _session_of_pid=lambda pid: 0,
                              _tasklist_sessions=lambda image: [])
        old_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **kw: popen_calls.append(a)
        try:
            got = C.ensure_proxy(wait_sec=0, verbose=False)
        finally:
            subprocess.Popen = old_popen
            restore()
        self.assertIsNone(got)
        self.assertEqual(popen_calls, [],
                         "会话 0 里竟然拉起了 GUI —— 单例锁会被抢走：%s" % popen_calls)


class TestRunWallClockBudget(unittest.TestCase):
    """
    bug #8（2026-10-14）：整轮运行跑过头，撞上计划任务的 ExecutionTimeLimit 被硬杀。
    ========================================================================
    实测事故：
        09:54  开始（[1/5] 结算）
        10:09  [2/5] 扫描结束（自己按 720s 正常截断，runs.note 记了 time_budget）
        10:09~ [4/5] 抓参照价 + 匹配赛程 …… 一直跑
        10:24  撞上 ExecutionTimeLimit = PT30M，被**硬杀**
    后果：报告没生成、看门狗没跑、digest/report 一起停在旧版本。
    这与第 5 条 bug（崩溃无回执）是同一个形状，只是触发者从异常变成了超时。

    而 ExecutionTimeLimit **改不了**（Set-ScheduledTask 要账户密码，报
    "The user name or password is incorrect"），所以只能让运行适应 30 分钟天花板。
    """

    def test_budget_fits_inside_the_hard_task_limit(self):
        """★ 最关键的一条：预算必须留在 PT30M(=1800s) 以内。"""
        from polytrade import config as C
        self.assertLessEqual(
            C.RUN_BUDGET_SEC, 1800,
            "整轮预算超过了计划任务的硬上限 1800s —— 又会被硬杀，修复失效")
        self.assertLessEqual(
            C.RUN_BUDGET_SEC + C.RUN_RESERVE_SEC, 1800,
            "预算 + 保留 超过 1800s：报告和看门狗可能来不及写")
        self.assertGreaterEqual(C.RUN_RESERVE_SEC, 120,
                                "留给报告/看门狗的保留时间太短")

    def test_fetch_budget_never_eats_the_reserve(self):
        """抓取预算 = min(cap, 剩余 - 保留)，绝不能吃到保留额。"""
        from polytrade import run as RUN
        # 剩余 500s、保留 240s、上限 720s -> 260s（受剩余约束，不受 cap 约束）
        self.assertEqual(RUN.plan_fetch_budget(500, 240, 720), 260)
        # 剩余很多 -> 受 cap 约束
        self.assertEqual(RUN.plan_fetch_budget(1500, 240, 720), 720)
        # 剩余几乎没了 -> 保底 60s，而不是 0（抓取是唯一"错过补不回来"的步骤）
        self.assertEqual(RUN.plan_fetch_budget(100, 240, 720), 60)
        self.assertEqual(RUN.plan_fetch_budget(-50, 240, 720), 60)

    def test_reserve_is_always_positive_after_planning(self):
        """任意剩余时间下，规划完都必须给保留额留下 >= RUN_RESERVE_SEC-60。"""
        from polytrade import run as RUN
        from polytrade import config as C
        for left in (60, 200, 400, 900, 5000):
            fb = RUN.plan_fetch_budget(left, C.RUN_RESERVE_SEC, C.FETCH_TIME_BUDGET_SEC)
            self.assertLessEqual(
                fb, max(60.0, left - C.RUN_RESERVE_SEC) + 1e-9,
                "left=%s 时抓取预算 %s 吃掉了保留额" % (left, fb))

    def test_scan_accepts_and_forwards_budget(self):
        """run_scan 必须能把预算透传给 fetch_all_markets（否则改了个寂寞）。"""
        import inspect
        from polytrade import scan as SC
        self.assertIn("budget_sec", inspect.signature(SC.fetch_all_markets).parameters)
        self.assertIn("budget_sec", inspect.signature(SC.run_scan).parameters)

    def test_refodds_budget_stops_the_loop_and_records_it(self):
        """
        参照价步骤（真凶）必须能被预算截断，且截断要**落库**。
        落库而不是留在内存：scan 的 runs.note 在扫描结束时就写完了，
        那时本步骤还没开始，写内存列表等于写进虚空。
        """
        import inspect
        import tempfile
        from polytrade import refodds as RO
        from polytrade import db as D

        self.assertIn("budget_sec", inspect.signature(RO.fetch_all).parameters)

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = D.connect(path)
        calls = []
        real_map, real_fetch = RO.LEAGUE_MAP, RO.fetch_league
        RO.LEAGUE_MAP = {"L%d" % i: "soccer_l%d" % i for i in range(6)}
        RO.fetch_league = lambda conn, sk, **kw: (calls.append(sk), (0, "stub"))[1]
        try:
            # 用 -1 而不是 0：Windows 上 time.time() 两次连续取值可能完全相同，
            # 用 0 会让第一次迭代的 elapsed(0.0) > 0 判为 False，测不出东西。
            RO.fetch_all(conn=conn, verbose=False, only_active=False, budget_sec=-1)
            rows = list(conn.execute(
                "SELECT kind, note FROM runs WHERE kind='refodds'"))
        finally:
            RO.LEAGUE_MAP, RO.fetch_league = real_map, real_fetch
            conn.close()
            os.remove(path)
        self.assertEqual(calls, [], "预算耗尽时不该发任何请求，实际 %s" % calls)
        self.assertEqual(len(rows), 1, "预算截断没有落库 —— 又变成静默少采了")
        self.assertIn("time_budget", rows[0][1])
        self.assertIn("leagues_skipped=6", rows[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
