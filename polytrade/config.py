"""
config.py - 纸面交易系统的全部可调参数（集中一处，便于事后审计）
============================================================================
原则：**任何影响结果的参数都必须显式写在这里**，
      不允许散落在代码里。这样事后回看时能准确知道
      "当时是用什么规则跑的"。
"""

import os
import socket

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)

# ---------------------------------------------------------------------------
# ★ 代理：必须显式设置，不能依赖系统代理
# ---------------------------------------------------------------------------
# 为什么必须写死：
#   实测本机的 HTTP_PROXY/HTTPS_PROXY 环境变量在【用户级和系统级都是空的】。
#   Python 的 requests 走的是 urllib.getproxies()，而它读的是
#   HKCU\...\Internet Settings（用户级注册表，只在该用户登录时加载）。
#   任务以「不管用户是否登录都运行」启动时，HKCU 是否被加载并不确定
#   —— 一旦没加载，Polymarket 就访问不了，任务静默失败。
#   所以这里显式把代理塞进环境变量，消除整类不确定性。
#
# 自动探测：端口在监听才设，否则跳过（换网络/关代理时仍能用）。
PROXY_CANDIDATES = ["http://127.0.0.1:7897", "http://127.0.0.1:7890"]

def _detect_proxy():
    for p in PROXY_CANDIDATES:
        try:
            hostport = p.split("//", 1)[1]
            host, port = hostport.rsplit(":", 1)
            s = socket.create_connection((host, int(port)), timeout=1.0)
            s.close()
            return p
        except Exception:
            continue
    return None

PROXY = os.environ.get("POLYTRADE_PROXY") or _detect_proxy()
if PROXY:
    os.environ.setdefault("HTTP_PROXY", PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)
    os.environ.setdefault("http_proxy", PROXY)
    os.environ.setdefault("https_proxy", PROXY)
    # 本机地址不走代理
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")

# ---------------------------------------------------------------------------
# ★★ 代理自愈（2026-10-12）★★
# ---------------------------------------------------------------------------
# 实际事故：Clash Verge 没在跑 → 7897 不监听 → 473 个 tag 全部请求失败。
# 系统从 10-10 20:45 起整整**静默死了 36 小时**，直到用户问起才发现。
#
# 根因不只是网络，而是：Clash Verge 是一个**GUI 程序**，
# 服务 clash_verge_service 只负责 TUN/提权辅助，**内核 verge-mihomo 是 GUI 拉起来的**。
# 所以机器重启/注销后，如果没人登录并启动它，代理就一直不在。
# 而我们的定时任务是在后台跑的，指望不上这一点。
#
# 对策：启动时若发现端口没监听，就主动把 Clash Verge 拉起来，并等它把内核带起来。
# 拉不起来也不吞掉 —— 后面 scan 的预检会抛出明确错误，看门狗也会记录。
#
# ★★★ 2026-10-13 重大修正：这个"自愈"本身造成了更严重的事故 ★★★
#
#   事故现象：用户点击 Clash Verge 图标，**什么窗口都不弹**。
#
#   根因链（实测）：
#     polytrade_run.bat → cmd.exe(会话0) → python.exe(会话0)
#       → ensure_proxy() 在这里 Popen 了 clash-verge.exe
#       → clash-verge 跑在【会话 0】，那里没有交互式桌面
#       → 日志报 [[Tray]] tray icon error (os error -2147467259)，窗口也创建不了
#       → 但它抢到了【单例锁】（本地端口 53263 + token）
#       → 用户之后每一次点图标，新实例都检测到单例、把"显示窗口"转发给会话 0 那个，
#         日志反复打印「检测到从单例模式恢复应用窗口 / 窗口激活成功」，
#         而那个窗口在会话 0 里，用户永远看不到。
#
#   所以：**在非交互式会话里启动 GUI 程序，不只是"没用"，是会抢走单例、
#   把用户自己的 GUI 一起弄坏。** 这比原来的问题（代理没起来）更难排查。
#
#   修正（两件，缺一不可）：
#     1) 只有当**当前进程就在交互式会话**里时才允许拉起；会话 0 一律不启。
#        代价是后台任务失去自愈能力 —— 但这本来就是个错误的能力。
#     2) 已经坏掉的那些怎么办？见 kill_invisible_gui()：
#        每次 ensure_proxy() 开头都会去找「会话号 != 桌面会话」的 clash-verge.exe
#        并结束它，把单例锁还回来。只杀 GUI，不碰 verge-mihomo 和服务，
#        所以代理不受影响。
#   另外：真要在无桌面环境里保代理，正解不是拉 GUI，而是让它**开机自启到桌面会话**。
AUTO_START_PROXY = True
# ★ 2026-10-15：无桌面会话用来"委托拉代理"的交互式任务名。
#   它的主体是 polytrade_proxy_up.bat（跑 polytrade.ensureproxy），
#   注册为 LogonType=Interactive（不需要账户密码），因此会跑在用户的桌面会话里。
PROXY_HELPER_TASK = "polytrade_proxy_up"
PROXY_EXE_CANDIDATES = [
    r"C:\Program Files\Clash Verge\clash-verge.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "clash-verge", "Clash Verge.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Clash Verge", "Clash Verge.exe"),
]


def interactive_session():
    """
    当前进程是否在**用户的桌面会话**里（即能不能创建窗口和托盘图标）。

    Windows 的会话 0 是服务会话，没有交互式桌面：
    在那里启动 GUI 程序会得到一个永远看不见的窗口，而且它会抢走单例锁，
    导致用户自己点图标也打不开 —— 2026-10-13 就是这么坏掉的。

    ★★★ 2026-10-14 修正：这个函数的第一版**判错了** ★★★
      第一版在会话 0 时的判据是「tasklist 里有没有 explorer.exe」，
      但 `tasklist /FI "IMAGENAME eq explorer.exe"` 会列出**所有会话**的
      explorer。本机实测有会话 1 和会话 2 各一个 explorer，
      于是会话 0 里的进程看到 explorer.exe 就以为自己能显示窗口，
      又把 GUI 拉进了会话 0 —— 10-14 09:24 的日志里 Tray 报错、
      单例被抢，全是同一个事故的重演（也就是说：**修正本身失效了**）。

      正确的判据只能是**会话号本身**：会话号 != 0 就是桌面会话；
      会话 0 必须看到会话号同为 0 的 explorer 才算（现实中几乎不会发生）。
    """
    try:
        me = _session_of_pid(os.getpid())
    except Exception:
        return False
    if me is None:
        return False
    if me != 0:
        return True
    # 本进程就在会话 0：要求 explorer 也**在会话 0**（而不是"任何会话里有 explorer"）
    try:
        return any(sid == 0 for _pid, sid in _tasklist_sessions("explorer.exe"))
    except Exception:
        return False


def _session_of_pid(pid):
    """返回进程所在会话号；查不到返回 None。"""
    try:
        import ctypes
        s = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(s))
        if not ok:
            return None
        return int(s.value)
    except Exception:
        return None


