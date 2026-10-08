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
    args = ap.parse_args()

    from . import config as C
    from . import scan as SC
    from . import settle as SE
    from . import report as RP

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["=" * 90, "polytrade run @ %s" % stamp, "=" * 90]
    ok = True

    def out(s):
        lines.append(str(s))
        print(s, flush=True)

    try:
        if not args.report_only and not args.settle_only:
            out("[1/3] 扫描 Polymarket ...")
            r = SC.run_scan(verbose=True, dry=args.dry)
            out("      %s" % r)

        if not args.report_only and not args.scan_only:
            out("[2/3] 结算已到期市场 ...")
            n = SE.settle_bets(verbose=True)
            out("      已结算 %d 笔" % n)

        if not args.scan_only and not args.settle_only:
            out("[3/3] 生成报告 ...")
            txt = RP.report()
            p = RP.save_report(txt, "report_%s.txt" % dt.datetime.now().strftime("%Y%m%d"))
            RP.save_report(txt, "report_latest.txt")
            out("      报告已写出: %s" % p)
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
