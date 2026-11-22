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
eng.get_config() / eng.put_config(patch)   # UI 精简结构（见 §4.1）
eng.get_simple() / eng.put_simple(simple)  # 同上，但不含派生档位
eng.list_accounts()               # 只有 has_password / has_totp，无明文
eng.upsert_account(slot_or_None, {'email': ..., 'password': ...})
eng.delete_account(slot)          # 注释而非删除

eng.start('all')                  # 或 'slot:N'
eng.status()                      # 合并 run-state.json + 进程存活 + 本次进度
eng.logs(after_seq=0, limit=200)  # 增量
eng.stop()                        # 进程树 + 独占的浏览器
```

`start()` 抛 `RuntimeError`（已在运行 / 无账号 / 找不到 node）或
`ValueError`（scope 不是 `all` / `slot:N`、槽位不存在），网关直接映射成 409/400。

### 4.1 配置读写：精简结构，不是 config.json 原文

`get_config()` 返回的是 **UI 用的精简结构**，不是 `config.json` 的原始内容：

```python
{
  'headless': False, 'clusters': 1, 'debugLogs': False,
  'humanize': True,                  # 布尔，不是字典
  'skipNonPointTasks': True, 'ensureStreakProtection': True,
  'browserChannel': 'chromium',
  'workers': {'doDesktopSearch': True, ...},   # 12 个 bool
  'delay_preset': '官方默认',        # 派生
  'preset': '标准',                  # 派生
}
```

`put_config(patch)` 是它的逆操作，按语义拆开处理：

* `preset` / `delay_preset` 是**派生档位**，只用来套用开关和间隔，
  **绝不写进 `config.json`**
* `humanize` 传布尔时只改 `humanize.enabled`，字典里 `quietHours` /
  `searchTargetRatio` / `readToEarnArticles` 等子键原样保留
* 载荷里没出现的键保持原值（`apply_simple()` 自带默认值，直接用局部载荷
  会把没提交的开关抹成默认值，所以引擎先拿当前状态铺底）
* 其余原始配置键按深合并处理，兼容传完整配置的调用方

**为什么这么绕**：旧版 `put_config()` 把 UI 载荷直接 `_deep_merge` 进
`config.json`，`humanize: true` 会把整块字典覆盖成布尔，`preset` /
`delay_preset` 也会被当成真键写进去。之后 `config_store.get_simple()`
调 `humanize.get()` 直接 `AttributeError`，而 `apply_simple()` 在
`humanize` 是布尔时 `TypeError`——**写一次就把配置写死**。
现在读侧 `_load_config()` 会把 `humanize` 非字典的情况修回
`{'enabled': bool}`，写侧兜底保证永远是字典，坏配置能自愈。


## 5. `run-state.json` 扩展字段

保持原有格式，追加：`scope`、`currentAccount`、`pointsGained`、
`progress{done,total,failed?,balance?}`、`updatedAt`。

终态（idle）同样保留最后一次的 `progress` / `pointsGained`，UI 跑完还能看到结果。

## 6. 已知限制

| 项 | 说明 |
|---|---|
| **配置路径** | 引擎读写 `MRS_ROOT/config.json` 与 `MRS_ROOT/.env`。合并 2026.11.19 后 node 会把它们**复制**到 `%LOCALAPPDATA%\MRS助手`（按 mtime 单向同步：程序目录更新就覆盖过去），所以网关写程序目录那份也能生效。网关是唯一写入方（需求 §6）时不会打架 |
| **node 版本** | `runner.detect_node()` 会优先挑运行时目录里的 node，实测可能选到 `~/.workbuddy/binaries/node/versions/22.x`，而 `dist/index.js` 要求 `>=24`，一启动就 `exit 1`。构造 `MrsEngine(root, node_exe=...)` 显式指定可绕开；`health()` 会把实际版本报出来 |
| **`scope='all'` 的陷阱** | `env_store.child_env_for_slot(path, None)` 返回的是「把所有槽位 EMAIL 置空」的覆盖表，语义上只适合当「清空」用。`start('all')` 必须传 `env_overrides=None`（让 node 自己读 `.env` 全部账号），传空串覆盖表会导致 `No accounts found` 并 `exit 1` |
| **日志只在内存** | 网关进程重启后 `logs()` 从空开始，`after_seq` 也随之重置；UI 需在重连时把 `afterSeq` 归零 |
| **浏览器清理是 best-effort** | `stop()` 会额外按命令行含 `ms-playwright/playwright/patchright` 清理 Chrome/Edge，只动 MRS 自己拉起的浏览器，不碰用户日常浏览器 |
| **集群模式进度** | `clusters > 1` 时 `progress.total` 以 `RUN-END` 的"处理账户数"为准，账号边界事件来自各 worker 的 IPC 汇总，粒度到账号而非子任务 |