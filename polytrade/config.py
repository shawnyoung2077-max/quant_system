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
        print("  [proxy] ⚠ 发现 %d 个跑在非桌面会话（会话 %s；桌面在会话 %d）的 "
              "Clash Verge GUI —— 它们会抢走单例锁、导致点图标无窗口，正在清理"
              % (len(targets), ",".join(str(s) for _, s in targets), desk), flush=True)
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
    #   把用户自己的 GUI 一起弄坏 —— 这正是今天那次事故。
    if not interactive_session():
        if verbose:
            print("  [proxy] ⚠ 端口未监听，但当前是**非交互式会话**（无桌面）——"
                  "拒绝拉起 Clash Verge。", flush=True)
            print("        原因：在无桌面会话里启动 GUI 会得到一个看不见的窗口，"
                  "并抢走单例锁，导致用户点图标也打不开。", flush=True)
            print("        请在桌面会话里手动启动 Clash Verge，或让它开机自启。", flush=True)
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
    "moneyline", "", "child_moneyline", "first_half_moneyline",
}

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
#     日上限  5% x $2  -> 1.29%   ← 现在选这个
#     日上限  5% x $1  -> 0.91%
STAKE = 2.0                # 单注金额（本金 $3000 -> 每注 0.067%）
MAX_OPEN_BETS = 300        # 持仓笔数上限（金额由下面的敞口闸控制）
MAX_NEW_BETS_PER_RUN = 25  # 每次扫描最多新增注数

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
