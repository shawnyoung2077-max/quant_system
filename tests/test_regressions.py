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
        """
        端到端：会话 0 里 ensure_proxy() 绝不能 Popen 出 **GUI** 进程。

        ★ 2026-10-15 修正断言：原来断言的是"任何 Popen 都不许发生"。
        但修复"委托"之后，会话 0 会调用 `schtasks /Run polytrade_proxy_up`
        （subprocess.run 内部就是 Popen），这是一个**合法且必要**的动作 ——
        它恰恰是"不自己拉 GUI，而是请桌面会话去拉"。
        所以这里要断言的是真正的不变量：**不许拉起 Clash 的 GUI 可执行文件**。
        """
        import subprocess
        popen_calls = []
        restore = self._patch(_detect_proxy=lambda: None,
                              _session_of_pid=lambda pid: 0,
                              _tasklist_sessions=lambda image: [],
                              _run_task=lambda name: False)
        old_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **kw: popen_calls.append(a)
        try:
            got = C.ensure_proxy(wait_sec=0, verbose=False)
        finally:
            subprocess.Popen = old_popen
            restore()
        self.assertIsNone(got)
        gui_launch = [c for c in popen_calls
                      if any("clash-verge" in str(x).lower() for x in c[0])]
        self.assertEqual(gui_launch, [],
                         "会话 0 里竟然拉起了 Clash GUI —— 单例锁会被抢走：%s"
                         % gui_launch)


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
        """
        ★ 最关键的一条：整轮预算 + 保留额必须留在 PT30M(=1800s) 以内，
        否则报告和看门狗又会来不及写 —— 修复失效。
        实测启动偏移只有约 2 秒（计划任务 10:36:37 触发，run.py 打印 10:36:39），
        所以真正的余量来自 RUN_RESERVE_SEC，而不是靠把预算压得很低。
        """
        from polytrade import config as C
        self.assertLessEqual(
            C.RUN_BUDGET_SEC + C.RUN_RESERVE_SEC, 1800,
            "预算 %ds + 保留 %ds 超过 1800s：报告和看门狗可能来不及写"
            % (C.RUN_BUDGET_SEC, C.RUN_RESERVE_SEC))
        self.assertLessEqual(
            C.RUN_BUDGET_SEC, 1800,
            "整轮预算超过计划任务的硬上限 1800s —— 又会被硬杀")
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

    def test_every_heavy_step_takes_a_budget(self):
        """
        ★ 只给整轮预算是不够的：某一步内部若是个长循环，它读不到整轮预算，
        整轮预算就形同虚设。实测「抓参照价+匹配赛程」跑了 1100s（预算 655s），
        其中约 1000s 在 match.run_match 的 O(市场 x 赛程) 双重循环里。
        所以每一步都必须有自己的 budget_sec 入口。
        """
        import inspect
        from polytrade import refodds as RO
        from polytrade import match as MT
        for fn, name in ((RO.fetch_all, "refodds.fetch_all"),
                         (MT.run_match, "match.run_match")):
            self.assertIn("budget_sec", inspect.signature(fn).parameters,
                          "%s 没有预算入口 —— 它可以把整轮运行拖死" % name)

    def test_step_budget_is_smaller_than_the_whole_run(self):
        """单步上限必须严格小于整轮预算，否则"分步约束"没有意义。"""
        from polytrade import config as C
        self.assertLess(
            C.REFODDS_MAX_SEC, C.RUN_BUDGET_SEC,
            "单步上限 %ds 不小于整轮预算 %ds" % (C.REFODDS_MAX_SEC, C.RUN_BUDGET_SEC))
        self.assertLessEqual(
            C.REFODDS_MAX_SEC + C.RUN_RESERVE_SEC, C.RUN_BUDGET_SEC,
            "单步上限 + 保留 超过了整轮预算：报告可能被挤掉")

    def test_match_budget_stops_the_loop_and_records_it(self):
        """匹配赛程必须能被预算截断，且截断要落库（否则又变成静默少匹配）。"""
        import tempfile
        from polytrade import match as MT
        from polytrade import refodds as RO
        from polytrade import db as D

        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        conn = D.connect(path)
        MT.ensure_schema(conn)
        RO.ensure_schema(conn)      # odds_fixtures 属于 refodds 的 schema
        conn.execute("INSERT INTO odds_fixtures(fetched_ts,sport_key,commence_time,"
                     "home,away,odds_home,odds_away,odds_draw) VALUES"
                     "('2026-01-01T00:00:00+00:00','soccer_x','2026-01-02T00:00:00+00:00',"
                     "'A','B',2.0,3.0,3.0)")
        for i in range(50):
            conn.execute(
                "INSERT INTO observations(snap_date,snap_ts,market_id,question,league,"
                "market_type,mid) VALUES(date('now'),?,'M%d','A vs B','L','moneyline',0.5)"
                % i, (D.now_iso(),))
        conn.commit()
        conn.close()

        real_connect = D.connect
        D.connect = lambda *a, **kw: real_connect(path)
        try:
            # budget_sec=-1 -> 第一次迭代就超预算（用 -1 而非 0：Windows 上
            # time.time() 连续两次取值可能相同，用 0 会漏测）
            n_new, n_try = MT.run_match(verbose=False, budget_sec=-1)
            c2 = real_connect(path)
            rows = list(c2.execute("SELECT note FROM runs WHERE kind='match'"))
            c2.close()
        finally:
            D.connect = real_connect
            os.remove(path)
        self.assertEqual(n_try, 0, "预算耗尽时不该匹配任何市场，实际 %s" % n_try)
        self.assertEqual(len(rows), 1, "匹配截断没有落库 —— 又变成静默少匹配了")
        self.assertIn("time_budget", rows[0][0])

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


