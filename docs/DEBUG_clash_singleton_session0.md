# 事故：点 Clash Verge 图标什么窗口都不弹

日期 2026-10-13 起，2026-10-14 定位并修复。
用户原话：「我的 clash 图标点击了但是没有弹窗」。

---

## 1. 现象

- 双击桌面 / 开始菜单的 Clash Verge 图标 → **没有任何窗口**。
- 托盘图标也不出现。
- 但 `http://127.0.0.1:7897` **是通的**（HTTP 200），代理本身完全正常。
- `C:\Users\26050\AppData\Roaming\io.github.clash-verge-rev.clash-verge-rev\logs\latest.log` 里反复打印：

```
[Window] 检测到从单例模式恢复应用窗口
[Window] 窗口激活成功
```

也就是说程序**认为**自己把窗口显示出来了 —— 但用户看不到。

---

## 2. 根因链

实测取证：

| 事实 | 取值 |
| --- | --- |
| `clash-verge.exe` PID | 23188（后变为 28540） |
| 会话号 | **0**（`tasklist /V` 显示 `Session Name: Services`、`User Name: N/A`） |
| 顶层窗口数（`EnumWindows` 按 PID 过滤） | **0** |
| 父进程 | `python.exe`(14168, 会话 0) ← `cmd.exe`(12592, 会话 0) ← 计划任务 `polytrade_scan` |

链条：

```
计划任务 polytrade_scan（LogonType=Password ⇒ 跑在会话 0）
  └─ polytrade_run.bat
      └─ python -m polytrade.run
          └─ config.ensure_proxy()          ← 发现 7897 没监听
              └─ subprocess.Popen([clash-verge.exe])
                  └─ GUI 落在【会话 0】
```

**会话 0 是服务会话，没有交互式桌面。** 在那里：

1. 窗口和托盘图标都创建不出来（日志 `[[Tray]] tray icon error: 未指定的错误 (os error -2147467259)`）；
2. 但进程**正常拿到了单例锁**（`singleton-instance.json` = `{"port":53263,"token":"694a35cf…"}`，
   `singleton-instance.lock` 被它独占锁定，别的进程读都读不了）；
3. 于是用户之后每一次点图标，新实例启动 → 检到单例 → 把「显示窗口」转发给会话 0 那个 →
   **自己立刻退出**，日志打印「窗口激活成功」。

⇒ **用户被自己的自动化脚本锁在门外。** 这比原来的问题（代理没起来）难查得多，
因为「图标点了没反应」看起来像 Clash 本身坏了。

顺带验证了一件重要的事：**杀掉 GUI 不影响代理。**
`verge-mihomo.exe` 是 `clash_verge_service`（LocalSystem，StartMode=Auto）的子进程，
不是 GUI 的子进程。GUI 死了 7897 照样在听。

---

## 3. 第一次修复 —— 以及它为什么失效了

`polytrade/config.py` 加了 `interactive_session()`：非桌面会话不许拉起 GUI。

**结果：2026-10-14 09:24，同样的事故又发生了一次。**
`clash-verge.exe` 又以会话 0 的身份被拉起来了。

原因在我自己的判据里：

```python
# 错的第一版
r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/FO", "CSV"])
return "explorer.exe" in (r.stdout or "")
```

`tasklist /FI "IMAGENAME eq explorer.exe"` 会列出**所有会话**的 explorer。
本机实测：

```
explorer.exe = [(13232, 1), (2324, 2)]     ← 会话 1 和会话 2，各一个
```

会话 0 里的 python 于是看到 `explorer.exe` 存在，**判定自己「可交互」**，放心地又拉了一个 GUI。

> 教训：判断「我能不能显示窗口」只能看**我自己所在的会话号**，
> 不能看「这台机器上有没有桌面」。这两件事在单用户单会话的机器上碰巧等价，
> 一旦出现第二个会话就立刻破裂 —— 而破裂的方式是**静默的**（不报错，只是继续坏）。

---

## 4. 最终修复（两件事，缺一不可）

### (a) 预防：`interactive_session()` 只看会话号

```python
me = _session_of_pid(os.getpid())      # ProcessIdToSessionId
if me != 0:  return True               # 非 0 会话 = 有桌面
return any(sid == 0 for _pid, sid in _tasklist_sessions("explorer.exe"))
```

会话 0 只在**会话号同为 0 的 explorer** 存在时才算可交互（现实中几乎不会发生）。

### (b) 自愈：`kill_invisible_gui()` 清掉历史遗留的隐形 GUI

每次 `ensure_proxy()` 开头都会执行，即使代理当前是好的 —— 因为那个隐形进程会一直霸占单例锁。

判据**故意收得很窄，只看会话 0**：

```python
targets = [(pid, sid) for pid, sid in _tasklist_sessions("clash-verge.exe") if sid == 0]
```

