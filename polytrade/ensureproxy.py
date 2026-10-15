"""
ensureproxy.py —— 只负责「把代理拉起来」，不做任何抓取
============================================================================
为什么单独一个入口（2026-10-15）：

  背景：主任务 polytrade_scan 是 LogonType=Password，跑在**会话 0**（服务会话，无桌面）。
        会话 0 里拉起 Clash Verge 会得到一个看不见的窗口 **并抢走单例锁**，
        把用户自己的 Clash 一起弄坏（见 docs/DEBUG_clash_singleton_session0.md）。
        所以 config.ensure_proxy() 在无桌面会话里**拒绝**启动 GUI。

  但代价是：Clash 没在跑的时候，后台任务就完全没救了。实测 2026-10-14~15 的 24 小时里
        **4 次运行颗粒无收**（12:12 / 14:28 / 21:05 / 次日 09:58），
        其中 09:58 那次是结构性无解 —— 机器 09:47 开机、用户 10:01 登录，任务 09:58 触发。

  解法：让"拉代理"这件事在一个**有桌面的会话**里执行，那就是这个入口。
        它由计划任务 polytrade_proxy_up 承载，注册为 **LogonType=Interactive** ——
        这种任务不需要账户密码（实测 `schtasks /Create /RU <user> /IT` 直接成功），
        而且 `/Run` 会跑在**用户登录的那个桌面会话**里（实测 session=2）。
        也就是说：会话 0 的主任务遇到代理不在时，**委托**给这个任务去做，
        而不是自己去拉 GUI。

  关系图：
        polytrade_scan (会话0，无桌面)  ──代理不在──▶  schtasks /Run polytrade_proxy_up
                                                              │
                                                              ▼
                                              polytrade_proxy_up (用户的桌面会话)
                                                              │
                                              ensure_proxy() 在这里是合法的
                                                              ▼
                                              Clash Verge GUI 正常启动、托盘正常
"""

import sys

# ★ 2026-10-15：入口显式建立编码安全 —— 见 run.py 顶部那段说明。
#   实测事故：本入口只 import 了 config（它没有 reconfigure 的副作用），
#   于是 print("✓ …") 在 GBK 的 stdout 上抛 UnicodeEncodeError 直接崩溃。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    from . import config as C
    print("=" * 70, flush=True)
    print("[ensureproxy] 在 Session=%s 里尝试保证代理可用" % _session(), flush=True)
    p = C.ensure_proxy(wait_sec=90, verbose=True)
    if p:
        print("[ensureproxy] ✓ 代理已就绪: %s" % p, flush=True)
        return 0
    print("[ensureproxy] ✗ 代理仍不可用（可能未注册辅助任务，或端口被占用）", flush=True)
    return 1


def _session():
    try:
        import ctypes
        s = ctypes.c_ulong(0)
        ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(s))
        return int(s.value)
    except Exception:
        return "?"


if __name__ == "__main__":
    sys.exit(main())
