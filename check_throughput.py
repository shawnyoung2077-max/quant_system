"""
check_throughput.py —— 核对「提高每次扫描上限」这一步是否真的起作用
============================================================================
背景：2026-10-16 把 MAX_NEW_BETS_PER_RUN 从 25 提到 40（单注同时 $2 -> $1）。
config.py 里承诺了改完必须核对下面 4 条，但**承诺不该只是注释**，
所以做成这个脚本，一条命令跑出来。

     1) runs.n_new_bets 是否从"3/12 顶到 25"变成"经常接近 40"
     2) 每日新建笔数是否真的从 38~55 升上去
        ★ 这一条最关键：没升就说明判断错了，应当回退
     3) 新注的 days_to_end 分布与旧注是否明显不同
     4) 新注的每笔收益率是否显著低于旧注
        （若是，说明"更远期更差"是真的，需回退并重估）

用法：
    python check_throughput.py

注意：样本可能还很少（改完当天只有一两轮），此时它会明确说"样本不足"，
而不是拿 3 笔数据下结论 —— 这个项目已经因为小样本误判过好几次。
"""

import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 变更生效时刻（UTC）。本地 12:05 的第一轮运行用的是新配置。
# entry_ts / run_ts 都是 UTC ISO 字符串。
CHANGE_TS = "2026-10-16T04:05:00+00:00"

MIN_RUNS_AFTER = 3      # 至少 3 轮之后才有资格判断
MIN_BETS_AFTER = 30     # 至少 30 笔结算之后才有资格判断收益


def _hdr(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def main():
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "polytrade.sqlite")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # ---------- 1) 每次扫描的新增笔数 ----------
    _hdr("1) 每次扫描的 n_new_bets（看是否经常接近新的上限 40）")
    rows = list(conn.execute(
        "SELECT run_ts, n_new_bets FROM runs WHERE kind='scan' ORDER BY run_id"))
    before = [r for r in rows if r["run_ts"] < CHANGE_TS]
    after = [r for r in rows if r["run_ts"] >= CHANGE_TS]
    for label, sub, cap in (("变更前（上限 25）", before, 25),
                            ("变更后（上限 40）", after, 40)):
        if not sub:
            print("   %-16s 无数据" % label)
            continue
        ns = [int(r["n_new_bets"] or 0) for r in sub]
        hit = sum(1 for x in ns if x >= cap)
        print("   %-16s 共 %2d 轮，新增笔数 %s" % (label, len(ns), ns))
        print("   %-16s 顶到上限(%d) 的有 %d / %d 轮；均值 %.1f"
              % ("", cap, hit, len(ns), sum(ns) / len(ns)))
    if len(after) < MIN_RUNS_AFTER:
        print("\n   ⚠ 变更后只有 %d 轮（< %d）—— **样本不足，先别看结论**"
              % (len(after), MIN_RUNS_AFTER))

    # ---------- 2) 每日新建笔数 ----------
    _hdr("2) 每日新建笔数（最关键：没升就说明判断错了，应回退）")
    for r in conn.execute(
            "SELECT entry_date d, COUNT(*) n, ROUND(SUM(stake),2) st "
            "FROM bets WHERE entry_date >= '2026-10-12' GROUP BY 1 ORDER BY 1"):
        mark = "  <-- 变更生效当天" if r["d"] >= "2026-10-16" else ""
        print("   %s  新建 %-4d 笔  $%-8.2f%s" % (r["d"], r["n"], r["st"], mark))
    d_before = [r["n"] for r in conn.execute(
        "SELECT COUNT(*) n FROM bets WHERE entry_date BETWEEN '2026-10-12' AND '2026-10-15' "
        "GROUP BY entry_date")]
    if len(after) >= MIN_RUNS_AFTER and d_before:
        print("   （变更前 4 天日均 %.1f 笔；变更后未满一天，先看趋势）"
              % (sum(d_before) / len(d_before)))

    # ---------- 3) 新注的 days_to_end 分布 ----------
    _hdr("3) days_to_end 分布：新注是否明显更远期")
    def dist(where):
        q = ("SELECT COUNT(*) n, AVG(days_to_end) a, MIN(days_to_end) mn, MAX(days_to_end) mx "
             "FROM bets WHERE days_to_end IS NOT NULL AND " + where)
        return conn.execute(q).fetchone()
    b = dist("entry_ts < '%s'" % CHANGE_TS)
    a = dist("entry_ts >= '%s'" % CHANGE_TS)
    print("   变更前：n=%s  均值=%s  范围=[%s, %s]"
          % (b["n"], "%.2f" % b["a"] if b["a"] else "-",
             "%.2f" % b["mn"] if b["mn"] else "-", "%.2f" % b["mx"] if b["mx"] else "-"))
    print("   变更后：n=%s  均值=%s  范围=[%s, %s]"
          % (a["n"], "%.2f" % a["a"] if a["a"] else "-",
             "%.2f" % a["mn"] if a["mn"] else "-", "%.2f" % a["mx"] if a["mx"] else "-"))
    if (a["n"] or 0) < MIN_BETS_AFTER:
        print("   ⚠ 变更后样本 %s 笔（< %d）—— 先别看差异" % (a["n"], MIN_BETS_AFTER))

    # ---------- 4) 新注的每笔收益率 ----------
    _hdr("4) 每笔收益率：新注是否显著低于旧注")
    def rets(where):
        return [r["pnl"] / r["stake"] for r in conn.execute(
            "SELECT pnl, stake FROM bets WHERE status='settled' AND stake>0 AND " + where)]
    def stat(rs):
        if len(rs) < 2:
            return None
        m = sum(rs) / len(rs)
        sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1))
        se = sd / math.sqrt(len(rs)) if sd > 0 else float("inf")
        return len(rs), m, sd, (m / se if se else 0)
    rb, ra = rets("entry_ts < '%s'" % CHANGE_TS), rets("entry_ts >= '%s'" % CHANGE_TS)
    sb, sa = stat(rb), stat(ra)
    for label, s in (("变更前", sb), ("变更后", sa)):
        if s is None:
            print("   %s：结算样本不足（%d 笔）" % (label, len(rb if label == "变更前" else ra)))
        else:
            print("   %s：n=%-4d 每笔均值=%+7.2f%%  标准差=%.2f  t=%+.2f"
                  % (label, s[0], 100 * s[1], s[2], s[3]))
    if sa is None or sa[0] < MIN_BETS_AFTER:
        print("\n   ⚠ 变更后已结算 %d 笔（< %d）—— **不足以判断收益差异**"
              % (len(ra), MIN_BETS_AFTER))
    print()
    print("提醒：本脚本只报事实。够不够下结论，按 docs/EXPERIMENT_stopping_rule.md 的")
    print("      检查点规则来（300 笔 / 30 天），不要提前挑对自己有利的口径。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