为什么不写「会话号 != 桌面会话就杀」：本机有会话 1 和会话 2 两个 explorer，
宽判据会**误杀用户在另一个会话里正常使用的 GUI**。
而「会话 0 里出现 GUI 进程」在 Vista 之后是 100% 病态的，杀它不可能误伤。
定位不到桌面会话时（完全无头）**什么都不做**。

只杀 `clash-verge.exe`，绝不碰 `verge-mihomo.exe` / `clash_verge_service`，所以代理不受影响。

---

## 5. 遗留的架构问题（已按第 8 节处理）

后台任务现在**没有代理自愈能力了**：会话 0 里发现端口没监听时，只会打印提示并继续，
后续 scan 的预检会抛出明确错误（这是刻意的，见 `DEBUG_proxy_outage_36h.md`）。

这意味着：**如果 Clash 没开机自启，机器重启后自动化会一直失败到有人登录并打开 Clash 为止。**

正确的解法不是「让后台脚本去拉 GUI」（那正是这次事故的来源），
而是让 GUI 在**用户登录时、在桌面会话里**自己起来 —— 见第 8 节。

---

## 6. 回归测试

`tests/test_regressions.py::TestProxySessionGuard`（6 条）：

| 测试 | 锁住的行为 |
| --- | --- |
| `test_session0_is_not_interactive_even_when_explorer_exists_elsewhere` | 本机存在会话 1/2 的 explorer 时，会话 0 仍必须判为非交互式 |
| `test_nonzero_session_is_interactive` | 非 0 会话判为交互式 |
| `test_kill_targets_only_session0_gui` | 清理目标只能是会话 0，不得碰会话 1/2 |
| `test_no_desktop_session_means_no_kill` | 定位不到桌面会话时不许杀 |
| `test_tasklist_sessions_parses_csv` | `tasklist /FO CSV` 解析（含 `INFO: No tasks…` 与逗号数字） |
| `test_ensure_proxy_refuses_gui_in_session0` | 端到端：会话 0 里 `ensure_proxy()` 不得 `Popen` |

合计 99 条测试全绿。

---

## 7. 现场处理与结果（2026-10-14 09:54）

从桌面会话（会话 1/2）用 `taskkill` 杀那个隐形 GUI → **Access is denied**
（跨会话操作不了别人会话里的进程令牌）。
但在**会话 0 里**跑一次修复后的 `ensure_proxy()` 就成功了 —— 也就是说
**它自己造成的破坏，它自己能清**：

```
[proxy] ⚠ 发现 1 个跑在会话 0（无桌面）里的 Clash Verge GUI ... 正在清理
[proxy]   ✓ 已结束 pid=28540（会话 0）
[proxy] 已清掉 1 个隐形 GUI，单例锁已释放 —— 现在点 Clash 图标应该能正常弹窗。
```

清理后逐项复核：

| 项 | 结果 |
| --- | --- |
| `clash-verge.exe` | 已无任何进程 |
| `verge-mihomo.exe` | pid 20596 仍在跑（服务子进程，不受影响） |
| 7897 | 仍在监听（owner = 20596） |
| 过代理 HTTPS 请求 | HTTP 200 |
| `singleton-instance.json` / `.lock` | 两个文件都能独占打开 → **锁已释放** |

⇒ 用户此时点图标即可正常弹窗，**不需要重启**。

---

## 8. 开机自启（用户已批准，2026-10-14）

第 5 节的遗留问题必须堵上，否则机器重启后自动化会一直失败。

检查发现 Clash 侧 `verge.yaml` 里 `enable_auto_launch: false`
（2026-09-21 的备份里也是 false，说明一直是关的），`HKCU\...\Run` 里也没有条目。

做法：在**启动文件夹**里放一个快捷方式（而不是写注册表、也不改动 `verge.yaml`）：

```
C:\Users\26050\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Clash Verge.lnk
  → C:\Program Files\Clash Verge\clash-verge.exe
```

为什么选启动文件夹而不是别的：
  * 它**在用户登录时的桌面会话里**启动，正是我们需要的会话；
  * 它是一眼能看见、随手能删的文件，比注册表项好审计；
  * 不改 `verge.yaml`，避免 Clash 自己再注册一份（`enable_auto_launch` 保持 false），
    保证**全机只有一个自启机制**。

代价：登录后会弹 Clash 主窗口（`enable_silent_start` 也是 false）。
如果想静默起托盘，在 Clash 设置里打开「静默启动」并把那个快捷方式删掉、
改用 Clash 自己的「开机自启」开关即可（两者只保留一个）。

---

## 9. 明确不做的方案

**不做**「会话 0 里端口没监听就直接拉无界面的 `verge-mihomo.exe`」兜底（用户已否决）。
理由：`verge-mihomo.exe` 现在是 `clash_verge_service` 管的，再起一个会抢同一个端口，
需要一整套互斥/接管判断，风险大于收益；打开开机自启之后覆盖率已经足够。

**不做**「会话号 != 桌面会话就杀 GUI」的宽判据。本机实测有两个桌面会话
（explorer 在会话 1 和会话 2 各一个），宽判据会误杀用户正在用的 GUI。