class TestProxyDelegation(unittest.TestCase):
    """
    bug #9（2026-10-15）：无桌面会话"拒绝拉 GUI"之后，没人能救代理。
    ========================================================================
    实测代价：2026-10-14~15 的 24 小时里 **4 次运行颗粒无收**
    （10-14 12:12 / 14:28 / 21:05、10-15 09:58），全是「代理不在 + 本进程无桌面」。
    其中 09:58 那次结构性无解：机器 09:47 开机、用户 10:01 登录，任务 09:58 触发。

    修法：会话 0 **自己**永远不拉 GUI（那会抢单例锁、弄坏用户的 Clash），
    但可以**委托**给一个 LogonType=Interactive 的任务 polytrade_proxy_up ——
    实测 `schtasks /Create /RU <user> /IT` 不需要账户密码，
    且 `/Run` 会跑在用户的桌面会话里（实测 session=2）。
    """

    def test_delegates_to_helper_task_when_no_desktop(self):
        """核心：无桌面 + 代理不在时，必须去触发辅助任务，而不是直接放弃。"""
        names = ("_detect_proxy", "interactive_session", "_session_of_pid",
                 "_tasklist_sessions", "_run_task", "kill_invisible_gui")
        real = {n: getattr(C, n) for n in names}
        calls = {"run_task": []}
        state = {"proxy_up": False}

        def fake_detect():
            return "http://127.0.0.1:7897" if state["proxy_up"] else None

        def fake_run_task(name):
            calls["run_task"].append(name)
            state["proxy_up"] = True          # 模拟"桌面会话那边真的拉起来了"
            return True

        saved_proxy = C.PROXY
        C._detect_proxy = fake_detect
        C.interactive_session = lambda: False
        C._session_of_pid = lambda pid: 0
        C._tasklist_sessions = lambda image: []
        C.kill_invisible_gui = lambda verbose=True: []
        C._run_task = fake_run_task
        try:
            got = C.ensure_proxy(wait_sec=10, verbose=False)
        finally:
            for n, f in real.items():
                setattr(C, n, f)
            C.PROXY = saved_proxy
        self.assertEqual(calls["run_task"], [C.PROXY_HELPER_TASK],
                         "没有委托给辅助任务：%s" % calls["run_task"])
        self.assertEqual(got, "http://127.0.0.1:7897", "委托之后没把代理接上")

    def test_returns_none_when_helper_task_cannot_be_run(self):
        """辅助任务触发不了（未注册 / 无人登录）时，只能如实返回 None，不能崩。"""
        names = ("_detect_proxy", "interactive_session", "_session_of_pid",
                 "_tasklist_sessions", "_run_task", "kill_invisible_gui")
        real = {n: getattr(C, n) for n in names}
        saved_proxy = C.PROXY
        C._detect_proxy = lambda: None
        C.interactive_session = lambda: False
        C._session_of_pid = lambda pid: 0
        C._tasklist_sessions = lambda image: []
        C.kill_invisible_gui = lambda verbose=True: []
        C._run_task = lambda name: False
        try:
            got = C.ensure_proxy(wait_sec=1, verbose=False)
        finally:
            for n, f in real.items():
                setattr(C, n, f)
            C.PROXY = saved_proxy
        self.assertIsNone(got)

    def test_helper_task_module_exists_and_is_wired(self):
        """辅助任务的载体必须真的存在、且名字和配置一致。"""
        from polytrade import ensureproxy
        self.assertTrue(callable(ensureproxy.main))
        bat = os.path.join(ROOT, "polytrade_proxy_up.bat")
        self.assertTrue(os.path.exists(bat), "缺少 %s —— 辅助任务没有载体" % bat)
        with open(bat, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        self.assertIn("polytrade.ensureproxy", body)
        self.assertEqual(C.PROXY_HELPER_TASK, "polytrade_proxy_up")


class TestStdoutEncodingSafety(unittest.TestCase):
    """
    bug #10（2026-10-15）：GBK 的 stdout 让"留痕"这一步自己崩掉。
    ========================================================================
    实测：本机 ANSI 代码页是 cp936，stdout **重定向到文件**时
    sys.stdout.encoding 就是 'gbk' —— batch 里 chcp 65001 也改不了
    （chcp 管控制台，不管重定向）：
        不带 chcp      : stdout.encoding = gbk
        带 chcp 65001  : stdout.encoding = gbk
    而日志里有 ⚠ ✓ ✗ ★ → 这些不在 GBK 里的字符，print 会抛 UnicodeEncodeError。

    之前没炸是**巧合**：report/digest/invariants/equity/match/refodds/
    aiforecast/papermm 各自在模块层 reconfigure 了一次，谁先被 import 谁修好全局。
    run.py 恰好 import 了它们；polytrade.ensureproxy 只 import config，于是崩了。

    所以：编码安全必须**在入口显式建立**，不能靠 import 副作用续命。
    """

    def _run_with_gbk_stdout(self, import_stmt):
        import subprocess
        code = (
            "import sys\n"
            "sys.stdout.reconfigure(encoding='gbk')\n"   # 模拟重定向到文件时的真实情况
            "%s\n"
            "print('\\u26a0 \\u2713 \\u2717 \\u2605 \\u2192')\n"
            "print('NO_CRASH')\n" % import_stmt
        )
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              cwd=ROOT, timeout=180)

    def test_run_entry_point_survives_gbk_stdout(self):
        r = self._run_with_gbk_stdout("import polytrade.run")
        self.assertIn("NO_CRASH", r.stdout or "",
                      "导入 polytrade.run 之后仍无法在 GBK stdout 上打印符号：%s"
                      % (r.stderr or "")[:300])

    def test_ensureproxy_entry_point_survives_gbk_stdout(self):
        r = self._run_with_gbk_stdout("import polytrade.ensureproxy")
        self.assertIn("NO_CRASH", r.stdout or "",
                      "导入 polytrade.ensureproxy 之后仍无法在 GBK stdout 上打印符号：%s"
                      % (r.stderr or "")[:300])

    def test_out_never_raises_even_if_print_fails(self):
        """
        run.out() 的首要职责是"一定留得下"。
        实测教训：out() 先 append 再 print，print 抛异常被外层 try 吞掉，
        于是那一行**只进了文件、没进 stdout**，两条输出悄悄分叉。
        """
        import inspect
        from polytrade import run as RUN
        src = inspect.getsource(RUN.main)
        i = src.index("def out(s)")
        self.assertIn("except Exception", src[i:i + 600],
                      "out() 没有兜住 print 异常 —— 留痕会随编码问题一起消失")


