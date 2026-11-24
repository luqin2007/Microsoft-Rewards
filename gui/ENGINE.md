# MRS 引擎（gui/engine.py）交付说明

对应 `需求-MRS引擎.md`。网关只需 `import engine` 并持有 `MrsEngine` 实例，
不必接触 `subprocess`、`run-state.json` 或 `gui/app.py`。

## 1. 改了哪些文件

| 文件 | 变更 | 说明 |
|---|---|---|
| `gui/engine.py` | **新增** | `MrsEngine`：健康检查 / 启停 / 状态 / 日志 / 配置 / 账号 / 元数据 |
| `gui/runner.py` | 小改 | `BotRunner.__init__(write_state=True)`；引擎传 `False` 接管状态文件写入。GUI 不传参数，行为与改动前完全一致 |
| `gui/config_store.py` | **未改** | 接口签名原样保留，仍无 tkinter 依赖 |
| `gui/env_store.py` | **未改** | 同上 |
| `gui/app.py` | **未改** | 旧 GUI 保留作参考 |
| `src/**` | **未改** | 业务逻辑零改动（`git diff src/` 为空） |

`engine.py` 只用标准库 + 上述既有模块，导入时不触发 tkinter / customtkinter。

## 2. `child_env_for_slot` 的语义（需求 §2.1 要求写明）

`env_store.child_env_for_slot(path, slot)` 做两件事：

1. 把**所有**已出现槽位的 `ACCOUNT_<n>_EMAIL` 置为空串（空串在 `Load.ts` 里等于"未设置"，用来屏蔽宿主进程可能残留的值）；
2. 把目标槽位的**全部字段**重映射成 `ACCOUNT_1_*`。

即：**不是"注释掉其他槽位"，而是"清屏 + 把选中的那个搬进 1 号位"**。
这样 `Load.ts` 只会解析出一个账号，且不需要修改 `.env`。

`slot=None` 时它只返回第 1 步的"清屏"结果，**不适用于"跑全部账号"**。
跑全部由 `MrsEngine` 处理：不传任何 `ACCOUNT_*` 覆盖，`BotRunner` 会先剔除宿主
环境里的 `ACCOUNT_*`，让 node 自己从 `.env` 读全部槽位。

## 3. 日志与状态解析策略

需求 §2.3 建议"优先在 TS 侧加结构化日志"，但 §0.2 同时禁止改 `src/`。
这里遵守硬约束（不改 TS），改为在 Python 侧解析**现有的结构化日志行**，
锚点都是稳定的 TAG（`Logger` 的第二段方括号内容），而不是脆弱的自由文本：

| TAG | 提取 |
|---|---|
| `ACCOUNT-START` | `currentAccount`（`开始处理账户: <email>`） |
| `ACCOUNT-END` | `pointsGained += 获得积分=N`，`progress.done += 1` |
| `ACCOUNT-SKIP` | `progress.done += 1` |
| `RUN-END` | 最终 `处理账户数 / 失败账户数 / 获得积分 / 现余额` 覆盖累计值 |

**注意**：合并 2026.11.19 后日志里的邮箱已被 `maskEmails()` 脱敏成 `abc****@domain`，
所以 `currentAccount` 也是脱敏形式。UI 若需完整邮箱，用 `list_accounts()` 的
`email` 与脱敏前缀匹配即可（前 3 位一致）。

若将来 TAG 改名或结构变化，只需改 `engine.py` 顶部四个正则，不影响网关。

## 4. 网关怎么调

```python
import sys
sys.path.insert(0, str(MRS_ROOT / 'gui'))
from engine import MrsEngine

eng = MrsEngine(str(MRS_ROOT))

eng.health()                      # 自检，实测 ~300ms（首次含 node --version，之后走缓存）
eng.presets()                     # worker 定义 + 档次/节奏档位 + 账号字段清单
eng.get_config() / eng.put_config(patch)
eng.get_simple() / eng.put_simple(simple)
eng.list_accounts()               # 只有 has_password / has_totp，无明文
eng.upsert_account(slot_or_None, {'email': ..., 'password': ...})
eng.delete_account(slot)          # 注释而非删除

eng.start('all')                  # 或 'slot:N'
eng.status()                      # 合并 run-state.json + 进程存活 + 本次进度
eng.logs(after_seq=0, limit=200)  # 增量
eng.stop()                        # 进程树 + 独占的浏览器
```

