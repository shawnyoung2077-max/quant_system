"""
run.py - 主入口：扫描 + 结算 + 报告（供定时任务调用）
============================================================================
用法：
    python -m polytrade.run              # 完整流程
    python -m polytrade.run --scan-only
    python -m polytrade.run --settle-only
    python -m polytrade.run --report-only
    python -m polytrade.run --dry        # 只扫描写观测，不下注
"""

import argparse
import datetime as dt
import os
import sys
import traceback

# ---------------------------------------------------------------------------
# ★★★ 2026-10-15：入口处**显式**把 stdout 设成「UTF-8 + 出错不抛」
# ---------------------------------------------------------------------------
# 背景（实测）：
#   本机默认 ANSI 代码页是 cp936。当 stdout **被重定向到文件**时，
#   Python 用的是 locale.getpreferredencoding(False) = cp936，
#   **即 batch 里写 `chcp 65001` 也没用** —— 实测两种情况下
#   sys.stdout.encoding 都是 'gbk'：
#       不带 chcp：stdout.encoding = gbk
#       带 chcp 65001：stdout.encoding = gbk      ← chcp 管的是控制台，不管重定向
#   而我们的日志里有 ⚠ / ✓ / ✗ / ★ / → 这些**不在 GBK 里**的字符，
#   print 它们会抛 UnicodeEncodeError。
#
# 之前为什么没炸？**纯属巧合**：report.py / digest.py / invariants.py /
#   equity.py / match.py / refodds.py / aiforecast.py / papermm.py
#   各自在模块层做了一次 `sys.stdout.reconfigure(errors="replace")`，
#   谁先被 import 谁就把全局修好了。run.py 恰好 import 了它们，所以没事。
#   polytrade.ensureproxy 只 import config，没这个副作用 —— 于是它一 print ✓ 就崩。
#
# ⇒ 这种"靠 import 副作用续命"的东西必须消灭：**编码安全要在入口显式建立**，
#   而不是依赖某个模块恰好被导入。顺带把编码统一成 UTF-8，
#   这样 cron.log 也能直接用 UTF-8 读（之前是 GBK，我为此误读过一次日志、
#   还据此下了一个错误结论，见 docs/DEBUG_run_timeout_30min.md 第 5 节）。
def _setup_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_setup_stdout()