def _run_task(name):
    """
    用 `schtasks /Run` 触发一个已注册的计划任务；返回是否成功。

    ★ 为什么用这个而不是别的：本机不是管理员，没法提权，
      也没法用 CreateProcessAsUser 把一个进程塞进别的会话。
      但 `schtasks /Run` 走的是计划任务服务，**可以**触发一个
      LogonType=Interactive 的任务，让它跑在用户的桌面会话里 ——
      这正是"把 GUI 拉到有桌面的地方"所需要的全部能力。
    """
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", name],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _tasklist_sessions(image):
    """用 tasklist 列出某镜像名所有进程的 (pid, session)；失败返回 []。"""
    import csv as _csv
    import io
    import subprocess
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % image, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20)
    except Exception:
        return []
    out = []
    for row in _csv.reader(io.StringIO(r.stdout or "")):
        # 列： "Image Name","PID","Session Name","Session#","Mem Usage"
        if len(row) < 4 or row[0].strip().lower() != image.lower():
            continue
        try:
            out.append((int(row[1]), int(row[3])))
        except Exception:
            continue
    return out


def interactive_session_id():
    """
    返回**用户桌面所在的会话号**（用 explorer.exe 定位）；定位不到返回 None。

    这是"用户看得见窗口"的那个会话。跑在别的会话里的 GUI 进程，
    它创建的窗口用户永远看不到。
    """
    for _pid, sid in _tasklist_sessions("explorer.exe"):
        if sid != 0:
            return sid
    # 退化：本进程若在非 0 会话，就以本会话为准
    return _session_of_pid(os.getpid())


def kill_invisible_gui(verbose=True):
    """
    ★ 2026-10-13 自愈：结束掉跑在**会话 0** 里的 clash-verge.exe。

    为什么需要它：一旦 GUI 被错误地拉进会话 0，它没有桌面，窗口永远不可见，
    但它**已经抢到了单例锁**。此后用户每次点图标，新实例都只是把
    「显示窗口」转发给它，日志打印「窗口激活成功」，用户却什么都看不到 ——
    唯一的恢复手段就是把这个进程杀掉（或重启）。

    ★ 判据故意收得很窄：**只看会话 0**。
      Windows Vista 以后会话 0 是服务会话，**永远没有交互式桌面**，
      所以那里出现的 GUI 进程 100% 是隐形的，杀它绝不可能误伤用户。
      不采用「和 desktop session 不同就杀」的宽判据 —— 本机实测存在
      会话 1 和会话 2 两个 explorer（多会话登录/远程桌面），
      宽判据会误杀用户在另一个会话里正常使用的 GUI。
      实测记录：explorer.exe = [(13232, 1), (2324, 2)]。

    ★ 只杀 clash-verge.exe（GUI），**绝不碰** verge-mihomo.exe / 服务，
      所以代理本身不受影响 —— 实测 7897 在 GUI 被杀后仍正常。

    返回被杀掉的 pid 列表。杀不掉会打印原因（通常是拒绝访问 -> 需要重启）。
    """
    desk = interactive_session_id()
    if desk in (None, 0):
        if verbose:
            print("  [proxy] 本机没有可用的桌面会话，跳过 GUI 清理", flush=True)
        return []
    targets = [(pid, sid) for pid, sid in _tasklist_sessions("clash-verge.exe")
               if sid == 0]
    if not targets:
        return []
    if verbose:
        print("  [proxy] ⚠ 发现 %d 个跑在会话 0（无桌面）里的 Clash Verge GUI "
              "—— 它们会抢走单例锁、导致点图标无窗口，正在清理"
              % len(targets), flush=True)
    import subprocess
    killed = []
    for pid, sid in targets:
        try:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20)
            if r.returncode == 0:
                killed.append(pid)
                if verbose:
                    print("  [proxy]   ✓ 已结束 pid=%d（会话 %d）" % (pid, sid), flush=True)
            elif verbose:
                print("  [proxy]   ✗ 结束 pid=%d 失败：%s"
                      % (pid, ((r.stdout or "") + (r.stderr or "")).strip()[:200]),
                      flush=True)
                print("  [proxy]     拒绝访问 = 该进程令牌更高，只能重启机器清掉它。",
                      flush=True)
        except Exception as e:
            if verbose:
                print("  [proxy]   ✗ 结束 pid=%d 异常：%r" % (pid, e), flush=True)
    if killed and verbose:
        print("  [proxy] 已清掉 %d 个隐形 GUI，单例锁已释放 —— "
              "现在点 Clash 图标应该能正常弹窗。" % len(killed), flush=True)
    return killed