> **关于 `native` 每次新建实例**：`gateway/adapters/mrs.py` 的 `native` property
> 每访问一次就 `MrsEngine(MRS_ROOT)` 一次。引擎已针对这一点做了两处兜底：
> `status()`/`stop()` 从 `run-state.json` + pid 存活判断（不依赖进程内句柄），
> `logs()` 从落盘文件恢复。但 `_progress`（本次运行的内存进度）只存在于
> 「start 的那个实例」里；若网关的 `/mrs/status` 走的是另一个实例，进度会回退到
> 状态文件里的快照（同样是准的，只是不是逐秒更新）。若希望实时进度，
> 网关侧应缓存实例（`self._native = MrsEngine(...)`），这是 gateway/ 的改动，MRS 不越界。

`start()` 抛 `RuntimeError`（已在运行 / 无账号 / 找不到 node）或
`ValueError`（scope 不是 `all` / `slot:N`、槽位不存在），网关直接映射成 409/400。

## 5. `run-state.json` 扩展字段

保持原有格式，追加：

| 字段 | 何时更新 | 用途 |
|---|---|---|
| `scope` | 启动时 | `all` / `slot:N` |
| `currentAccount` | 进入账号时；`RUN-END` 清空 | 当前处理谁（**已脱敏**） |
| `currentTask` | 每条业务日志（TAG 变化时） | 当前在做什么，如 `READ-TO-EARN` / `URL-REWARD` |
| `pointsGained` | 账号结束时累加；`RUN-END` 校准 | 本次累计获得 |
| `progress.done/total` | 账号结束 / 启动 | 进度条 |
| `progress.failed/balance` | `RUN-END`（`balance` 也会从业务行实时更新） | 失败数 / 当前余额 |
| `lastLogAt` | 每条日志 | 判断日志是否还在推进（区别于 `updatedAt`）。**建议消费方用它算 `logAgeSec` 并给较大阈值（600 秒）**：`[FLOW] 大任务间隔等待` 实测出现过 234 秒，90 秒级别的阈值会把正常等待误报成卡死 |
| `updatedAt` | 心跳（每 10 秒）+ 事件 | 状态新鲜度；UI 可用它做「可能卡住」判断 |

写盘策略：账号级事件（`ACCOUNT-START/END/SKIP`、`RUN-END`）**立即写**；普通业务行**节流 3 秒**；
另有心跳线程每 10 秒刷一次 `updatedAt`——单个账号动辄十几分钟，不这么做 UI 会以为卡死。

终态（idle）同样保留最后一次的 `progress` / `pointsGained` / `currentTask`，UI 跑完还能看到结果。

**`status()` 的 pid 语义**：以 `run-state.json` 为准（文件说 running 且该 pid 存活才认），
内存里的运行器只作补充。原因是网关每次访问 `native` 都会新建一个 `MrsEngine`，
「内存里有 runner」不代表它就是在跑的那一个。

**`startedAt` 的语义**：本轮**进程**的启动时刻，取自 `Popen` 之后，与进程真实创建时间差 < 200ms
（PID 占位那次写的是 `null`）。消费方可以用「进程创建时间 ≈ `startedAt`」判断 pid 有没有被系统复用：
两边差超过 5 秒即视为 `pid` 不属于本轮。拿不到创建时间或 `startedAt` 为空时，**一律按在跑处理**——
误报在跑比放行重复启动安全（后者会让两份引擎抢同一批账号）。

## 6. 已知限制

| 项 | 说明 |
|---|---|
| **不要在 gui/ 的模块里写 `from __future__ import annotations`** | 这些文件是给网关的 **frozen（PyInstaller）进程**在运行时从磁盘 import 的。实测：frozen 宿主若没把 `__future__` 打进去，加载带该语句的外部模块会直接 `ModuleNotFoundError: No module named '__future__'`。四个文件已全部去掉；将来加回来前先确认宿主补了 `hiddenimports=['__future__']` |
| **配置路径** | 引擎读写 `MRS_ROOT/config.json` 与 `MRS_ROOT/.env`。合并 2026.11.19 后 node 会把它们**复制**到 `%LOCALAPPDATA%\MRS助手`（按 mtime 单向同步：程序目录更新就覆盖过去），所以网关写程序目录那份也能生效。网关是唯一写入方（需求 §6）时不会打架 |
| **日志持久化** | `logs()` 先读内存环形缓冲，缓冲为空时从 `logs/engine-log.jsonl` 尾部恢复；每行同时追加到该文件。所以网关每次 `native` 新建实例、甚至网关重启后，`after_seq` 增量拉取仍然连续。`start()` 会把 seq 归零并重建该文件（网关据此判断「新一轮」） |
| **浏览器清理是 best-effort** | `stop()` 会额外按命令行含 `ms-playwright/playwright/patchright` 清理 Chrome/Edge，只动 MRS 自己拉起的浏览器，不碰用户日常浏览器 |
| **集群模式进度** | `clusters > 1` 时 `progress.total` 以 `RUN-END` 的"处理账户数"为准，账号边界事件来自各 worker 的 IPC 汇总，粒度到账号而非子任务 |