def plan_fetch_budget(budget_left, reserve, cap):
    """
    把「整轮还剩多少秒」折算成「这次抓取允许用多少秒」。

    规则（2026-10-14）：
      * 上限是 cap（配置里的 FETCH_TIME_BUDGET_SEC）—— 抓取本身不该无限跑；
      * 再减去 reserve —— 那是留给参照价/AI/**报告与看门狗**的，不许被吃掉；
      * 但至少给 60 秒：宁可抓一点点，也不要因为算术把抓取整个关掉
        （抓取是唯一"错过就补不回来"的步骤）。

    ⚠ 报告步骤永远不需要预算 —— 它是本地的、很便宜。所以"预算耗尽"的正确
      含义是"跳过增强项"，不是"跳过留痕"。
    """
    return min(float(cap), max(60.0, float(budget_left) - float(reserve)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--settle-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只写观测，不建纸面下注")
    ap.add_argument("--no-ai", action="store_true", help="跳过 AI 赛前预测（省额度）")
    ap.add_argument("--no-ref", action="store_true", help="跳过参照价步骤（省额度）")
    args = ap.parse_args()

    from . import config as C
    from . import scan as SC
    from . import settle as SE
    from . import report as RP
    from . import db as DB

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["=" * 90, "polytrade run @ %s" % stamp, "=" * 90]
    ok = True

    # ★★★ 2026-10-14：整轮墙钟预算 ★★★
    #   实测事故：那次运行在 [4/5] 抓参照价阶段跑过头，撞上计划任务的
    #   ExecutionTimeLimit(PT30M) 被**硬杀** —— 报告与看门狗都没执行，
    #   于是"没有信号"。而 ExecutionTimeLimit 本机改不了（改任务要账户密码）。
    #   对策：每一步开跑前问一句"还剩多少时间"，不够就跳过并**明确上报**。
    #   RUN_RESERVE_SEC 是留给报告/看门狗的，任何步骤都不许吃掉它。
    import time as _time
    _t0 = _time.time()
    skipped_steps = []

    def budget_left():
        return C.RUN_BUDGET_SEC - (_time.time() - _t0)

    def have_budget(need_sec, label):
        """够就跑，不够就登记跳过（绝不静默省略）。"""
        left = budget_left()
        if left < need_sec + C.RUN_RESERVE_SEC:
            skipped_steps.append("%s(剩%.0fs<需%.0fs)" % (label, left, need_sec))
            out("      ⚠ 预算不足，跳过 %s：剩余 %.0fs，需要 %.0fs + 保留 %ds"
                % (label, left, need_sec, C.RUN_RESERVE_SEC))
            return False
        return True

    def out(s):
        # ★ 2026-10-15：print 失败**绝不允许**影响流程。
        #   实测教训：out("[0/4] ⚠ 代理不可用 …") 被一层 try/except 包着，
        #   而 stdout 是 GBK，print 抛 UnicodeEncodeError 被静默吞掉 ——
        #   结果那一行**只进了 lines（写文件），没进 stdout（cron.log）**，
        #   两条输出悄悄分叉。留痕函数的首要职责是"一定留得下"，
        #   所以这里自己兜住异常，并且始终先 append 再 print。
        lines.append(str(s))
        try:
            print(s, flush=True)
        except Exception:
            pass

    try:
        # ★★ 2026-10-12 改顺序：结算提到扫描之前 ★★
        #   原来顺序是 scan → refodds → settle → report。
        #   实测后果：一次结算 90 笔、释放 $900 敞口之后，**同一次运行里的扫描
        #   看到的仍是结算前的 $1020/$300**，于是全部被风险闸拦下，
        #   要再等 2 小时下一次运行才能真正用上这笔额度。
        #   结算很快（~10 秒），提到最前面就能让释放的额度立刻可用。
        #   另外代理自愈也放在最前面，否则扫描必然失败。
        try:
            if not C.AUTO_START_PROXY:
                raise RuntimeError("")
            _p = C.ensure_proxy(verbose=True)
            if not _p:
                out("[0/4] ⚠ 代理不可用 —— 后面的扫描会失败（见下方错误）")
        except Exception:
            pass

        if not args.report_only and not args.scan_only:
            out("[1/5] 结算已到期市场 ...")
            n = SE.settle_bets(verbose=True)
            out("      已结算 %d 笔" % n)
            # ★ 净值快照：没有它就无法定义「回撤」。必须在结算之后拍。
            try:
                from . import equity as EQ
                s = EQ.snapshot()
                out("      净值 $%.2f（已实现 $%+.2f / 未实现 $%+.2f / 敞口 $%.0f）"
                    % (s["equity"], s["realized_pnl"], s["unrealized_pnl"],
                       s["open_stake"]))
            except Exception as e:
                out("      净值快照失败: %s" % str(e)[:140])

        if not args.report_only and not args.settle_only:
            out("[2/5] 扫描 Polymarket ...")
            # ★ 抓取预算 = min(配置上限, 整轮剩余 - 保留)。
            #   不这样做的话，抓取会把留给参照价/AI/报告的额度吃光。
            fetch_budget = plan_fetch_budget(budget_left(),
                                             C.RUN_RESERVE_SEC,
                                             C.FETCH_TIME_BUDGET_SEC)
            out("      本轮剩余预算 %.0fs -> 抓取预算 %.0fs"
                % (budget_left(), fetch_budget))
            r = SC.run_scan(verbose=True, dry=args.dry, budget_sec=fetch_budget)
            out("      %s" % r)

        # 参照价（Pinnacle de-vig）。额度受限：内部有缓存与预算器，
        # 且只抓「Polymarket 有对应市场」的联赛 ⇒ 约 150 次/月（预算 400）。
        # 失败不影响主流程（参照价是增强项，不是必需项）。
        if (not args.report_only and not args.settle_only and not args.no_ref
                and have_budget(120, "参照价+匹配赛程")):
            try:
                from . import refodds as RO
                from . import match as MT
                out("[4/5] 抓参照价 + 匹配赛程 ...")
                # ★ 本步骤曾经把整轮运行拖死（实测跑了 1100s，预算是 655s），
                #   而且内部一句都不打印，外部看起来就是卡死。
                #   现在两层约束：步骤有自己的上限 REFODDS_MAX_SEC，
                #   且抓取与匹配各自还要能吃到子预算。
                step_budget = max(90.0, min(C.REFODDS_MAX_SEC,
                                            budget_left() - C.RUN_RESERVE_SEC - 90.0))
                out("      本步预算 %.0fs（整轮剩余 %.0fs，上限 %ds）"
                    % (step_budget, budget_left(), C.REFODDS_MAX_SEC))
                t_step = _time.time()
                nf = RO.fetch_all(verbose=True,
                                  budget_sec=max(60.0, step_budget * 0.4))
                match_budget = max(30.0, step_budget - (_time.time() - t_step))
                out("      匹配赛程预算 %.0fs" % match_budget)
                nm, _ = MT.run_match(verbose=True, budget_sec=match_budget)
                conn = DB.connect()
                filled = RO.fill_ref_fair(conn)
                conn.close()
                out("      赛程 %d 条 / 匹配 %d 个市场 / 填入 ref_fair %d 行"
                    % (nf, nm, filled))
            except Exception as e:
                out("      参照价步骤跳过: %s" % str(e)[:140])

        # ★ AI 赛前概率预测（研究层，不影响下注决策）
        #   下注仍然由确定性代码决定 —— 否则实验规则就不固定了。
        #   AI 只负责：赛前独立给一个概率、落库、赛后由固定公式评估。
        if (not args.report_only and not args.settle_only
                and not args.no_ai and getattr(C, "AI_FORECAST_ENABLED", False)
                and have_budget(90, "AI 赛前预测")):
            try:
                from . import aiforecast as AF
                out("[3/5] AI 赛前概率预测（落库，不下注）...")
                r = AF.run(verbose=True)
                out("      %s" % r)
            except Exception as e:
                out("      AI 预测步骤跳过: %s" % str(e)[:160])

        out("      整轮耗时 %.0fs（预算 %ds，保留 %ds）"
            % (_time.time() - _t0, C.RUN_BUDGET_SEC, C.RUN_RESERVE_SEC))
        if skipped_steps:
            out("      ⚠ 因预算被跳过的步骤：%s" % "; ".join(skipped_steps))

        if not args.scan_only and not args.settle_only:
            out("[5/5] 生成报告 ...")
            txt = RP.report()
            p = RP.save_report(txt, "report_%s.txt" % dt.datetime.now().strftime("%Y%m%d"))
            RP.save_report(txt, "report_latest.txt")
            out("      报告已写出: %s" % p)
            # 看门狗 + 每日简报（人可读，打开就能看）
            try:
                from . import digest as DG
                issues, _ = DG.health_check()
                dp = DG.save_digest()
                out("      简报已写出: %s" % dp)
                if issues:
                    out("      ! 发现 %d 个异常:" % len(issues))
                    for i in issues:
                        out("        %s" % i)
            except Exception as e:
                out("      简报步骤跳过: %s" % str(e)[:120])
            # ★ 预算跳过必须落到**人一定会打开的**简报里，不能只躺在日志里。
            #   否则"这次少做了两件事"就变成了新的静默失败。
            if skipped_steps:
                try:
                    dp2 = os.path.join(C.OUT_DIR, "digest_latest.txt")
                    with open(dp2, "a", encoding="utf-8") as fh:
                        fh.write("\n【本次运行因墙钟预算跳过的步骤】\n")
                        for s in skipped_steps:
                            fh.write("  - %s\n" % s)
                        fh.write("  （原因：计划任务 ExecutionTimeLimit=PT30M 且本机改不了，"
                                 "详见 config.RUN_BUDGET_SEC 注释）\n")
                except Exception:
                    pass
        elif args.report_only:
            txt = RP.report()
            RP.save_report(txt, "report_latest.txt")
            out("报告已刷新")
    except Exception:
        ok = False
        err = traceback.format_exc()
        lines.append("ERROR:\n" + err)
        print(err, file=sys.stderr)
        # ★★ 2026-10-12：出错时也必须跑看门狗并写简报 ★★
        #   实际事故：代理挂掉 -> 第 1 步抛异常 -> 第 4 步（报告/看门狗）永远不执行
        #   -> digest_latest.txt 和 report_latest.txt 一起过期
        #   -> **没有任何信号**，系统静默死了 36 小时，直到用户问起才发现。
        #   这是本会话第 4 次"静默失败"。所以：失败也必须留下痕迹。
        try:
            from . import digest as DG
            try:
                DG.health_check(verbose=True)
            except Exception as e2:
                print("  （健康检查本身也失败：%r）" % e2, flush=True)
            try:
                dp = DG.save_digest()
                out("      ⚠ 简报已写出（含失败记录）: %s" % dp)
            except Exception as e2:
                print("  简报写入失败: %r" % e2, flush=True)
            # 简报是纯文本，把失败摘要直接追上去，保证打开文件就看得到
            try:
                p = os.path.join(C.OUT_DIR, "digest_latest.txt")
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write("\n【本次运行失败】\n")
                    for ln in err.strip().splitlines()[-14:]:
                        fh.write("  " + ln + "\n")
            except Exception:
                pass
        except Exception as e2:
            print("  失败回执步骤跳过: %r" % e2, flush=True)

    logp = os.path.join(C.LOG_DIR, dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    with open(logp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