def ensure_proxy(wait_sec=45, verbose=True):
    """
    确保代理端口在监听。不在就尝试拉起 Clash Verge 并等待内核起来。

    返回最终可用的代理地址（或 None）。会顺便刷新 os.environ，
    因为 requests 是在请求时才读环境变量的。

    ⚠ 只在交互式会话里才会真的去拉起 GUI —— 见上面 2026-10-13 的事故记录。
    """
    global PROXY
    # ★ 先清理历史遗留的隐形 GUI：即使代理现在是好的也要做，
    #   因为那个进程会一直霸占单例锁，让用户点不开自己的 Clash。
    try:
        kill_invisible_gui(verbose=verbose)
    except Exception as e:
        if verbose:
            print("  [proxy] GUI 清理异常（忽略）: %r" % e, flush=True)
    p = _detect_proxy()
    if p:
        PROXY = p
        return p
    if not AUTO_START_PROXY:
        if verbose:
            print("  [proxy] 7897/7890 均未监听，且 AUTO_START_PROXY=False", flush=True)
        return None

    # ★ 2026-10-13：非交互式会话（通常是会话 0）绝不允许拉起 GUI。
    #   在那里启动会得到一个看不见的窗口，而且会抢走单例锁，
    #   把用户自己的 GUI 一起弄坏 —— 这正是那次事故。
    #
    # ★★★ 2026-10-15：光"拒绝"不够，改成"委托" ★★★
    #   只拒绝的代价，实测 24 小时里 **4 次运行颗粒无收**
    #   （10-14 12:12 / 14:28 / 21:05、10-15 09:58），
    #   全部是「代理不在 + 本进程无桌面 ⇒ 没人能拉起 GUI」。
    #   其中 09:58 那次结构性无解：机器 09:47 开机、用户 10:01 登录，
    #   而任务是 09:58 触发的 —— 那一刻桌面上根本没人。
    #
    #   现在改为：委托给 polytrade_proxy_up 这个 **Interactive** 任务。
    #   实测 `schtasks /Create /RU <user> /IT` **不需要账户密码**就能注册，
    #   而且 `/Run` 会跑在用户登录的桌面会话里（实测 session=2）——
    #   在那里启动 GUI 是合法的，托盘和窗口都正常。
    #   所以：会话 0 **自己**永远不拉 GUI，但它可以请桌面会话里的任务去拉。
    if not interactive_session():
        if verbose:
            print("  [proxy] ⚠ 端口未监听，且本进程无桌面（会话 %s）——"
                  "自己拉 GUI 会抢走单例锁，改为委托交互式任务 %s"
                  % (_session_of_pid(os.getpid()), PROXY_HELPER_TASK), flush=True)
        if not _run_task(PROXY_HELPER_TASK):
            if verbose:
                print("  [proxy] ✗ 无法触发辅助任务 %s（未注册？）—— "
                      "请手动启动 Clash，或确认它的开机自启。" % PROXY_HELPER_TASK,
                      flush=True)
            return None
        import time
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            time.sleep(2)
            p = _detect_proxy()
            if p:
                PROXY = p
                for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    os.environ[k] = p
                if verbose:
                    print("  [proxy] ✓ 委托成功：桌面会话里的辅助任务把代理拉起来了 "
                          "%s（等了 %.0f 秒）" % (p, time.time() - t0), flush=True)
                return p
        if verbose:
            print("  [proxy] ✗ 辅助任务在 %ds 内没能拉起代理 —— "
                  "通常意味着**当前桌面上没人登录**，那就真的没人能启动 Clash 了"
                  % wait_sec, flush=True)
        return None

    exe = next((x for x in PROXY_EXE_CANDIDATES if x and os.path.exists(x)), None)
    if not exe:
        if verbose:
            print("  [proxy] 端口未监听，且找不到 Clash Verge 可执行文件", flush=True)
        return None

    if verbose:
        print("  [proxy] 端口未监听 —— 尝试启动 %s 并等待内核就绪（最多 %ds）"
              % (exe, wait_sec), flush=True)
    try:
        import subprocess
        subprocess.Popen([exe], close_fds=True)
    except Exception as e:
        if verbose:
            print("  [proxy] 启动失败：%r" % e, flush=True)
        return None

    import time
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        time.sleep(2)
        p = _detect_proxy()
        if p:
            PROXY = p
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ[k] = p
            if verbose:
                print("  [proxy] 已就绪 %s（等了 %.0f 秒）" % (p, time.time() - t0), flush=True)
            return p
    if verbose:
        print("  [proxy] 启动后 %ds 内仍未监听 —— 可能需要人工登录/选节点" % wait_sec, flush=True)
    return None

# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(ROOT, "data", "polytrade.sqlite")
LOG_DIR = os.path.join(ROOT, "polytrade_logs")
OUT_DIR = os.path.join(ROOT, "polytrade_out")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SPORTS_CATALOG = os.path.join(
    r"D:\26050\Documents\polymarket_sports\data", "sports_catalog.json")

# 抓取多少个联赛 tag。
# ★★★ 2026-10-10 修正：原值 200 是一个**静默的数据采集缺陷** ★★★
#   fetch_all_markets 里写的是 tags[:MAX_LEAGUE_TAGS]，也就是
#   「按 catalog 的原始顺序取前 200 个」。而 catalog 的顺序 ≈ 收录时间，
#   没有任何优先级含义（ordering 字段只有 home/away，不是排序权重）。
#
#   实测 catalog 共 473 个联赛，截前 200 个的后果：
#     抓到了    UFL、Plunket Shield、National T20 Cup、ECS Switzerland、
#               Maharani Trophy、JCL T10、Pondicherry PL、NCAA Lacrosse ...
#     漏掉了    Premier League(#466)、UEFA Europa League(#450)、
#               MLS(#423)、Serie A(#436)、Liga MX(#223)、A-League Men(#361)、
#               UFC、NHL、NBA
#
#   代价：实盘 100% 押在了证据基础之外的联赛上（Eerste Divisie 历史本档 0 行），
#   于是测出 k=-11.5pp 时，根本分不清是"策略失效"还是"从来没抓到该抓的联赛"。
#
#   现在改为 0 = 抓全部。473 个 tag 约 165 秒，仍在 2 小时周期内。
MAX_LEAGUE_TAGS = 0

# 每个 tag 翻多少页。
# ★ 2026-10-10 新增：实测 gamma 的 /markets 单次最多只返回 100 条
#   （limit=500 与 limit=100 返回完全相同的集合），而 Premier League
#   一个 tag 就有 600+ 个未结算市场 —— 只取首页等于只看到 1/6。
#   实测 offset 0~500 能翻出 600 个不同的 PL 市场，所以分页是有效的。
#   翻页是**按需**的：某一页返回不足 100 条就说明到底了，立即停止。
MAX_PAGES_PER_TAG = 10  # hard safety cap; fetcher warns when a tag hits it
# ★ 2026-10-10 实测页深的边际收益（同一套筛选，逐档单独跑）：
#     页数   抓到市场   下注候选   1天窗口内   3天   7天   耗时
#      1     14,453       77          6        0     0    165 s
#      3     36,054      230         33        0     6    281 s
#     10     89,220      667        101        4     8    511 s
#   10 页相对 3 页：窗口内候选 33 -> 101。而扫描是每 2 小时一次、
#   单次 30 分钟上限，511 秒完全放得下 —— 所以取 10 页。
#   注意 53 个联赛会打满 10 页（Bundesliga / LaLiga / CS2 / College Football ...），
#   即真实联赛的市场数远超 1000。打满时 fetcher 会在 runs.note 里告警，
#   避免又出现"静默少采"。
MARKETS_PER_TAG = 100

# ★ 抓取的墙钟预算（秒）。定时任务的 ExecutionTimeLimit 是 30 分钟，
#   而 473 tag x 10 页最坏情况是 4,730 次请求；一旦代理抽风，
#   单请求要重试 5 次 x 20s 超时，足以把任务拖到被强杀。
#   超预算时**按墙钟截断并写进 runs.note**（交给看门狗），绝不允许静默截断 ——
#   静默少采正是联赛覆盖缺陷能藏两天的原因。实测正常耗时约 510 秒。
FETCH_TIME_BUDGET_SEC = 720
SETTLE_TIME_BUDGET_SEC = int(os.environ.get("POLYTRADE_SETTLE_TIME_BUDGET_SEC", "120"))
SETTLE_HTTP_TIMEOUT_SEC = int(os.environ.get("POLYTRADE_SETTLE_HTTP_TIMEOUT_SEC", "8"))