class TestSettleFastFail(unittest.TestCase):
    """
    bug #11（2026-10-15）：代理不在时 settle 死磕，烧掉 400~1400 秒。
    ========================================================================
    实测「扫描前已耗时」（正常只要 18~32 秒）：
        413 / 439 / 666 / 743 / 1380 秒
    两层后果：
      1) 代理真挂掉的那几次，整轮白跑 10~23 分钟才失败；
      2) 10-15 10:01 那次代理刚好恢复，settle 吃掉 666 秒，
         把参照价和 AI 的预算全挤掉（两步双双被跳过）—— "恢复了却什么都没多拿到"。
    结算天然幂等，本轮查不了下轮再来即可，没有理由死磕。
    """

    def test_settle_fast_fails_when_network_is_down(self):
        import time
        from polytrade import settle as SE
        from polytrade import scan as SC
        real = SC._get
        SC._get = lambda *a, **kw: {"__err__": "stub: connection refused"}
        try:
            t0 = time.time()
            n = SE.settle_bets(verbose=False)
            dt = time.time() - t0
        finally:
            SC._get = real
        self.assertEqual(n, 0)
        self.assertLess(dt, 5.0,
                        "网络不可达时 settle 没有快速失败，耗时 %.1fs" % dt)

    def test_assert_network_exists_and_raises(self):
        """预检必须是一个可复用的函数（settle 和 scan 共用同一份判断）。"""
        from polytrade import scan as SC
        self.assertTrue(callable(SC.assert_network))
        real = SC._get
        SC._get = lambda *a, **kw: {"__err__": "stub"}
        try:
            with self.assertRaises(RuntimeError):
                SC.assert_network()
        finally:
            SC._get = real

    def test_repeated_tickets_fetch_one_market_result(self):
        import tempfile
        from unittest.mock import patch
        from polytrade import db as D
        from polytrade import settle as SE
        from polytrade import scan as SC
        real_connect = D.connect

        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            db_path = f.name
        conn = real_connect(db_path)
        conn.executemany(
            "INSERT INTO bets (market_id, side, entry_price, shares, stake, status) "
            "VALUES (?, ?, ?, ?, ?, 'open')",
            [("same-market", "YES", 0.4, 2.5, 1.0),
             ("same-market", "NO", 0.6, 2.5, 1.0)])
        conn.commit()
        conn.close()
        try:
            with patch.object(D, "connect", side_effect=lambda: real_connect(db_path)), \
                 patch.object(SC, "assert_network"), \
                 patch.object(SE, "fetch_resolution", return_value=(True, 1, {})) as fetch:
                self.assertEqual(SE.settle_bets(verbose=False), 2)
                self.assertEqual(fetch.call_count, 1)
            conn = real_connect(db_path)
            statuses = [r[0] for r in conn.execute("SELECT status FROM bets")]
            conn.close()
            self.assertEqual(statuses, ["settled", "settled"])
        finally:
            os.unlink(db_path)