## 7. 运行期日志 TAG 清单（实测 66 个）

**权威来源是运行日志。** 源码 grep 会漏（`LOGIN-CHECKPOINT` / `PRE-BROWSER-CLOSE` 走
`checkpointActiveSession('TAG')` 间接传参；`SEARCH-COOKIE-*` / `CONTEXT-REFRESH-*` 也是同类）。
下表取自 VM `C:\Tools\MRS助手\logs\*.log` 中 **26 个文件**（排除 231MB 的 crash 日志），
用严格正则只匹配「级别 + 平台 + [TAG]」的第三段，共 **66 个真实 TAG**：

| TAG | 中文 | 出现次数 | 当前任务 |
|---|---|---|---|
| `REACT-PARSE` | 解析奖励数据 | 18291 | 是 |
| `BOOTSTRAP` | 引导积分页 | 17681 | 是 |
| `COOKIE-SYNC` | 同步 Cookie | 4658 | 是 |
| `GET-DASHBOARD-DATA` | 读取仪表盘数据 | 4337 | 是 |
| `LOGIN` | 开始登录 | 3784 | 是 |
| `READ-TO-EARN` | 阅读赚积分 | 3324 | 是 |
| `QUERY-CLUSTER` | 查询任务簇 | 2610 | 是 |
| `GHOST-CLICK` | 补点未生效任务 | 2406 | 是 |
| `SEARCH-CLOSE-TABS` | 关闭搜索标签页 | 2343 | 是 |
| `QUERY-QUEUE` | 处理任务队列 | 2324 | 是 |
| `LOGIN-BING` | Bing 会话验证 | 2303 | 是 |
| `DETECT-STATE` | 检测页面状态 | 1765 | 是 |
| `SEARCH-BING` | Bing 搜索 | 1583 | 是 |
| `FLOW` | 任务间隔等待 | 1346 | 是 |
| `SEARCH-COOKIE-SEED` | 播种搜索 Cookie | 1252 | 是 |
| `BROWSER` | 启动浏览器 | 1243 | 是 |
| `SEARCH-COOKIE-CAPTURE` | 捕获搜索 Cookie | 1190 | 是 |
| `ACTIVITY` | 活动执行 | 796 | 是 |
| `LOGIN-APP` | App 登录 | 780 | 是 |
| `DAILY-CHECK-IN` | 每日签到 | 724 | 是 |
| `URL-REWARD` | 领取链接奖励 | 713 | 是 |
| `GET-REWARD-SESSION` | 获取奖励会话 | 632 | 是 |
| `CLOSE-BROWSER` | 关闭浏览器 | 584 | 是 |
| `PRE-BROWSER-CLOSE` | 关闭前收尾 | 579 | 是 |
| `HANDLE-STATE` | 处理页面状态 | 562 | 是 |
| `LOGIN-CHECKPOINT` | 登录检查点 | 548 | 是 |
| `PUNCHCARD` | 打卡卡片 | 368 | 是 |
| `EARN-SNAPSHOT` | 积分快照 | 362 | 否 |
| `QUERY-MANAGER` | 查询调度 | 348 | 是 |
| `CLAIM-BONUS-POINTS` | 领取奖励积分 | 320 | 是 |
| `SESSION` | 恢复会话 | 317 | 是 |
| `BROWSER-FINGERPRINT` | 浏览器指纹 | 314 | 是 |
| `SEARCH-CHINA-TRENDS` | 中国区热词 | 296 | 是 |
| `BUILD` | 构建请求 | 277 | 是 |
| `SEARCH-MANAGER` | 搜索调度 | 266 | 是 |
| `DAILY-SET` | 每日任务集 | 264 | 是 |
| `ACCOUNT-START` | 开始处理账户 | 178 | 否 |
| `MORE-PROMOTIONS` | 更多推广活动 | 163 | 是 |
| `POINTS` | 积分结算 | 149 | 是 |
| `ENABLE-STREAK-PROTECTION` | 开启连击保护 | 149 | 是 |
| `GET-APP-TOKEN` | 获取 App Token | 148 | 是 |
| `RUN-START` | 整轮启动 | 136 | 否 |
| `ACTIVATE-SEARCH-PERK` | 激活搜索特权 | 129 | 是 |
| `ACCOUNT-END` | 账户完成 | 124 | 否 |
| `RUN-END` | 本轮结束 | 121 | 否 |
| `APP-PROMOTIONS` | App 推广活动 | 112 | 是 |
| `APP-REWARD` | App 奖励 | 84 | 是 |
| `ACCOUNT-DELAY` | 账号间隔等待 | 44 | 否 |
| `RELOAD-BAD-PAGE` | 重载异常页面 | 33 | 是 |
| `SEARCH-BING-RELATED` | Bing 相关搜索 | 32 | 是 |
| `REWARDS-PAGE` | Rewards 页面 | 26 | 是 |
| `SEARCH-BING-SUGGESTIONS` | Bing 搜索建议 | 26 | 是 |
| `ACCOUNT-PARTIAL` | 账号部分完成 | 15 | 否 |
| `HUMANIZE` | 拟人化操作 | 13 | 是 |
| `LOGIN-TOTP` | 两步验证 | 8 | 是 |
| `MAIN-ERROR` | 主流程异常 | 6 | 否 |
| `GET-APP-DASHBOARD-DATA` | 读取 App 仪表盘 | 4 | 是 |
| `CONTEXT-REFRESH` | 刷新上下文 | 4 | 是 |
| `DISMISS-ALL-MESSAGES` | 关闭弹窗 | 4 | 是 |
| `LOGIN-ENTER-PASSWORD` | 输入密码 | 4 | 是 |
| `LOGIN-ENTER-EMAIL` | 输入邮箱 | 3 | 是 |
| `DISABLE-FIDO` | 关闭 Passkey 提示 | 3 | 是 |
| `SEARCH-RANDOM-SCROLL` | 随机滚动 | 1 | 是 |
| `CONTEXT-REFRESH-COOKIE-SEED` | 刷新并播种 Cookie | 1 | 是 |
| `USERAGENT-EDGE-VERSION` | 查询 Edge 版本 | 1 | 是 |
| `GET-CURRENT-POINTS` | 读取当前积分 | 1 | 是 |