# ---------------------------------------------------------------------------
# ★ 核心（策略）候选的数量守卫（2026-10-15）
# ---------------------------------------------------------------------------
# 背景：下注候选只从「核心抓取」出（见 scan.fetch_core）。实测发现
#   gamma 对**非法的 sports_market_types 取值不报错，只返回空列表**：
#       sports_market_types=moneyline -> n=20（正常）
#       sports_market_types=zzz      -> n=0   （不是错误！）
#   而 fetch_core 把「空列表」和「正常抓到底」返回成同一个 issue=None，
#   于是 FETCH_CORE_COMPLETE 照样为 True —— **系统会一切正常地停止下注**。
#
# 阈值只用于抓「整块塌掉」这种灾难，**不是覆盖率达标线**：
#   正常一天核心候选是几百到上千个（抽查单 tag：LaLiga 63 / Bundesliga 54 /
#   K League 1 36；Serie A 那天是 0，属正常 —— 窗口内确实没有比赛）。
#   定得低是为了不产生假警报 —— 告警一旦变成噪音就没人看了。
CORE_MIN_CANDIDATES = int(os.environ.get("POLYTRADE_CORE_MIN_CANDIDATES", "20"))

# ---------------------------------------------------------------------------
# ★ 抓取请求的重试与超时（2026-10-15，实测后定的折中）
# ---------------------------------------------------------------------------
# 事实一（实测，证明重试设置**不是**瓶颈）：
#   完整 473 个 tag 连跑 3 遍 = 1,419 次请求，只失败 1 次（一次 Read timeout）。
#   即 tries=1 / timeout=10 本身在生产规模下是能跑通的，不是它导致的问题。
#
# 事实二（实测，证明**没有重试会丢数据**）：
#   2026-10-15 12:05 那一轮遇到一次瞬时中断 —— **473/473 个 tag 全部失败**，
#   整轮只采到 1,578 个市场（前一轮 87,653 个，单轮丢掉 98% 的数据）。
#   `tries=1` 意味着一次抖动就毫无还手之力。
#
# 本仓库早就实测过这条链路会抖（见文件末尾"代理抖动说明"：
#   "同一变体三轮测试会出现 200/ERR/ERR 的混合……HTTP_RETRIES 才从 3 提到 5"）。
#   把 tries 降到 1 等于把这份韧性归零。
#
# 折中：tries=2 / timeout=15 —— 单请求最坏 30 秒（**仍然有界**，不会像
#   tries=5 x timeout=40 那样把墙钟预算吃光），但足以扛过一次抖动。
FETCH_HTTP_TRIES = int(os.environ.get("POLYTRADE_FETCH_HTTP_TRIES", "2"))
FETCH_HTTP_TIMEOUT = int(os.environ.get("POLYTRADE_FETCH_HTTP_TIMEOUT", "15"))

# ---------------------------------------------------------------------------
# ★★★ 整轮运行的墙钟预算（2026-10-14 新增）★★★
# ---------------------------------------------------------------------------
# 为什么 FETCH_TIME_BUDGET_SEC 不够：
#   实测事故 —— 2026-10-14 09:54 那次运行，
#     09:54  开始（[1/5] 结算）
#     10:09  [2/5] 扫描结束（自己按 720s 正常截断，runs.note 记了 time_budget）
#     10:09~ [4/5] 抓参照价 + 匹配赛程 …… 一直跑到
#     10:24  撞上计划任务的 ExecutionTimeLimit = PT30M，被**硬杀**。
#   后果：报告没生成、看门狗没跑、digest/report 一起停在旧版本 ——
#   这正是那条「崩溃无回执」（第 5 条 bug）的同一个形状，
#   只不过触发者从"异常"变成了"超时"。
#
# 而且 ExecutionTimeLimit **改不了**：Set-ScheduledTask 需要账户密码
# （报 "The user name or password is incorrect"），本机无法提权。
# 所以只能让运行自己去适应这个 30 分钟的天花板。
#
# 口径：
#   RUN_BUDGET_SEC    整轮允许占用的墙钟秒数。1800s 上限，留 240s 余量 -> 1560s。
#   RUN_RESERVE_SEC   为「报告 + 看门狗 + 简报」预留的秒数，任何情况都不许被吃掉。
#                     ★ 取 180 有实测依据：这三步合起来约 **8 秒**
#                     （2026-10-14 演练：整轮 129s，[5/5] 之前到 123s 才结束）。
#                     180s 是 20 倍余量，同时比 240s 多让出 60s 给 AI 那一步 ——
#                     AI 样本是用户明确要的研究产物，不该被过大的保留额挤掉。
#   （只定义一次：早前这里被重复定义过一次，正是"两处描述同一约束必然漂移"的老毛病。）
#   每步开跑前检查剩余预算，不够就**跳过并明确上报**，绝不静默省略。
#   报告步骤本身很便宜（本地读写），所以它永远能执行。
#
#   两个值都可以用环境变量覆盖 —— 便于在不改代码的情况下做一次「小预算演练」，
#   验证跳过逻辑真的会触发、而且报告照样写得出来：
#       POLYTRADE_RUN_BUDGET_SEC=120 python -m polytrade.run --dry
#   ★ 2026-10-14 实测补充：_t0 是在 run.py 的 main() 里取的，而计划任务的
#     PT30M 是从**任务启动**开始算的 —— 两者之间隔着 cmd.exe 启动、
#     python 解释器启动、以及模块导入。实测看到过约 4 分钟的偏移
#     （计划任务 10:36:45 触发，run.py 内部算出的剩余时间反推 main() 约在 10:40 之后）。
#     所以预算**不能贴着 1800 设**：必须留出「启动偏移 + 收尾」的余量。
#     1440s(24min) + 6min 余量 = 稳稳落在 PT30M 以内。
RUN_BUDGET_SEC = int(os.environ.get("POLYTRADE_RUN_BUDGET_SEC", "1560"))
# ★ 实测核对（2026-10-14 11:06 那次完整运行）：
#   计划任务 10:36:37 触发 → run.py 打印 run @ 10:36:39，**启动偏移只有约 2 秒**，
#   不是我先以为的 4 分钟（那是我读到了缓冲过的日志，结论下早了，已更正）。
#   所以 1560 是合适的：实测整轮构成 = 结算 19s + 扫描 646s
#   + 参照价/匹配赛跑 1100s + 报告 8s = 1765s（AI 被跳过），离 1800 只剩 36 秒。
#   ⇒ 真正的问题不是预算数值，而是**某一步内部不吃预算**（见 REFODDS_MAX_SEC）。
RUN_RESERVE_SEC = int(os.environ.get("POLYTRADE_RUN_RESERVE_SEC", "180"))

# ---------------------------------------------------------------------------
# ★★ 单步预算上限（2026-10-14）★★
# ---------------------------------------------------------------------------
# 只给「整轮预算」是不够的：如果某一步内部是个很长的循环，它根本读不到预算，
# 于是整轮预算形同虚设。实测就是如此 ——
#   「抓参照价 + 匹配赛程」这一步实际跑了 1100 秒（预算是 655 秒），
#   其中约 1000 秒花在 match.run_match 的 O(市场数 x 赛程数) 双重循环里，
#   而且它**一句都不打印**，外部看起来就是卡死（cron.log 静默 17 分钟）。
# 所以每一步都要有自己的、比整轮预算更小的上限，并且内部要能停下来。
#
# 分配（整轮 1560s）：
#   结算         ~20s   （首步，不设上限，很快）
#   扫描         646s   （抓取受 FETCH_TIME_BUDGET_SEC=720 约束）
#   参照价+匹配  420s   （REFODDS_MAX_SEC，增强项，可以少做）
#   AI 预测      120s   （AI_MAX_CALLS_PER_RUN=25）
#   报告+看门狗   8s    （实测，保留 180s 是 20 倍余量）
# 这样即使扫描变慢，也总是给「报告 + 看门狗」留下位置 —— 留痕优先于做多。
REFODDS_MAX_SEC = int(os.environ.get("POLYTRADE_REFODDS_MAX_SEC", "420"))