class TestCoreFetchCoverage(unittest.TestCase):
    def test_strategy_markets_are_fetched_before_broad_snapshot(self):
        import json
        import tempfile
        from unittest.mock import patch
        from polytrade import scan as SC

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                         delete=False) as f:
            json.dump([{"primaryTagId": "tag-1", "name": "League"}], f)
            catalog_path = f.name
        calls = []
        market = {"id": "market-1", "sportsMarketType": "moneyline"}

        def get(_url, params=None, **_kwargs):
            params = dict(params or [])
            calls.append(params)
            if "end_date_min" in params:
                return [market]
            return []

        try:
            with patch.object(C, "SPORTS_CATALOG", catalog_path), \
                 patch.object(C, "MAX_LEAGUE_TAGS", 0), \
                 patch.object(C, "MAX_PAGES_PER_TAG", 2), \
                 patch.object(C, "MARKETS_PER_TAG", 2), \
                 patch.object(SC, "assert_network"), \
                 patch.object(SC, "_get", side_effect=get):
                rows = SC.fetch_all_markets(verbose=False, workers=1, budget_sec=5)
            self.assertTrue(SC.FETCH_CORE_COMPLETE)
            self.assertTrue(rows[0]["__core_candidate"])
            self.assertIn("end_date_min", calls[0], "core strategy query must precede broad crawl")
            self.assertNotIn("end_date_min", calls[-1])
        finally:
            os.unlink(catalog_path)

    def test_core_page_cap_marks_coverage_incomplete(self):
        import json
        import tempfile
        from unittest.mock import patch
        from polytrade import scan as SC

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                         delete=False) as f:
            json.dump([{"primaryTagId": "tag-1", "name": "League"}], f)
            catalog_path = f.name
        market = {"id": "market-1", "sportsMarketType": "moneyline"}
        def get(_url, params=None, **_kwargs):
            return [market] if "end_date_min" in dict(params or []) else []
        try:
            with patch.object(C, "SPORTS_CATALOG", catalog_path), \
                 patch.object(C, "MAX_LEAGUE_TAGS", 0), \
                 patch.object(C, "MAX_PAGES_PER_TAG", 1), \
                 patch.object(C, "MARKETS_PER_TAG", 1), \
                 patch.object(SC, "assert_network"), \
                 patch.object(SC, "_get", side_effect=get):
                SC.fetch_all_markets(verbose=False, workers=1, budget_sec=5)
            self.assertFalse(SC.FETCH_CORE_COMPLETE)
            self.assertTrue(any("core_incomplete" in issue for issue in SC.FETCH_COVERAGE_ISSUES))
        finally:
            os.unlink(catalog_path)