**「当前任务」列**：`是` = 适合「正在做 XX」状态卡；`否` = 只适合日志时间线
（账号边界 `ACCOUNT-*`、整轮 `RUN-*`、一次性快照 `EARN-SNAPSHOT`、异常 `MAIN-ERROR`）。

> **`FLOW` 双语义**：`MAIN [FLOW] 开始为 xxx 创建会话` 与 `MOBILE [FLOW] 大任务间隔等待 | N秒`
> 是两回事；运行期绝大多数时间是后者，默认按「任务间隔等待」显示。

### 7.1 与 UI 现有映射的差异（2026-11-23，以 26 个日志为准）

**UI 缺的 11 个**（会显示英文，按频次）：

| TAG | 次数 | 建议中文 |
|---|---|---|
| `BOOTSTRAP` | 17681 | 引导积分页 |
| `ACTIVITY` | 796 | 活动执行 |
| `RUN-START` | 136 | 整轮启动 |
| `APP-REWARD` | 84 | App 奖励 |
| `LOGIN-TOTP` | 8 | 两步验证 |
| `DISMISS-ALL-MESSAGES` | 4 | 关闭弹窗 |
| `LOGIN-ENTER-PASSWORD` | 4 | 输入密码 |
| `LOGIN-ENTER-EMAIL` | 3 | 输入邮箱 |
| `DISABLE-FIDO` | 3 | 关闭 Passkey 提示 |
| `USERAGENT-EDGE-VERSION` | 1 | 查询 Edge 版本 |
| `GET-CURRENT-POINTS` | 1 | 读取当前积分 |

**UI 有、但这 26 个日志里从未出现过的 10 个**：

`LOGIN-2FA` `LOGIN-PASSKEY` `SEARCH` `SEARCH-DESKTOP` `SEARCH-MOBILE` `DAILY` `ERROR` `EARN` `REDEEM` `STREAK`