# ---------------------------------------------------------------------------
# ★ 观测范围 vs 下注范围（两者必须分开！）
# ---------------------------------------------------------------------------
# 设计教训：初版把"价格档 0.10-0.15"同时用在【观测】和【下注】上，
# 结果 229 个合格市场只记录到 8 个 —— 那个档只占价格轴的 5%，
# 而市场大量聚集在 0.30-0.50（勘察实测 830/2482）。
#
# 正确做法：**观测要宽收，下注才用窄规则。**
#   · 观测贵在"全"：记录所有价格档，日后任何假设都能在同一份数据上检验
#   · 下注贵在"准"：只有明确规则的子集才真下注
# 存储成本可忽略，而"当时没记录"是无法补救的。

# 观测范围（宽）
OBS_PRICE_LO = 0.02
OBS_PRICE_HI = 0.98
OBS_MAX_SPREAD = 0.20          # 只排除真正空盘口（如 0.01/0.99）
OBS_TYPES = None               # None = 记录所有类型（类型本身也写进表，便于事后筛）

# 下注范围（窄；决定"什么算一个机会"）
# ⚠️ 第二次修正：初版把下注档设成 0.10-0.15（我历史研究的那一档），
#    但实测 228 个合格市场里只有 7 个落在这 5 个百分点的区间 ——
#    按这个节奏几周也攒不到十几注，永远到不了 t>2 所需的样本量。
#    而且我历史研究在两个样本上给出了【相反符号】，本来就没锁定方向。
#    所以放宽到覆盖"冷门高估"所在的整段（0.05-0.35），先攒数据。
#    ⇒ 真正确认方向靠 observations 表的校准分析，不靠这个下注档。
BET_PRICE_LO = 0.05
BET_PRICE_HI = 0.35
BET_MAX_SPREAD = 0.06
BET_TYPES = {
    "moneyline", "child_moneyline", "first_half_moneyline",
}
# ★★★ 2026-10-15：直接打真实 gamma API 测出来的三条硬约束，别"简化"掉 ★★★
#
# 1) 只有 `moneyline` 是真值。
#    sports_market_types=moneyline            -> n=20  ✅
#    sports_market_types=child_moneyline      -> n=0
#    sports_market_types=first_half_moneyline -> n=0
#    后两个是**字段可能出现的取值、但不是合法的过滤取值**（API 不一致）。
#    保留在集合里无害（抓取时返回空），但要知道它们实际上不起作用；
#    历史上有 6 笔注的 market_type 是 child_moneyline（$12），
#    这些市场以后不会再被抓到、也就不会再下注。
#
# 2) `end_date_min` / `end_date_max` **单独使用会 HTTP 500**，实测：
#        仅 end_date_min/max                -> 500
#        end_date_min/max + sports_market_types -> 63（正常，且 63/63 在窗口内）
#    所以 scan.fetch_core 总是把类型过滤和日期窗口一起传，这不是冗余而是必需。
#    后人若"顺手删掉看起来多余的 sports_market_types"，核心抓取会直接 500。
#
# 3) 未知的参数名会被**静默忽略**（zzzparam=1 不报错、结果与不传一样）。
#    所以参数名打错不会炸，只会悄悄不生效 —— 任何新的过滤参数都必须
#    用真实 API 验证它真的改变了返回集，而不是"加上了就以为生效了"。

# ★★★ 2026-10-12 修：下注价格预筛必须**从 BET_RULES 推导**，不能各写各的 ★★★
#   原来预筛写死 [0.05, 0.35)，而 BET_RULES 第二条要求 mid ∈ [0.90, 0.99) ——
#   预筛先把 mid >= 0.35 的全剔除了，于是那条规则**永远不可达**。
#   实测 183 笔全是 YES，一条 NO 都没下过：它是死代码。
#
#   教训：**两个描述同一约束的地方，一定会漂移。**
#   所以这里不在文件里就地推导（BET_RULES 在下面才定义），
#   而是在文件末尾统一算 —— 见文件最后的 BET_PRICE_MIN/MAX 段。
BET_PRICE_LO_LEGACY = 0.05   # 保留仅作历史参考，代码不再使用
BET_PRICE_HI_LEGACY = 0.35

# ---------------------------------------------------------------------------
# ★ 高档位（买 NO）的偏差估计 —— 分辨率只能到「档」，到不了「联赛」
# ---------------------------------------------------------------------------
# 依据（historical_rows.csv，[0.90,0.99)、lead 1/3）：
#     全部           n=392 市场  均价0.9384 实际0.8801  bias=-5.83pp  t=-3.55
#     剔除 World Cup n=258 市场  均价0.9403 实际0.8643  bias=-7.60pp  t=-3.56
#     只看 World Cup n=134 市场  bias=-2.43pp  t=-0.98  （不显著）
#   ⇒ 效应不是世界杯撑起来的，**剔除后反而更强**。
#
# 但联赛层放不下：52 个联赛里只有 1 个（World Cup）n>=40，n>=20 的只有 4 个。
# 所以高档位的门槛只能设在**档**这一层 —— 这和低档位（联赛层，12 个联赛）不同。
# 「在数据支持的分辨率上设门槛」这条原则不变，只是两档的支持分辨率不一样。
#
# 净偏差推导（NO 口径，用实付 ask 而非 mid）：
#     中位价差 0.04 -> 买 NO 实际付 p_N = 1-mid + 0.02 = 0.0816
#     q_N = 1 - 0.8801 = 0.1199
#     gross = 0.1199 - 0.0816 = 0.0383
#     fee   = 0.05 x 0.0816 x 0.9184 = 0.00375
#     net   = 0.0346  ->  每 $1 投入 EV = q_N/p_N - 1 - fee率 = +42%
#
# ⚠ 与低档位同样的警告：这是**历史回测**数。低档位历史 +7.8pp、实盘 -3.5pp，
#   所以高档位也必须先用小仓位跑出实盘样本，不能直接按 +42% 预期。
HIGH_BAND_LO = 0.90
HIGH_BAND_HI = 0.99
HIGH_BAND_NET_EDGE_PP = 0.0346

