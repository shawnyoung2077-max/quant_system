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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--settle-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--dry", action="store_true", help="只写观测，不建纸面下注")
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

    def out(s):
        lines.append(str(s))
        print(s, flush=True)

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
            out("[1/4] 结算已到期市场 ...")
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
            out("[2/4] 扫描 Polymarket ...")
            r = SC.run_scan(verbose=True, dry=args.dry)
            out("      %s" % r)

        # 参照价（Pinnacle de-vig）。额度受限：内部有缓存与预算器，
        # 且只抓「Polymarket 有对应市场」的联赛 ⇒ 约 150 次/月（预算 400）。
        # 失败不影响主流程（参照价是增强项，不是必需项）。
        if not args.report_only and not args.settle_only and not args.no_ref:
            try:
                from . import refodds as RO
                from . import match as MT
                out("[3/4] 抓参照价 + 匹配赛程 ...")
                nf = RO.fetch_all(verbose=False)
                nm, _ = MT.run_match(verbose=False)
                conn = DB.connect()
                filled = RO.fill_ref_fair(conn)
                conn.close()
                out("      赛程 %d 条 / 匹配 %d 个市场 / 填入 ref_fair %d 行"
                    % (nf, nm, filled))
            except Exception as e:
                out("      参照价步骤跳过: %s" % str(e)[:140])

        if not args.scan_only and not args.settle_only:
            out("[4/4] 生成报告 ...")
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