其中 `LOGIN-2FA` / `LOGIN-PASSKEY` / `SEARCH` / `SEARCH-DESKTOP` / `SEARCH-MOBILE` /
`DAILY` / `ERROR` / `EARN` / `REDEEM` / `STREAK` 在源码里也查不到对应写法，
基本可判定为**手写近似名**（真实名分别是 `LOGIN-TOTP`、无、`SEARCH-BING`、`SEARCH-BING`、
`DAILY-SET`、`*-ERROR`、`EARN-SNAPSHOT`、无、`ENABLE-STREAK-PROTECTION`）。
`LOGIN-2FA` / `LOGIN-PASSKEY` 这两个也可能只是这批账号没走到那一步——**建议先注释保留、跑两周再删**。

> 更正：`SEARCH-COOKIE-SEED` / `SEARCH-COOKIE-CAPTURE` / `CONTEXT-REFRESH-COOKIE-SEED`
> 一度被误判为死映射，实际分别出现 1252 / 1190 / 1 次，是真实 TAG。

**未登记的 TAG 一律原样显示**（源码里还有约 40 个不常触发的，如 `CLUSTER-*`、`VISUAL-SEARCH-*`、
`USERAGENT-*`、多源 `SEARCH-*`）。

**上游新增频率**：`2026.11.11 → 19.2 → 23.1` 两轮更新里 TAG 名称零变化，人工同步足够，
不需要运行时解析本文档。UI 保留自有映射为主，本文档作对账依据。

### 7.2 「动作型」与「状态型」TAG 的划分

UI 有个「当前任务」状态卡，需要知道哪些 TAG 是「正在做什么」、哪些只是「发生了什么」。

**划分规则很简单，不用额外字段**：

| 集合 | 类型 | 用途 |
|---|---|---|
| `stages[].tags`（87 个） | **动作型** | 可以显示为「正在 阅读赚积分」 |
| `boundaryTags`（21 个） | **状态型** | 不显示为当前任务（账号/整轮边界、异常、集群） |
| `skipTags`（2 个） | **跳过信号** | 既不是动作也不是状态，要看消息内容判定 |

所以 UI 的「状态型名单」可以直接用 `boundaryTags + skipTags`，**不必自己硬编码维护**。

**`ACCOUNT-START` 为什么算状态型**：它虽然字面是「开始处理账户」，但属于**边界事件**——
「当前处理的是哪个账号」已经由 `run-state.json` 的 `currentAccount` 字段单独表达。
如果让它进「当前任务」，会把真正在做的动作（如 `READ-TO-EARN`）覆盖掉，
用户看到「当前任务：开始处理账户」，反而不知道在干什么。

**同一时刻只应有一个动作型 TAG 占据「当前任务」**。日志里动作型 TAG 交替出现属于正常
（引导→解析→搜索→阅读…），UI 按「最近一个动作型 TAG」显示即可；
状态型 TAG 出现时**不要把当前的「当前任务」清空**，除非同时带了账号切换（`ACCOUNT-END`）。

### 7.3 TAG 的两种产生方式（统计时必须都算上）

不是所有 TAG 都写成 `logger.info(…, 'TAG', …)` 的字面量。有一类是通过函数参数间接传进 logger 的：

```typescript
await this.browser.func.checkpointActiveSession('LOGIN-CHECKPOINT')      // → logger.debug(…, source, …)
await this.browser.func.checkpointActiveSession('PRE-BROWSER-CLOSE')
await this.bot.browser.func.synchronizeActiveBrowserCookies('SEARCH-COOKIE-SEED', true)
await this.bot.browser.func.synchronizeActiveBrowserCookies('SEARCH-COOKIE-CAPTURE')
await this.browser.func.synchronizeActiveBrowserCookies('CONTEXT-REFRESH-COOKIE-SEED', true)
```

**只 grep `logger\.xxx\(…'TAG'…` 会漏掉这 5 个**，进而误判成「源码里不存在」或「永远不会命中」。
实际它们都会命中，而且都走 `logger.debug` —— 即 `debugLogs=false` 时**不落盘**。

统计「某 TAG 有没有日志 / 是什么级别」时，必须同时扫这三类调用：

```
logger.(info|warn|error|debug)(..., 'TAG', ...)
checkpointActiveSession('TAG')
synchronizeActiveBrowserCookies('TAG' [, true])
```

> 本文档 §7 的 87 个阶段 TAG 里，有 20+ 个只在 **debug** 级出现（上表 5 个 + `COOKIE-SYNC` /
> `QUERY-*` / `SEARCH-*` 多源等）。默认 `debugLogs=false` 时它们不会出现在日志里 ——
> 这不影响「阶段能不能被看见」，因为**每个阶段都至少有一个 info 级 TAG 兜底**；
> 但如果将来往 `stages()` 里加新 TAG，别只加 debug 级的，UI 会一直显示「还没到」。