# ---------------------------------------------------------------------------
# ★ AI 赛前概率预测（2026-10-12 用户要求："每次比赛前调用 AI 分析胜率"）
# ---------------------------------------------------------------------------
# 定位：**研究层，不是执行层。**
#   执行（下注）必须是确定性代码 —— 否则实验的规则就不固定了。
#   AI 只做一件事：赛前独立给出一个概率，落库，赛后用固定公式评估。
#
# 已知的三个风险，全部用数据回答（见 aiforecast.py 顶部注释）：
#   ① AI 可能只是在复述市场价  -> 用 corr(AI, 市场) 判
#   ② LLM 校准差，尤其极端概率 -> 用 k_AI 判
#   ③ AI 的偏离可能纯噪声      -> 用 corr(AI偏离, 结果偏离) 判
#
# 额度：宁可少问，不可失控。默认每天最多 150 次。
AI_FORECAST_ENABLED = True
AI_MAX_CALLS_PER_RUN = 25
AI_MAX_CALLS_PER_DAY = 150
AI_MIN_LEAD_DAYS = 0.25       # 距开赛不足 6 小时不问：答案没用且可能被场边消息污染
AI_SLEEP_SEC = 0.4            # 每次调用之间的间隔，避免打爆限流
AI_MAX_TOKENS = 200
AI_MODEL = "deepseek-chat"
AI_ENDPOINT = "https://api.deepseek.com/chat/completions"
# 凭据：先找环境变量 DEEPSEEK_API_KEY，再读下面这个文件里的 refs.DEEPSEEK_API_KEY
AI_CREDENTIALS_PATH = os.path.join(
    os.environ.get("USERPROFILE", ""), ".dsh", ".credentials.yaml")

# 距结算天数（观测与下注共用；太近没时间建仓，太远信息不足）
MIN_DAYS = 0.25
MAX_DAYS = 14.0

# 流动性下限
MIN_VOL24 = 0.0

# ---------------------------------------------------------------------------
# "冷门联赛"的定义（用户假设的核心变量）
# ---------------------------------------------------------------------------
# 用【本系统观测到的联赛累计成交额】分位定义。
# 每次 scan 时重新计算，所以这个定义是动态的、自我更新的。
COLD_QUANTILE = 0.50       # 成交额低于中位的联赛算冷门

# ---------------------------------------------------------------------------
# 纸面账本
# ---------------------------------------------------------------------------
# ★ 2026-10-10 协议变更（用户批准）：
#   1) 每个市场只下一笔（原先同一市场会按 1/3/7 天各下一笔，占 3 个额度）。
#   2) 额度上限 100 -> 300。
#
#   为什么改：我们真正缺的是**独立市场数**，它直接决定统计功效。
#   实测第一轮 100 笔里只有 75 个独立市场（25 笔是重复），
#   而且 100 个额度一天就填满 —— 结算修好之前额度再也不释放，实验直接冻结。
#   同一市场下 3 笔不会让标准误降低，只会让 [t 值](虚高)。
#
#   影响与一致性：分析端（report.settled_edge_analysis）本来就按 market_id 去重，
#   所以变更前后 1/3/7 天的样本**可以合并比较**，不需要改表结构。
#   协议分界线 = 2026-10-10T07:00Z（此前的 100 笔按旧协议）。
#
#   注意：这不是资金计划，是**数据采集配置** —— 纸面资金不受真实约束，
#   本金数字只是为了把 "% of 本金" 读得通顺。
PAPER_BANKROLL = 3000.0    # 纸面本金
# ★ 2026-10-10 用户要求："一天涨跌 8% 的投资品我不会投"。
#   仓位大小是**反推**出来的，不是拍脑袋：
#     单注收益率标准差 per = 2.24（在 0.16~0.29 的买入价上实测）
#     日盈亏标准差 = per x stake x sqrt(当日结算注数) = per x sqrt(stake x 日投放)
#   注意：波动由 **stake** 主导，不是由投放比例主导（因为注数 = 投放/stake）。
#   实测各组合的日标准差（本金 $3000）：
#     日上限 20% x $10 -> 5.78%   ← 原来的配置，一次 2.5 sigma 就是你看到的 8%
#     日上限 10% x $2  -> 1.83%
#     日上限  5% x $2  -> 1.29%
#     日上限  5% x $1  -> 0.91%
#
# ★★★ 2026-10-16：用户要求把单注降到 $1 以下（"2 美元太多了"）。实际取 $1.00。★★★
#
#   为什么波动会**下降**（这是反直觉但正确的一条）：
#     日 P&L 标准差 = per x stake x sqrt(当日结算注数)
#     而"当日结算注数"受**敞口**约束：注数 ≈ 敞口上限 / stake
#     => 日标准差 ∝ stake x sqrt(1/stake) = sqrt(stake)
#   所以**把单注减半，日波动降低到 1/√2 ≈ 71%**，同时手里能装的笔数翻倍。
#   用小注不是"少赚一点"，在同等敞口下它同时降低了波动、提高了样本笔数上限。
#
#   按实测数据重算（本金 $3000、持仓实测均值 1.95 天、每笔收益率标准差 2.07）：
#     单注 $2  敞口 $300 -> 150 笔在手 -> ~77 笔/天结算 -> 日标准差 $36 = 1.21%（2σ 2.42%）
#     单注 $1  敞口 $300 -> 300 笔在手 -> ~154 笔/天    -> 日标准差 $26 = 0.86%（2σ 1.71%）
#
#   ⚠️ 代价 / 现实性提醒（重要）
#     入场价实测 0.031~0.32、平均 0.234。$1 对应的股数：
#        均价 0.234 -> 4.3 股    0.30 -> 3.3 股    0.15 -> 6.7 股
#     而真实盘口常见的最小下单量是 **5 股**。也就是说 $1 在价格 > 0.20 的市场上
#     **可能小到挂不出去**（本系统是纸面模拟，未建模最小下单量）。
#     若要保证在本策略的 [0.15, 0.30) 档内都 >= 5 股，单注下限是 **$1.50**。
#     ⇒ 现在取 $1.00 是"按用户要求 + 保留可读性/可验证性"的折中；
#       将来若真金白银去做，这里要先提高到 $1.50 或改用"按股数下单"。
STAKE = 1.0                # 单注金额（本金 $3000 -> 每注 0.033%）
MAX_OPEN_BETS = 300        # 持仓笔数上限（金额由下面的敞口闸控制）
# ★★★ 2026-10-16：25 -> 40（用户要求执行"第二步"）★★★
#
# 为什么是它，而不是敞口（实测四道闸的占用）：
#   每次扫描 25 笔：**12 次运行里 3 次顶到 25**，其余 0~3 笔  <- 真正在削峰的
#   每日新增 $150 ：实际 38~55 笔，占 51%~73%               <- 没卡住
#   同时在场 $300 ：实际 90 笔 / $220，占 60%/73%            <- 没卡住
#   => 直接提高敞口上限大概率看不到效果，因为上游这道闸先切掉了。
#
# 为什么是 40 这个数：
#   单注 $1 时的系统吞吐天花板 = min(每日 $150 -> 150 笔, 敞口 $300 / 1.95 天 -> 154 笔/天)
#                              ≈ 150 笔/天
#   ★ 2026-10-16 更正（我第一版这个外推**错了近一倍**，实测打脸）：
#     我原来写"约 3~4 次运行/天会出注 -> 40 x 3~4 = 120~160 笔/天"。
#     实测全部 61 轮扫描（跨 7.01 天）：n_new_bets 分布是
#         [25,25,25,25,0,0,0,...]  —— **双峰：绝大多数轮是 0**，
#     只有 11/61 轮顶到 25，平均 **1.6 轮/天**（不是 3~4 轮）。
#     修正后的预期：25->40 每天多约 23.5 笔，日均新建 46 -> **约 70 笔/天**，
#     攒满 300 笔从 6.5 天降到约 **4.3 天**。
#     对应工作点：在场约 137 笔 x $1 = $137，日标准差 $17.3 = 0.58%（2σ 1.16%）。
#   ⇒ 教训：我把"运行次数"当成了"出注次数"。分布是双峰的，
#     用均值 × 次数去外推会严重高估 —— 这类错误正是 check_throughput.py 要抓的。
#
#   ⚠️ 下一次核对会告诉我们到底是谁在卡：若运行开始频繁顶到 40，说明候选还富余、
#      可以继续提；若停在 25~40 之间，说明**候选数量**才是真约束，再提无益。
#
# 口径影响（依 EXPERIMENT_stopping_rule.md 的原则：**改「一次吃多少」不重置窗口**）：
#   本参数是**吞吐上限**，不是"什么算一笔合规下注"。合规集合（过偏差门槛的那些市场）
#   完全没变，变的是「同一批合规注里这次取多少笔」。
#   实测旁证：多出来的注来自更远期的市场（核心抓取按 endDate 升序），
#   而 corr(days_to_end, 每笔收益率) = -0.074（几乎无关），
#   分桶均值差 -4.6pp 仅 0.16σ（n=168 vs 73）—— 测不出差异。
#   ⚠️ 但 >7 天那一档只有 12 笔且全是亏损（ROI -104%），样本太小不能当结论，
#      所以**必须盯着**：见下面 _MONITOR 注释。
#
# 观察指标（改完跑 3~5 轮后核对，不许只看"感觉变快了"）：
#   1. runs.n_new_bets 是否从"3/12 顶到 25"变成"经常接近 40"；
#   2. 每日新建笔数是否真的从 38~55 升上去（没升 = 我判断错了，回退）；
#   3. 新注的 days_to_end 分布与旧注是否明显不同；
#   4. 新注的每笔收益率是否显著低于旧注（若是，说明远期档真的更差，需回退并重估）。
MAX_NEW_BETS_PER_RUN = 40

