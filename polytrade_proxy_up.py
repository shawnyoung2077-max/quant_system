"""
polytrade_proxy_up.py —— 计划任务 polytrade_proxy_up 的执行入口（仓库根目录）
============================================================================
为什么不直接用 .bat（2026-10-15 实测教训）：

  .bat + LogonType=Interactive 的组合下，`schtasks /Run` 会返回成功、
  LastTaskResult=0、LastRunTime 正常更新，**但日志文件里一个字都没有**。
  也就是说"任务跑了"与"任务做了什么"之间断了线，排查时完全瞎。
  （对照：同样形态的 .bat 任务 polytrade_scan 用 LogonType=Password 就正常；
    而本任务必须是 Interactive 才能进用户的桌面会话 —— 正是这个组合哑掉。）

  现在改为让计划任务**直接执行 python.exe**、把这个脚本作为参数，
  日志由 polytrade.ensureproxy 自己写（见那里的 _Tee）。
  形态与当初验证通过的那个探针任务完全一致（python.exe + 脚本参数 → 落在 Session 2）。

注册命令见 docs/DEBUG_clash_singleton_session0.md 第 10.3 节。
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from polytrade import ensureproxy  # noqa: E402

if __name__ == "__main__":
    sys.exit(ensureproxy.main())
