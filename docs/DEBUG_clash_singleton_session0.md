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
需要一整套互斥/接管判断，风险大于收益。

**不做**「会话号 != 桌面会话就杀 GUI」的宽判据。本机实测有两个桌面会话
（explorer 在会话 1 和会话 2 各一个），宽判据会误杀用户正在用的 GUI。

---

## 10. ★★★ 后续（2026-10-15）：光"拒绝"不够，改成"委托" ★★★

### 10.1 只拒绝的代价：24 小时里 4 次运行颗粒无收

第 4 节的修复（拒绝在无桌面会话拉 GUI）+ 第 8 节的开机自启，**实测证明不够**：

| 时间 | 结果 | 说明 |
| --- | --- | --- |
| 10-14 12:12 | 失败 | 代理未检测到（用户当天还没重新登录，自启快捷方式没机会跑） |
| 10-14 14:28 | 失败 | 代理未检测到 |
| 10-14 21:05 | 失败 | 代理在，但连接被重置 |
| 10-15 09:58 | 失败 | **结构性无解**：机器 09:47 开机、用户 10:01 登录，任务 09:58 触发 |

⇒ 我此前判断的「打开开机自启之后覆盖率已经足够」**是错的**，已作废。

### 10.2 修法：会话 0 不自己拉 GUI，但可以**请桌面会话去拉**

关键发现（实测，不需要管理员、不需要账户密码）：

```
schtasks /Create /TN <name> /TR <bat> /SC ONCE /ST 23:59 /RU "young\蔡向阳" /IT /F
  -> SUCCESS（**全程没有要求密码**）
  -> LogonType = Interactive
schtasks /Run /TN <name>
  -> 任务跑在 **Session=2**（用户的桌面会话）
```

也就是说：`schtasks /Run` 走的是计划任务服务（SYSTEM），
**谁调用它、在哪个会话调用它都不重要** —— 它能把一个 Interactive 任务
放进用户的桌面会话里执行。这正是"把 GUI 拉到有桌面的地方"所需的全部能力。

实现：

- 新入口 `polytrade/ensureproxy.py` + `polytrade_proxy_up.bat`（只做一件事：保证代理可用）；
- 计划任务 `polytrade_proxy_up`（Interactive，`/SC ONCE` 挂在 23:59 上，平时不会自己触发）；
- `config.ensure_proxy()` 在「无桌面 + 端口没监听」时不再直接放弃，而是
  `schtasks /Run polytrade_proxy_up`，然后轮询等待代理起来。

**端到端实测**（把 `polytrade_run.bat` 临时换成探针，触发真正的会话 0 任务）：

```
SESSION          = 0
interactive      = False
  [proxy] ⚠ 端口未监听，且本进程无桌面（会话 0）——自己拉 GUI 会抢走单例锁，
          改为委托交互式任务 polytrade_proxy_up
  [proxy] ✗ 辅助任务在 30s 内没能拉起代理 —— 通常意味着**当前桌面上没人登录**
--- 同时 proxy_up.log 里出现了 ---
[ensureproxy] 在 Session=2 里尝试保证代理可用
[ensureproxy] ✓ 代理已就绪: http://127.0.0.1:7897
```

⇒ 会话 0 → schtasks → **桌面会话（Session=2）** 这条链路真的通了
（探针里代理"拉不起来"是因为我把候选端口指到了没人监听的 7999，
故意让它走委托分支，真实的 7897 全程未受影响）。

至此仍然**未被实测**的只剩最后一小步：桌面会话里"端口真的没监听时启动 GUI"。
那是 `ensure_proxy` 原本就有的路径（10-13 那次误启动证明了它确实能拉起 Clash），
只是当时跑在了错误的会话里；现在它跑在正确的会话里。
验证它需要真的杀掉用户的 Clash，代价不值得，故**如实标注为未验证**。

### 10.3 一个必须注明的机器级改动

`polytrade_proxy_up` 是**注册在系统里的计划任务**，不在 git 仓库里。
换机器或重装后要重新注册：

```powershell
schtasks /Create /TN "polytrade_proxy_up" /SC ONCE /ST 23:59 /RU "<用户名>" /IT /F `
  /TR "D:\26050\Documents\quant_system\polytrade_proxy_up.bat"
```

---

## 11. 顺带修掉的两个"留痕"类 bug（2026-10-15）

### 11.1 GBK 的 stdout 会让"留痕"这一步自己崩掉

实测：本机 ANSI 代码页是 cp936，stdout **重定向到文件**时
`sys.stdout.encoding` 就是 `gbk` —— **batch 里写 `chcp 65001` 也改不了**
（chcp 管控制台，不管重定向）：

```
不带 chcp      : stdout.encoding = gbk
带 chcp 65001  : stdout.encoding = gbk
```

而日志里有 ⚠ ✓ ✗ ★ → 这些**不在 GBK 里**的字符，`print` 会抛 `UnicodeEncodeError`。

**之前为什么没炸？纯属巧合。** `report.py` / `digest.py` / `invariants.py` /
`equity.py` / `match.py` / `refodds.py` / `aiforecast.py` / `papermm.py`
各自在模块层做了一次 `sys.stdout.reconfigure(errors="replace")` ——
谁先被 import，谁就顺手把全局修好了。`run.py` 恰好 import 了它们；
而新的 `polytrade.ensureproxy` 只 import `config`，于是它一 print `✓` 就崩：

```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
```

修法：在**入口显式**建立编码安全（`run.py` / `ensureproxy.py` 顶部
统一 `reconfigure(encoding="utf-8", errors="replace")`），
不再依赖 import 副作用。顺带把日志统一成 UTF-8
（旧的 GBK `cron.log` 已归档为 `cron_20261015_gbk.log`）——
之前正是这份 GBK + 缓冲的日志让我误读、并据此下了一个错误结论
（见 `DEBUG_run_timeout_30min.md` 第 5 节）。

### 11.2 `out()` 在 print 失败时会"两条输出悄悄分叉"

`run.out()` 原本是"先 append 到内存列表、再 print"。stdout 是 GBK 时，
print 抛异常，而调用点外面包着 `try/except Exception: pass` —— 于是那一行
**只进了写文件的 `lines`，没进 stdout（cron.log）**。
留痕函数的首要职责是"一定留得下"，所以现在 `out()` 自己兜住 print 异常。

