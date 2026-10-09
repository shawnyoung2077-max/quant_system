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
        if not args.report_only and not args.settle_only:
            out("[1/4] 扫描 Polymarket ...")
            r = SC.run_scan(verbose=True, dry=args.dry)
            out("      %s" % r)

        # 参照价（Pinnacle de-vig）。额度受限：内部有缓存与预算器，
        # 且只抓「Polymarket 有对应市场」的联赛 ⇒ 约 150 次/月（预算 400）。
        # 失败不影响主流程（参照价是增强项，不是必需项）。
        if not args.report_only and not args.settle_only and not args.no_ref:
            try:
                from . import refodds as RO
                from . import match as MT
                out("[2/4] 抓参照价 + 匹配赛程 ...")
                nf = RO.fetch_all(verbose=False)
                nm, _ = MT.run_match(verbose=False)
                conn = DB.connect()
                filled = RO.fill_ref_fair(conn)
                conn.close()
                out("      赛程 %d 条 / 匹配 %d 个市场 / 填入 ref_fair %d 行"
                    % (nf, nm, filled))
            except Exception as e:
                out("      参照价步骤跳过: %s" % str(e)[:140])

        if not args.report_only and not args.scan_only:
            out("[3/4] 结算已到期市场 ...")
            n = SE.settle_bets(verbose=True)
            out("      已结算 %d 笔" % n)

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
        lines.append("ERROR:\n" + traceback.format_exc())
        print(traceback.format_exc(), file=sys.stderr)

    logp = os.path.join(C.LOG_DIR, dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    with open(logp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