# ---------------------------------------------------------------------------
# ★ 仓位与风险上限（用户拍板："稳健不求暴富"）
# ---------------------------------------------------------------------------
# 起因：第一轮 100 笔在 42 分钟内全部开完，占用本金 100%。
# 一次坏周末亏掉 $253.62 —— 按当时本金算是 -25%，按实际投入算是 -45%。
# 蒙特卡洛（33 个独立市场、市场公允的零假设）显示这种幅度约 8% 概率会发生。
# 也就是说：**即使 edge 是真的，这种下注方式也会被一个坏周末打穿。**
#
# 三道闸，目的不是提高收益，是让系统活得久：
#   MAX_DAILY_NEW_PCT  同一天新增投放 / 本金 的上限   <- 用户明确要求的那条
#   MAX_EXPOSURE_PCT   同时在场金额 / 本金 的上限
#   MAX_CLUSTER_PCT    单个「联赛 x 比赛日」簇金额 / 本金 的上限
#
# 为什么还要按簇限：实测 56 笔里 22 笔来自荷乙同一轮 ——
# 那不是 100 个独立头寸，而是几个比赛日。按簇限才真正在限相关性。
#
# 实算（本金 $3000 / 单注 $2）：日投放上限 $150、敞口上限 $300、单簇上限 $60。
# ★ 2026-10-16 单注改为 $1 后，**金额闸的含义变了**（它们是本金的百分比，不随单注变）：
#     日投放 $150 -> 从"最多 75 笔"变成"最多 150 笔"
#     敞口   $300 -> 从"最多 150 笔在场"变成"最多 300 笔在场"（正好等于 MAX_OPEN_BETS）
#     单簇    $60 -> 从"最多 30 笔"变成"最多 60 笔"
#   注意：这**不会自动让下注变多** —— 实际笔数由"候选数量"和 MAX_NEW_BETS_PER_RUN 决定，
#   金额闸只是上限。实测每天新建 38~55 笔（$2 时占日上限 51%~73%），
#   所以在候选数量不变的前提下，改成 $1 后每天的**笔数基本不变**、占用金额减半。
#   要真的加快样本积累，要动的是 MAX_NEW_BETS_PER_RUN（见下面"待办"注释）。
MAX_DAILY_NEW_PCT = 0.05
MAX_EXPOSURE_PCT = 0.10
MAX_CLUSTER_PCT = 0.02

# ---------------------------------------------------------------------------
# ★ 双线并行（2026-10-10 用户批准）
# ---------------------------------------------------------------------------
# 起因：实盘第一轮 k=-11.5pp，但事后查明这些注 100% 押在与历史证据不重叠的
# 联赛上（Eerste Divisie 在历史本档里 0 行）。于是根本分不清
# "edge 是假的" 还是 "只是联赛选错了"。两条线分别回答两个问题：
#
#   valid   : 只在与历史证据重叠的联赛建仓
#             -> 回答「历史那个 longshot 偏差，在今天、在这个平台上还成立吗」
#   explore : 其余联赛
#             -> 回答「冷门联赛自己有没有独立的机会」
#
# 实现方式 = **按联赛分区**（不是同一市场下两注）：
#   一个市场只会落到其中一条线，避免重复占用额度、也避免样本相关。
#   两条线规则完全相同，唯一差别是联赛集合 —— 所以可以直接对比。
#
# 判定依据是 modeling/league_support.csv（由 build_league_support.py 生成）：
#   某联赛需有至少 30 个独立市场，且扣除 taker 费后的 bias 通过
#   按市场聚类区间 + 多重比较校正，才进白名单。
TRACKS = {
    "valid":   {"max_open": 150, "share": 0.90},
    "explore": {"max_open": 150, "share": 0.10},
}
# share = 该线能占用多少比例的日投放/敞口/单簇额度。
#
# ★ 2026-10-10 说明为什么 explore 只给 10%：
#   用户现在的规则是"限制在偏差较大的时候才交易"，而 explore 的定义就是
#   **估不出偏差**的联赛 —— 严格说它不该占用投资额度。
#   但它仍有两个用处：(a) 实测这些市场的真实成交价与滑点，
#   (b) 作为对照组。所以留一条象征性的额度。
#
#   更重要的是：**探索这些联赛所需的证据并不来自下注，而来自 observations 表。**
#   历史那套偏差结论就是从「某时点的价格 + 最终结果」算出来的，
#   而 observations 每个快照都记录了 bid/ask/mid 和最终结算，
#   无论我们有没有下注。所以限制 explore 的下注额度
#   **不会削弱证据积累**，只会降低无意义的敞口。
WHITELIST_PATH = os.path.join(ROOT, "modeling", "league_support.csv")

