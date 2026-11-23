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

## 7. 运行期日志 TAG 清单（实测为准）

**权威来源是运行日志，不是源码 grep。** 因为部分 TAG 是间接传入 logger 的
（例：`checkpointActiveSession('LOGIN-CHECKPOINT')`、`checkpointActiveSession('PRE-BROWSER-CLOSE')`），
只 grep `logger.xxx(…, 'TAG', …)` 会漏掉这些（实测能扫到 105 个直接调用，但仍不全）。

下表是 2026-11-23 从 VM 的 `crash-2026-11-23.log`（前 2 万行）与当天正常日志的并集里
提取的**实际出现过**的 42 个：

| TAG | 建议中文 | 当前任务 |
|---|---|---|
| `BROWSER` | 启动浏览器 | 是 |
| `SESSION` | 恢复会话 | 是 |
| `LOGIN` | 登录流程 | 是 |
| `LOGIN-BING` | Bing 会话验证 | 是 |
| `LOGIN-APP` | App 会话登录 | 是 |
| `DETECT-STATE` | 识别页面状态 | 是 |
| `HANDLE-STATE` | 处理页面状态 | 是 |
| `LOGIN-CHECKPOINT` | 登录检查点 | 是 |
| `GET-APP-TOKEN` | 获取 App 令牌 | 是 |
| `GET-DASHBOARD-DATA` | 获取仪表板 | 是 |
| `GET-REWARD-SESSION` | 获取奖励会话 | 是 |
| `COOKIE-SYNC` | 同步 Cookie | 是 |
| `REACT-PARSE` | 解析奖励数据 | 是 |
| `BUILD` | 构建页面数据 | 是 |
| `BOOTSTRAP` | 引导积分页 | 是 |
| `BROWSER-FINGERPRINT` | 浏览器指纹 | 是 |
| `RELOAD-BAD-PAGE` | 重载异常页面 | 是 |
| `CLOSE-BROWSER` | 关闭浏览器 | 是 |
| `PRE-BROWSER-CLOSE` | 关浏览器前检查点 | 是 |
| `DAILY-SET` | 每日任务集 | 是 |
| `DAILY-CHECK-IN` | 每日签到 | 是 |
| `MORE-PROMOTIONS` | 更多推广活动 | 是 |
| `PUNCHCARD` | 打卡卡片 | 是 |
| `APP-PROMOTIONS` | 应用推广 | 是 |
| `READ-TO-EARN` | 阅读赚积分 | 是 |
| `ACTIVATE-SEARCH-PERK` | 搜索特权激活 | 是 |
| `CLAIM-BONUS-POINTS` | 领取奖励积分 | 是 |
| `URL-REWARD` | URL 奖励 | 是 |
| `SEARCH-BING` | Bing 搜索 | 是 |
| `SEARCH-MANAGER` | 搜索管理 | 是 |
| `SEARCH-CHINA-TRENDS` | 中国热搜源 | 是 |
| `ACTIVITY` | 活动执行 | 是 |
| `FLOW` | 任务间隔等待 / 开始创建会话 | 是* |
| `ACCOUNT-START` | 开始账号 | 是 |
| `ACCOUNT-END` | 账号完成 | 是 |
| `ACCOUNT-PARTIAL` | 账号部分完成 | 是 |
| `ACCOUNT-DELAY` | 账号间等待 | 是 |
| `POINTS` | 积分结算 | 是 |
| `RUN-START` | 整轮启动 | 否 |
| `RUN-END` | 整轮结束 | 否 |
| `UNCAUGHT-EXCEPTION` | 未捕获异常 | 否 |

**「当前任务」列**：`是` = 适合显示在「正在做 XX」状态卡；`否` = 只适合日志时间线
（整轮开始/结束、异常这类是状态而不是进度）。