class TestCoreCandidateGuard(unittest.TestCase):
    """
    bug #12（2026-10-15）：核心抓取「静默变空」会让系统一切正常地停止下注。
    ========================================================================
    实测（直接打真实 gamma API）：
        sports_market_types=moneyline -> n=20（正常）
        sports_market_types=zzz      -> n=0   （**不是报错**，是空列表）
        zzzparam=1（未知参数名）      -> 与不传一样（**被静默忽略**）
    于是 fetch_core 把「空列表」和「正常抓到底」返回成同一个 issue=None，
    FETCH_CORE_COMPLETE 照样为 True、简报一切正常 —— 但永远没有下注候选。

    还实测到两条隐式耦合，必须锁住，否则后人一「简化」就炸：
        end_date_min/max 单独用                  -> HTTP 500
        end_date_min/max + sports_market_types   -> 正常（63 个，63/63 在窗口内）
        child_moneyline / first_half_moneyline 作过滤值 -> n=0（死值）
    """

    def test_core_min_candidates_threshold_is_low_and_positive(self):
        """阈值只能用来抓「整块塌掉」，不能变成日常噪音。"""
        self.assertGreater(C.CORE_MIN_CANDIDATES, 0, "阈值为 0 等于没有守卫")
        self.assertLessEqual(
            C.CORE_MIN_CANDIDATES, 100,
            "阈值 %d 太高会把正常日子误报成故障（正常核心候选是几百到上千）"
            % C.CORE_MIN_CANDIDATES)

    def test_core_candidates_are_tracked_globally(self):
        """核心候选数必须被记录，否则「过少」这件事没有任何入口能发现。"""
        from polytrade import scan as SC
        self.assertTrue(hasattr(SC, "FETCH_CORE_CANDIDATES"))

    def test_empty_core_is_recorded_as_an_issue(self):
        """
        核心候选为 0 时必须写进 FETCH_COVERAGE_ISSUES（进而进 runs.note 和简报）。
        构造的正是「取值失效」的真实形态：fetch_core 全部返回空列表且 issue=None。
        """
        from unittest import mock
        from polytrade import scan as SC
        with mock.patch.object(SC, "_get", return_value=[]):
            with mock.patch.object(SC, "assert_network", return_value=None):
                try:
                    SC.fetch_all_markets(verbose=False, workers=1, budget_sec=5)
                except Exception:
                    pass   # 后续步骤可能因数据不足报错，这里只关心 coverage issues
        notes = " ".join(SC.FETCH_COVERAGE_ISSUES)
        self.assertIn("core_empty", notes,
                      "核心候选为 0 竟然没有被记录 —— 系统会静默停注：%s" % notes)

    def test_digest_grades_core_breakage_as_severe(self):
        """
        简报分级：「决策样本不可信」才该是严重，宽扫缺口只能是提示。
        否则 page_cap（每轮 50+ 联赛）会让简报每轮报警，告警变噪音 ——
        而那正是「静默少采能藏两天」的土壤。
        """
        import inspect
        from polytrade import digest as DG
        src = inspect.getsource(DG.health_check)
        self.assertIn("core_empty", src, "digest 不认识 core_empty，静默停注不会被告警")
        self.assertIn("core_incomplete", src)
        i = src.find("core_incomplete")
        self.assertIn("提示", src[i:i + 900],
                      "宽扫缺口没有被降级为提示 —— 简报会每轮报警")

    def test_bet_candidates_blocked_when_core_is_too_small(self):
        """
        核心候选过少时必须禁止下注。
        若只剩几个候选还照常下注，等于在一个有偏的小子集上下注，比不下注更糟。
        """
        import inspect
        from polytrade import scan as SC
        src = inspect.getsource(SC.run_scan)
        self.assertIn("CORE_MIN_CANDIDATES", src,
                      "run_scan 没有用候选数量守卫拦住下注")


if __name__ == "__main__":
    unittest.main(verbosity=2)