# ---------------------------------------------------------------------------
# ★ 偏差门槛（用户要求："限制在偏差较大的时候才交易"）
# ---------------------------------------------------------------------------
# 原来的规则是「价格落在 [0.15,0.30) 就买 YES」—— 那是**价格区间**，不是**偏差**。
# 同一区间里偏差可以从 -14.5pp 到 +31.4pp，所以两者根本不是一回事。
#
# 偏差怎么估（这是关键，决定了门槛能设在哪一层）：
#   试过「联赛 x 0.03 宽的价格细档」= 356 个单元格，其中 n>=40 的只有 28 个，
#   经按市场聚类 bootstrap + BH 多重比较校正后，**显著的一个都没有**。
#   见 build_edge_cells.py 的诊断输出。
#   退到「只按联赛」就成立：26 个联赛 n>=40，其中最强的一批
#       中超 +24.1pp(t=4.03) / 英冠 +22.5pp(3.83) / MLS +21.7pp(3.90)
#       巴甲 +19.7pp(4.33) / 土超 +13.8pp(2.43) / 德甲 +8.7pp(3.22) / 英超 +4.5pp(2.44)
#
# 所以门槛只能设在**联赛层**。这不是偷懒，是数据分辨率的事实上限 ——
# 在不支持的分辨率上设门槛，等于编一个数字。
#
# 口径：门槛比的是 **净偏差** = gross_bias - 手续费 - 实测价差成本
#   fee          = 0.05 x p x (1-p)      官方 taker 公式（体育）
#   spread_cost  = 0.0087                本项目实测「成交价 - 当时中间价」均值
MIN_EDGE_PP = 0.05         # 旧口径：联赛层**绝对**净偏差门槛。保留仅作对照，代码不再使用。
SPREAD_COST = 0.0087       # 实测半价差（元/股），必须从偏差里扣掉

# ★★★ 2026-10-12 改：门槛从「绝对 pp」改成「相对投入的收益率」 ★★★
#   为什么必须改：
#     [0.15,0.30) 档的合约要付 ~0.20 一股；[0.90,0.99) 档买 NO 只付 ~0.08 一股。
#     同一个"净偏差 3.46pp"，在前者是 17% 的单笔收益，在后者是 **42%**。
#     用绝对 pp 当门槛，等于对小额合约收更严的标准 —— 这是指标口径错误，
#     不是风险偏好问题。
#
#   换算：要求 净偏差 / 实付价 >= 30%
#     低档位 p≈0.20 -> 需要净偏差 >= 6.0pp（与原来的 5pp 几乎一致）
#     高档位 p≈0.08 -> 需要净偏差 >= 2.4pp（实测 3.46pp，通过）
#   实测这样一改：低档位的过关联赛名单**完全不变**（仍是那 12 个，英超 4.50pp 仍被挡），
#   而高档位第一次进得来。正是想要的结果。
MIN_EDGE_PCT_OF_STAKE = 0.30

# ---------------------------------------------------------------------------
# ★ 下注规则（v2：来自建模的干净对照结论，不是我的猜测）
# ---------------------------------------------------------------------------
# 依据：剔除「已定型」样本后的价格档校准（本项目独立复核）
#   价格档      市场价    实际胜率   偏差      t      结论
#   .15-.20    0.1735    0.2487   +7.5pp   3.47   买 YES  ★强
#   .20-.30    0.2522    0.3418   +9.0pp   6.40   买 YES  ★强
#   .30-.50    0.4146    0.4385   +2.4pp   2.77   边缘，不做
#   .50-.70    0.5667    0.5537   -1.3pp  -1.32   无
#   .70-.90    0.7711    0.7637   -0.7pp  -0.50   不显著，不做
#   .90-1.00   0.9544    0.8333  -12.1pp  -2.98   买 NO（样本较小）
#
# ⚠️ v1 的错误：当时把 DEFAULT_SIDE 设成 "NO"，方向反了。
#    规则必须按价格档区分方向，不能用单一方向。
#
# ⚠️ 预期收益请按【保守值】规划：
#    样本外的 4.31¢/股出现在 longshot 偏差 11.7pp 的时期，
#    而全期均值是 6.84pp。按全期折算净 edge 约 1.9¢/股。
#    本模拟的目的就是验证这个数字，而不是复述历史高位。
BET_RULES = [
    # (价格下限, 价格上限, 方向, 备注)
    (0.15, 0.30, "YES", "干净对照 +7.5~+9.0pp，t=3.47~6.40 —— 证据最强"),
    (0.90, 0.99, "NO", "档层净偏差 +3.46pp（剔除世界杯 -7.60pp, t=-3.56, n=258）"),
]

# ★ 下注价格预筛：**从 BET_RULES 推导**（放在这里，因为 BET_RULES 刚定义完）。
#   必须放在 BET_RULES 之后才行 —— 否则 NameError（这条我犯过一次）。
BET_PRICE_MIN = min(r[0] for r in BET_RULES)
BET_PRICE_MAX = max(r[1] for r in BET_RULES)

# 入场时点：记录全部三个，事后可分别评估（避免"6 选 1"）
ENTRY_LEAD_DAYS = (1, 3, 7)

# 兼容字段（不再用于决定方向）
DEFAULT_SIDE = None

# ---------------------------------------------------------------------------
# 成本（官方公式）
# ---------------------------------------------------------------------------
# fee = C x feeRate x p x (1-p)      C=份额数, p=成交价
# 体育类 taker feeRate = 0.05；Maker = 0
SPORTS_FEE_RATE = 0.05
# 买 NO 时成交价 = 1 - YES_bid（付 NO 的卖一价）
# 买 YES 时成交价 = YES_ask
# 这就是跨越价差的成本，与官方费率分开计
USE_OFFICIAL_FEE = True

# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 40
HTTP_RETRIES = 5

# ★ 代理抖动说明（实测）：
#   本机代理连通性【不稳定】—— 同一变体三轮测试会出现 200/ERR/ERR 的混合。
#   这不是配置问题，是代理链路本身在抖。
#   对 7x24 的后果：单次抓取里若连续几次都撞上抖动窗口，该轮就会少抓数据。
#   对策：HTTP_RETRIES 从 3 提到 5，并在 _get 里对「连接类异常」退避重试
#   （4xx/5xx 属于服务端答复，不重试；连接重置/超时属于链路问题，必须重试）。