> \* `FLOW` 有**双语义**，别只看名字：`MAIN [FLOW] 开始为 xxx 创建会话` 与
> `MOBILE [FLOW] 大任务间隔等待 | N秒` 完全是两回事。运行期间绝大多数时间是后者，
> 所以默认按「任务间隔等待」显示更贴切。

**未登记的 TAG 一律原样显示**。源码里另有约 60 个不常触发的 TAG
（`QUERY-*`、`SEARCH-*` 多源、`VISUAL-SEARCH-*`、`CLUSTER-*`、`USERAGENT-*` 等），
首次出现时显示原样即可，确认常用后再补中文名到本表。

**上游新增频率**：2026.11.11 → 19.2 → 23.1 两轮更新里，TAG **名称**没有变化
（23.1 只是在 `GHOST-CLICK` 上多了一个「点击已触发跳转」的 debug 分支）。
属于低频事件，**人工同步足够，不需要运行时解析本文档**。

**给 UI 的建议**（与网关侧讨论结果一致）：UI 保留自有映射作为运行时主数据，
本文档作为对账依据；不要运行时读这个文件——UI 是 PyInstaller 单文件包，
依赖另一个目录的 Markdown 会多一个耦合点，且表格格式变动会让解析崩掉。

### 7.1 与 UI 现有映射的精确差异（2026-11-23 核对）

三方数据：`ui/logparse.py` 的 `_CODE_TITLE` **65 条** / 源码 TAG **107 个** /
本轮在 VM 实际出现 **42 个**。

> 网关侧此前汇总时用的是**手打的 31 条**，与真实 65 条不符；这也是「8 个 TAG 不存在」的直接成因
> （`LOGIN-2FA` / `PUNCH-CARDS` / `SEARCH-DESKTOP` 等是汇总时的近似写法）。
> **对比差异时务必直接从 `ui/logparse.py` dump key 列表，不要手打。**

#### UI 该删的 13 条（死映射：源码 11.11.1 与 23.1 两个版本里都不存在，永远不会命中）

`CONTEXT-REFRESH-COOKIE-SEED`、`DAILY`、`EARN`、`ERROR`、`LOGIN-2FA`、`LOGIN-PASSKEY`、
`REDEEM`、`SEARCH`、`SEARCH-COOKIE-CAPTURE`、`SEARCH-COOKIE-SEED`、`SEARCH-DESKTOP`、
`SEARCH-MOBILE`、`STREAK`

> 注意几个容易混淆的真实名：它是 `LOGIN-TOTP` 不是 `LOGIN-2FA`；是 `PUNCHCARD` 不是 `PUNCH-CARDS`；
> 是 `EARN-SNAPSHOT` / `ENABLE-STREAK-PROTECTION`，没有裸的 `EARN` / `STREAK`；
> 是 `ACCOUNT-ERROR` / `MAIN-ERROR`，没有裸的 `ERROR`；搜索相关只有 `SEARCH-BING` /
> `SEARCH-MANAGER` / `SEARCH-CHINA-TRENDS` 等，没有裸 `SEARCH` 或按平台拆分的 `SEARCH-DESKTOP|MOBILE`。
>
> 一个永远命中不了的映射只是噪音，删掉比留着干净。

#### UI 该补的 4 条（本轮真实出现过、但没有中文名，状态卡/时间线会显示英文）

| TAG | 建议中文 |
|---|---|
| `ACTIVITY` | 活动执行 |
| `BOOTSTRAP` | 引导积分页 |
| `RUN-START` | 整轮启动 |
| `UNCAUGHT-EXCEPTION` | 未捕获异常 |

#### 其余 51 个源码 TAG

本轮没有触发，UI 也没有映射 —— **按原样显示即可**，确认常用后再补。
主要包括 `CLUSTER-*`、`LOGIN-*` 细分阶段（`LOGIN-TOTP` / `LOGIN-RECOVERY` / `LOGIN-PASSWORDLESS` …）、
多源 `SEARCH-*`、`VISUAL-SEARCH-*`、`USERAGENT-*`、`*ERROR` 等。
