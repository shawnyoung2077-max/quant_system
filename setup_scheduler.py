"""setup_scheduler.py - 注册 Windows 定时任务（每 2 小时跑一次 polytrade）

踩过的坑（已处理）：
  1. 计划任务的 PATH 极简，必须用绝对 python 路径
  2. .bat 要 GBK 编码，否则中文/特殊字符会崩
  3. 用 WMI 启动可以脱离会话（否则关掉窗口就停）
  4. "只在用户登录时运行"会导致静默失败 —— 必须设为不管用户是否登录都运行
"""

import os
import subprocess
import sys

ROOT = r"D:\26050\Documents\quant_system"
PY = r"C:\Users\26050\AppData\Local\Programs\Python\Python311\python.exe"
BAT = os.path.join(ROOT, "polytrade_run.bat")
TASK = "polytrade_scan"

BAT_TEXT = (
    "@echo off\r\n"
    "chcp 65001 >nul\r\n"
    "cd /d \"%s\"\r\n"
    "\"%s\" -m polytrade.run >> \"%s\\polytrade_logs\\cron.log\" 2>&1\r\n"
    % (ROOT, PY, ROOT)
)


def main():
    with open(BAT, "w", encoding="gbk", errors="replace") as fh:
        fh.write(BAT_TEXT)
    print("已写出 %s" % BAT)
    print(open(BAT, encoding="gbk").read())

    # 删除已存在的同名任务
    subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"],
                   capture_output=True, text=True)

    # 注册：每 2 小时一次
    cmd = ["schtasks", "/Create", "/TN", TASK,
           "/TR", '"%s"' % BAT,
           "/SC", "HOURLY", "/MO", "2",
           "/ST", "00:05",
           "/RL", "HIGHEST", "/F"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print("STDOUT:", r.stdout.strip())
    print("STDERR:", r.stderr.strip())

    q = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"],
                       capture_output=True, text=True)
    txt = q.stdout
    for key in ("TaskName", "Status", "Next Run Time", "Schedule Type",
                "Repeat: Every", "Logon Mode"):
        for ln in txt.splitlines():
            if ln.lower().startswith(key.lower()):
                print(ln.strip())


if __name__ == "__main__":
    main()
