# -*- coding: utf-8 -*-
"""MRS 引擎：把「带 GUI 的桌面程序」暴露成「可被网关调用的引擎」。

网关（gateway/adapters/mrs.py）只跟这个类打交道，不碰 subprocess、不碰
run-state.json。设计约束见 `需求-MRS引擎.md`：

* 不改 `src/`（TypeScript 业务逻辑）与 `gui/app.py`（旧 GUI 保留作参考）
* 不引入新的第三方依赖（只用标准库 + 本仓库既有的 gui/*.py）
* 本文件不 import tkinter，可以被纯 Python 进程直接导入

主要能力：
  health()            快速自检（不启动任务）
  start(scope)        启动 node dist/index.js，scope = 'all' | 'slot:N'
  stop()              停整个进程树（含脱离的浏览器进程）
  status()            读 run-state.json 并校验进程存活
  logs(after_seq)     增量拉取日志（内存环形缓冲）
  get_config/put_config、list_accounts/upsert_account/delete_account
  presets()           任务开关/档位元数据，UI 不用硬编码中文
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config_store as cs
import env_store as ev
import runner as mr

STATE_NAME = 'run-state.json'

# 引擎日志文件（logs/engine-log.jsonl）的自我保护参数。
# 2026-11-23 VM 上撞过一次：debugLogs=true 时 node 一侧 7 分钟输出 105MB，
# 每行 open/close 根本跟不上，最终把 stdout 读线程拖垮。
LOG_MAX_BYTES = 64 * 1024 * 1024       # 单文件上限，超过就轮转成 .1
LOG_TAIL_READ_BYTES = 2 * 1024 * 1024  # 新实例恢复缓冲时只读尾部这么多，不读整个文件

# 日志行固定格式：[时间] [账号] [LEVEL] 平台 [TAG] 正文
LOG_LINE_RE = re.compile(
    r'^\[[^\]]*\]\s*\[[^\]]*\]\s*\[(?P<level>INFO|WARN|ERROR|DEBUG)\]\s*'
    r'(?P<platform>DESKTOP|MOBILE|MAIN)\s*\[(?P<tag>[A-Za-z0-9_.-]+)\]\s*(?P<msg>.*)$'
)
ACCOUNT_START_RE = re.compile(r'开始处理账户:\s*([^\s|]+)')
GAINED_RE = re.compile(r'获得积分=(-?\d+)')
BALANCE_RE = re.compile(r'现余额=(\d+)')
# 业务行（领取/UrlReward/签到…）里带的是 currentBalance=<最新余额>，用它做实时余额
LIVE_BALANCE_RE = re.compile(r'currentBalance=(\d+)')
ACCOUNTS_DONE_RE = re.compile(r'处理账户数=(\d+)')
FAILED_RE = re.compile(r'失败账户数=(\d+)')

# 账号字段（小写别名 → .env 里的大写字段名），网关传小写更顺手
ACCOUNT_FIELD_ALIASES = {
    'email': 'EMAIL',
    'password': 'PASSWORD',
    'totp_secret': 'TOTP_SECRET',
    'recovery_email': 'RECOVERY_EMAIL',
    'geo_locale': 'GEO_LOCALE',
    'lang_code': 'LANG_CODE',
    'proxy_http': 'PROXY_HTTP',
    'proxy_url': 'PROXY_URL',
    'proxy_port': 'PROXY_PORT',
    'proxy_username': 'PROXY_USERNAME',
    'proxy_password': 'PROXY_PASSWORD',
    'save_fingerprint_mobile': 'SAVE_FINGERPRINT_MOBILE',
    'save_fingerprint_desktop': 'SAVE_FINGERPRINT_DESKTOP',
}

# 一个账号完整跑一遍会经历的业务阶段。
#
# 顺序取自 src/index.ts 的 Main()（2026.11.23.1）：移动会话 → 登录 → 仪表板 →
# 连击保护 → 打卡卡片 → 搜索特权 → 每日任务集 → 更多推广 → App 签到 → App 推广 →
# 阅读 → 搜索 → 领取奖励。
#
# 每条带 tags：把日志 TAG 归到阶段，UI 不必靠「出现顺序」猜，也不会因为某个 TAG
# 反复出现而多出条目。optional=True 表示该阶段受配置开关或账号状态控制（例如
# doReadToEarn 关掉、或 App 类任务因没有 accessToken 整组跳过时，就不会发生）。
STAGES: List[Dict[str, Any]] = [
    {
        'code': 'SESSION',
        'label': '创建会话',
        'order': 1,
        'optional': False,
        'tags': ['FLOW', 'BROWSER', 'SESSION', 'BROWSER-FINGERPRINT', 'CLOSE-BROWSER', 'PRE-BROWSER-CLOSE'],
    },
    {
        'code': 'LOGIN',
        'label': '登录',
        'order': 2,
        'optional': False,
        'tags': [
            'LOGIN', 'LOGIN-BING', 'LOGIN-APP', 'LOGIN-TOTP', 'LOGIN-ENTER-EMAIL', 'LOGIN-ENTER-PASSWORD',
            'LOGIN-CHECKPOINT', 'DETECT-STATE', 'HANDLE-STATE', 'DISABLE-FIDO', 'GET-APP-TOKEN',
            'GET-REWARD-SESSION',
        ],
    },
    {
        'code': 'DASHBOARD',
        'label': '读取仪表板',
        'order': 3,
        'optional': False,
        'tags': [
            'GET-DASHBOARD-DATA', 'GET-APP-DASHBOARD-DATA', 'POINTS', 'REACT-PARSE', 'BUILD', 'BOOTSTRAP',
            'REWARDS-PAGE', 'RELOAD-BAD-PAGE', 'COOKIE-SYNC', 'CONTEXT-REFRESH',
            'CONTEXT-REFRESH-COOKIE-SEED', 'GET-CURRENT-POINTS', 'USERAGENT-EDGE-VERSION',
            'DISMISS-ALL-MESSAGES', 'HUMANIZE',
        ],
    },
    {'code': 'STREAK', 'label': '连击保护', 'order': 4, 'optional': True, 'tags': ['ENABLE-STREAK-PROTECTION']},
    {'code': 'PUNCHCARD', 'label': '打卡卡片', 'order': 5, 'optional': True, 'tags': ['PUNCHCARD']},
    {'code': 'SEARCH-PERK', 'label': '搜索特权', 'order': 6, 'optional': True, 'tags': ['ACTIVATE-SEARCH-PERK']},
    {'code': 'DAILY-SET', 'label': '每日任务集', 'order': 7, 'optional': True, 'tags': ['DAILY-SET']},
    {'code': 'MORE-PROMOTIONS', 'label': '更多推广', 'order': 8, 'optional': True, 'tags': ['MORE-PROMOTIONS']},
    {'code': 'APP-CHECKIN', 'label': 'App 签到', 'order': 9, 'optional': True, 'tags': ['DAILY-CHECK-IN']},
    {'code': 'APP-PROMOTIONS', 'label': 'App 推广', 'order': 10, 'optional': True, 'tags': ['APP-PROMOTIONS', 'APP-REWARD']},
    {'code': 'READ-TO-EARN', 'label': '阅读赚积分', 'order': 11, 'optional': True, 'tags': ['READ-TO-EARN']},
    {
        'code': 'SEARCH',
        'label': '搜索',
        'order': 12,
        'optional': True,
        'tags': [
            'SEARCH-BING', 'SEARCH-MANAGER', 'SEARCH-CHINA-TRENDS', 'SEARCH-BING-RELATED',
            'SEARCH-BING-SUGGESTIONS', 'SEARCH-RANDOM-SCROLL', 'SEARCH-COOKIE-SEED',
            'SEARCH-COOKIE-CAPTURE', 'SEARCH-CLOSE-TABS', 'QUERY-QUEUE', 'QUERY-CLUSTER',
            'QUERY-MANAGER', 'GHOST-CLICK',
        ],
    },
    {
        'code': 'CLAIM',
        'label': '领取奖励积分',
        'order': 13,
        'optional': True,
        'tags': ['CLAIM-BONUS-POINTS', 'URL-REWARD', 'ACTIVITY'],
    },
]

# 这些 TAG 描述的是「账号/整轮边界」或异常，不属于上面任何一个业务阶段。
BOUNDARY_TAGS = [
    'ACCOUNT-START', 'ACCOUNT-END', 'ACCOUNT-PARTIAL', 'ACCOUNT-DELAY',
    'EARN-SNAPSHOT', 'RUN-START', 'RUN-END', 'MAIN-ERROR',
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_created_epoch(pid: Optional[int]) -> Optional[float]:
    """进程创建时间（Unix epoch 秒）；拿不到返回 None。

    py 的进程被强杀时 ``_on_exit`` 没机会跑，``run-state.json`` 会留下僵尸 pid；
    而 pid 是会被复用的 —— 单看 ``pid_alive`` 会把「别的进程恰好拿到这个号」
    当成「本轮还在跑」。拿进程创建时间来区分。
    """
    if not pid or int(pid) <= 0 or os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exited),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            # FILETIME：自 1601-01-01 起的 100ns 计数
            return ticks / 1e7 - 11644473600.0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None

def pid_alive(pid: Optional[int]) -> bool:
    """进程是否还活着。

    **不要用 ``os.kill(pid, 0)`` 判存活，但它并不会杀进程** —— 这一点曾被误解
    （包括本文件早先的注释）。``signal.CTRL_C_EVENT == 0``，所以 Windows 上
    ``os.kill(pid, 0)`` 走的是 ``GenerateConsoleCtrlEvent(0, pid)``（广播 Ctrl+C），
    不是 ``TerminateProcess``；实测对 node 子进程调用后它照样活着。
    （CPython issue 14480 曾提议让 0 表示「探测存活」，被 rejected，理由正是
     0 已经是 CTRL_C_EVENT。）

    它真正坏在两个方向：
      ① **刚死的进程仍报存活** —— ``p.kill()`` 之后同一 pid 立刻探测不抛异常
         （句柄未回收），于是「已经跑完的一轮」被当成「还在跑」；
      ② **权限不足报死** —— 探测更高权限的进程抛 ``winerror=5``，
         被 ``except OSError`` 吞成「已死」。
    两个方向都会让调用方判断错，所以这里改用
    ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` + ``GetExitCodeProcess == 259``。
    pid 被复用的情况另由 ``process_created_epoch()`` 的创建时间校验兜住。
    """
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False

class MrsEngine:
    """网关调用入口。一个进程内持有一个实例即可。"""

    def __init__(
        self,
        root: str,
        *,
        node_exe: Optional[str] = None,
        log_buffer_size: int = 2000,
    ) -> None:
        self.root = os.path.abspath(root)
        self.config_path = os.path.join(self.root, 'config.json')
        self.example_path = os.path.join(self.root, 'config.example.json')
        self.env_path = os.path.join(self.root, '.env')
        self.state_path = os.path.join(self.root, STATE_NAME)

        self._node_hint = node_exe
        self._node_version_cache: Optional[str] = None
        self._node_version_checked = False

        self._lock = threading.RLock()
        self._runner: Optional[mr.BotRunner] = None
        self._logs: deque = deque(maxlen=max(200, int(log_buffer_size)))
        self._seq = 0

        # 心跳：M-5 —— 之前只在账号切换时写 run-state.json，一个账号要跑十几分钟，
        # 期间 updatedAt 不动，UI 会以为卡住了。这里每 10 秒至少刷一次。
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._last_state_write = 0.0
        self._state_lock = threading.Lock()

        # 日志落盘：网关每次访问 adapter.native 都会新建一个 MrsEngine，
        # 纯内存缓冲会让 logs() 永远返回空。这里把日志同时写到
        # logs/engine-log.jsonl，新实例或网关重启后能接着增量拉取。
        self.log_dir = os.path.join(self.root, 'logs')
        self.log_path = os.path.join(self.log_dir, 'engine-log.jsonl')
        self._log_loaded = False
        # 长驻句柄：每行 open/close 在高日志量下会把读取线程拖死（见 LOG_MAX_BYTES 注释）
        self._log_fh = None
        self._log_size = 0

        # 本次运行的进度（除运行中以外都保留最后一次的值，供终态展示）
        self._progress: Dict[str, Any] = {
            'scope': None,
            'currentAccount': None,
            'pointsGained': 0,
            'progress': None,
        }

    # ------------------------------------------------------------ 内部工具

    def _write_state(self, payload: Dict[str, Any]) -> None:
        """原子写 run-state.json（与 GUI 的 runner.py 同一套格式）。"""
        try:
            tmp = self.state_path + '.tmp'
            merged = dict(payload)
            merged.setdefault('updatedAt', _utc_now())
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    # -------------------------------------------------- 运行中状态刷新（M-5）

    def _write_running_state(self) -> None:
        """把当前进度写进 run-state.json（只有确实在跑时才写）。"""
        pid = self._running_pid()
        if not pid:
            return
        state = self._read_state()
        self._write_state(
            {
                'state': 'running',
                'pid': pid,
                'startedAt': state.get('startedAt'),
                'entry': 'dist/index.js',
                'cwd': self.root,
                **self._snapshot(),
            }
        )

    def _write_state_throttled(self, min_interval: float = 3.0) -> None:
        """日志驱动的高频更新：最多每 min_interval 秒落一次盘，避免逐行写文件。"""
        now = time.time()
        with self._state_lock:
            if now - self._last_state_write < min_interval:
                return
            self._last_state_write = now
        self._write_running_state()

    def _start_heartbeat(self, interval: float = 10.0) -> None:
        """每 interval 秒刷一次 updatedAt。

        M-5：单个账号可能要跑十几分钟，期间没有任何账号切换事件，若只在事件时写，
        ``updatedAt`` 会一直停在启动时刻，UI 看着像卡死。
        """
        self._stop_heartbeat()
        self._hb_stop = threading.Event()
        stop_event = self._hb_stop

        def loop() -> None:
            while not stop_event.wait(interval):
                try:
                    self._write_running_state()
                except Exception:
                    pass

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self._hb_thread = thread

    def _stop_heartbeat(self) -> None:
        event = self._hb_stop
        if event is not None:
            event.set()
        thread = self._hb_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._hb_thread = None

    def _read_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, encoding='utf-8') as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ------------------------------------------------------------ 日志持久化

    def _load_persisted_logs(self) -> None:
        """把上一次运行（或上一个实例）的日志尾部读回内存缓冲。

        网关每访问一次 ``native`` 就会 new 一个 MrsEngine，没有这一步的话
        ``logs()`` 永远是空的。只读文件尾部：崩过一次之后这文件可能是几百 MB，
        全量读会把网关卡住。
        """
        if self._log_loaded:
            return
        self._log_loaded = True
        try:
            size = os.path.getsize(self.log_path)
            with open(self.log_path, 'rb') as fh:
                if size > LOG_TAIL_READ_BYTES:
                    fh.seek(size - LOG_TAIL_READ_BYTES)
                    fh.readline()  # 丢掉可能被截断的首行
                raw = fh.read().decode('utf-8', errors='replace')
        except Exception:
            return
        tail = raw.splitlines()[-self._logs.maxlen :]
        with self._lock:
            for line in tail:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                seq = int(item.get('seq') or 0)
                self._logs.append(
                    {'seq': seq, 'level': item.get('level') or 'info', 'text': item.get('text') or ''}
                )
                self._seq = max(self._seq, seq)

    def _close_log_sink(self) -> None:
        fh = self._log_fh
        self._log_fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    def _persist_log(self, entry: Dict[str, Any]) -> None:
        """把一行日志追加到 logs/engine-log.jsonl。

        * ``debug`` 级别不落盘 —— 打开 debugLogs 时它占了 90% 以上的量，
          真正排查用到的还是 info 及以上；内存缓冲里仍然保留。
        * 长驻文件句柄，不逐行 open/close。
        * 超过 ``LOG_MAX_BYTES`` 轮转成 ``.1``，只留最近一份，避免无限增长。
        """
        if entry.get('level') == 'debug':
            return
        try:
            line = json.dumps(entry, ensure_ascii=False) + '\n'
            payload = line.encode('utf-8', errors='replace')
            if self._log_fh is None:
                os.makedirs(self.log_dir, exist_ok=True)
                self._log_fh = open(self.log_path, 'a', encoding='utf-8')
                try:
                    self._log_size = os.path.getsize(self.log_path)
                except OSError:
                    self._log_size = 0
            if self._log_size + len(payload) > LOG_MAX_BYTES:
                self._close_log_sink()
                rotated = self.log_path + '.1'
                try:
                    if os.path.exists(rotated):
                        os.remove(rotated)
                    os.replace(self.log_path, rotated)
                except Exception:
                    pass
                self._log_fh = open(self.log_path, 'w', encoding='utf-8')
                self._log_size = 0
            self._log_fh.write(line)
            self._log_size += len(payload)
            self._log_fh.flush()
        except Exception:
            # 磁盘满 / 权限问题：关掉句柄下次重开，绝不把异常抛给日志回调
            self._close_log_sink()

    def _reset_log_sink(self) -> None:
        """新一轮运行：清空内存缓冲、seq 归零、日志文件重建。

        ``start()`` 把 seq 归零是网关用来判断「新一轮」的信号
        （见 gateway/adapters/mrs.py 的 _last_max_seq），这里保持一致。
        """
        with self._lock:
            self._logs.clear()
            self._seq = 0
        self._log_loaded = True
        self._close_log_sink()
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path, 'w', encoding='utf-8'):
                pass
            self._log_size = 0
        except Exception:
            self._log_size = 0

    def _running_pid(self) -> Optional[int]:
        """当前在跑的是谁。

        以 ``run-state.json`` 为准，内存里的运行器只作补充：

        * M-3：网关每次访问 ``native`` 都会 new 一个 MrsEngine。上一轮的 node 进程
          没退干净时，旧实例的内存 runner 还「活着」，但真正在跑的是另一个 pid
          （网关 fallback 自己 Popen 的，或新一轮的）。旧实现优先返回内存 pid，
          于是 status() 报出上一轮的 pid，甚至把正在跑的一轮报成 idle。
        * 所以先看文件：文件说 running 且该 pid 存活 → 就是它（文件比内存新）。
        * 文件说没跑，但内存 runner 确实还活着（刚 start、状态还没落盘）→ 用内存的。
        """
        state = self._read_state()
        state_pid = state.get('pid')
        if state.get('state') == 'running' and pid_alive(state_pid):
            # 短路：文件说 running、却已经有 lastExit —— 这是矛盾状态
            # （上一轮结束的痕迹没被覆盖，或进程正在退出的窗口期）。
            # 正常运行时 _write_running_state 不带 lastExit，所以不会误伤。
            if not state.get('lastExit') and self._pid_matches_run(state_pid, state.get('startedAt')):
                return int(state_pid)

        with self._lock:
            runner = self._runner
        if runner is not None and runner.running:
            proc = runner.proc
            if proc is not None and pid_alive(proc.pid):
                return int(proc.pid)
        return None

    def _pid_matches_run(self, pid: Optional[int], started_at: Optional[str]) -> bool:
        """文件里那个 pid，是否真是「本轮」起的进程（防 pid 复用误判）。

        * 没记 ``startedAt`` 或拿不到进程创建时间 → 不判死（宁可误报在跑，也别放行重复启动）
        * ``startedAt`` 取自 Popen 之后，与进程真实创建时间的差 < 200ms
        * 双向窗口 5 秒：
          - 进程比 ``startedAt`` **早**太多 → 这个 pid 属于更早的进程
          - 进程比 ``startedAt`` **晚**太多 → 旧 pid 已死，号被系统分给了新进程
        """
        if not started_at:
            return True
        created = process_created_epoch(pid)
        if created is None:
            return True
        try:
            started = datetime.fromisoformat(str(started_at)).timestamp()
        except Exception:
            return True
        return abs(created - started) <= 5.0

    def _discard_stale_runner(self) -> None:
        """M-3 的配套：状态文件说没在跑，内存里却还挂着一个运行器 → 视为残留并清掉。

        否则「上一轮 node 进程不退」会让下一次 start() 直接抛「已有任务在运行」，
        网关只好 fallback 自己 Popen，两份进程同时写同一个 run-state.json。
        """
        with self._lock:
            stale = self._runner
            if stale is None:
                return
            self._runner = None
        # 关键：先摘掉旧轮次的回调，否则它退出时会写一份 idle/旧进度，
        # 把本轮的 running 状态覆盖掉。
        stale.on_line = None
        stale.on_exit = None
        if stale.running:
            try:
                stale.stop()
            except Exception:
                pass

    def _resolve_node(self) -> str:
        if self._node_hint and os.path.isfile(self._node_hint):
            return self._node_hint
        return mr.detect_node() or ''

    def _node_version(self) -> Optional[str]:
        if self._node_version_checked:
            return self._node_version_cache
        self._node_version_checked = True
        node = self._resolve_node()
        if not node:
            return None
        try:
            out = subprocess.run(
                [node, '--version'],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=mr._NO_WINDOW,  # noqa: SLF001 - 同包内复用
            )
            self._node_version_cache = (out.stdout or '').strip() or None
        except Exception:
            self._node_version_cache = None
        return self._node_version_cache

    def _snapshot(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            payload = dict(self._progress)
            payload.update(extra or {})
            return payload

    # ---------------------------------------------------------- 健康检查

    def health(self) -> Dict[str, Any]:
        """快速自检（<500ms）。除 node 版本首次探测外不启动任何进程。"""
        problems: List[str] = []

        node = self._resolve_node()
        node_available = bool(node)
        if not node_available:
            problems.append('未找到 node.exe（程序目录 / 运行时目录 / PATH 都没有）')

        dist_entry = os.path.join(self.root, 'dist', 'index.js')
        dist_exists = os.path.isfile(dist_entry)
        if not dist_exists:
            problems.append('dist/index.js 不存在，请先构建或补齐 dist 目录')

        config_exists = os.path.isfile(self.config_path)
        if not config_exists:
            problems.append('config.json 不存在')

        env_exists = os.path.isfile(self.env_path)
        if not env_exists:
            problems.append('.env 不存在，没有可运行的账号')

        try:
            accounts = ev.list_accounts(self.env_path)
        except Exception:
            accounts = []
        if env_exists and not accounts:
            problems.append('.env 里没有有效账号（EMAIL 为空或仍是示例占位）')

        # 浏览器：channel=chrome 用系统 Chrome；chromium 由 patchright 自带
        channel = 'chromium'
        try:
            cfg = cs.load(self.config_path, self.example_path)
            channel = str(cfg.get('browserChannel') or 'chromium')
        except Exception:
            pass
        chrome_path = mr.detect_system_chrome()
        if channel == 'chrome' and not chrome_path:
            problems.append('browserChannel=chrome 但系统未安装 Chrome')
        browser = {
            'channel': channel,
            'available': bool(chrome_path) if channel == 'chrome' else True,
            'path': chrome_path or '',
        }

        return {
            'nodeAvailable': node_available,
            'nodePath': node,
            'nodeVersion': self._node_version(),
            'distExists': dist_exists,
            'configExists': config_exists,
            'envExists': env_exists,
            'accountCount': len(accounts),
            'browser': browser,
            'problems': problems,
        }

    # ------------------------------------------------------------ 运行控制

    def start(self, scope: str = 'all') -> Dict[str, Any]:
        """启动一次运行。

        scope:
          'all'    跑 .env 里的全部账号（清掉父进程残留的 ACCOUNT_* 覆盖）
          'slot:N' 只跑第 N 个槽位（重映射到 ACCOUNT_1_*，见 env_store）
        """
        if self._running_pid():
            raise RuntimeError('已有任务在运行')

        # 状态文件说没在跑，但内存里可能还挂着上一轮的孤儿运行器（M-3/M-4）
        self._discard_stale_runner()

        slot: Optional[int] = None
        norm = (scope or 'all').strip().lower()
        if norm not in ('', 'all'):
            match = re.fullmatch(r'slot:(\d+)', norm)
            if not match:
                raise ValueError(f"无法识别的 scope: {scope!r}（应为 'all' 或 'slot:N'）")
            slot = int(match.group(1))

        accounts = ev.list_accounts(self.env_path)
        if not accounts:
            raise RuntimeError('.env 里没有可用账号')

        if slot is not None:
            if slot not in {int(a['slot']) for a in accounts}:
                raise ValueError(f'槽位 {slot} 不存在或已被注释')
            total = 1
            overrides = ev.child_env_for_slot(self.env_path, slot)
        else:
            total = len(accounts)
            # 跑全部：不传任何 ACCOUNT_* 覆盖（BotRunner 会先清空父进程残留）
            overrides = None

        node = self._resolve_node()
        if not node:
            raise RuntimeError('找不到 node.exe')

        with self._lock:
            self._progress = {
                'scope': 'all' if slot is None else f'slot:{slot}',
                'currentAccount': None,
                'currentTask': None,
                'pointsGained': 0,
                'progress': {'done': 0, 'total': total},
            }
        self._reset_log_sink()

        # 占位状态：此刻还没有 pid，也还没法给准 startedAt。
        # startedAt 故意留空而不是先取当前时间 —— 见下面 Popen 之后那处注释。
        self._write_state(
            {
                'state': 'running',
                'pid': None,
                'startedAt': None,
                'entry': 'dist/index.js',
                'nodeExe': node,
                'cwd': self.root,
                **self._snapshot(),
            }
        )

        runner = mr.BotRunner(write_state=False)
        runner.on_line = self._on_line
        runner.on_exit = self._on_exit
        runner.start(node, self.root, env_overrides=overrides)

        with self._lock:
            self._runner = runner
        pid = runner.proc.pid if runner.proc else None
        # startedAt 在 Popen 之后才取：这样它与进程真实创建时间几乎同一刻（差 < 200ms），
        # 消费方（网关）就能用「进程启动时间 ≈ startedAt」来判断 pid 有没有被系统复用。
        # 若在 Popen 之前取，startedAt 会系统性偏早（差值 = Popen 耗时），对面就得放宽容差。
        started_at = _utc_now()
        self._write_state(
            {
                'state': 'running',
                'pid': pid,
                'startedAt': started_at,
                'entry': 'dist/index.js',
                'nodeExe': node,
                'cwd': self.root,
                **self._snapshot(),
            }
        )
        self._start_heartbeat()
        return {'pid': pid, 'scope': self._snapshot().get('scope'), 'total': total}

    def stop(self, *, cleanup_browser: bool = True) -> None:
        """停整个进程树。

        BotRunner.stop() 走 taskkill /T /F；但 patchright 拉起的 Chrome 有时
        会脱离进程树，这里按 playwright 的临时目录特征再补一刀（只动
        playwright/patchright 自己的浏览器，不碰用户日常 Chrome）。
        """
        with self._lock:
            runner = self._runner

        self._stop_heartbeat()
        pid = self._running_pid()
        if runner is not None and runner.running:
            runner.stop()
        elif pid:
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True,
                    creationflags=mr._NO_WINDOW,  # noqa: SLF001
                )
            except Exception:
                pass

        if cleanup_browser:
            self._cleanup_playwright_browser()

        # 等一会儿确认真的没了，再落终态（runner 的 on_exit 也会写，二者一致）
        deadline = time.time() + 8
        while time.time() < deadline:
            if not self._running_pid():
                break
            time.sleep(0.2)

        state = self._read_state()
        if state.get('state') == 'running' and not self._running_pid():
            self._write_state(
                {
                    'state': 'idle',
                    'pid': None,
                    'startedAt': None,
                    'lastExit': {'code': None, 'at': _utc_now(), 'reason': 'stopped'},
                    **self._snapshot(),
                }
            )

    def _cleanup_playwright_browser(self) -> None:
        """只清理 patchright/playwright 启动的浏览器进程。"""
        if os.name != 'nt':
            return
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" "
            "| Where-Object { $_.CommandLine -and ($_.CommandLine -match 'ms-playwright|playwright|patchright') } "
            "| ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"
        )
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                capture_output=True,
                timeout=15,
                creationflags=mr._NO_WINDOW,  # noqa: SLF001
            )
        except Exception:
            pass

    def status(self) -> Dict[str, Any]:
        """读状态文件 + 校验进程存活，并合并本次会话的进度。"""
        state = self._read_state()
        alive_pid = self._running_pid()
        state_now = 'running' if alive_pid else ('idle' if state else 'unknown')

        merged: Dict[str, Any] = {
            'state': state_now,
            'pid': alive_pid if state_now == 'running' else None,
            'startedAt': state.get('startedAt'),
            'alive': bool(alive_pid),
            'source': 'mrs-engine',
        }
        if state.get('lastExit'):
            merged['lastExit'] = state['lastExit']

        # 内存进度（同进程）优先；网关重启后只剩状态文件里的快照
        with self._lock:
            snapshot = dict(self._progress)
        for key in ('scope', 'currentAccount', 'pointsGained', 'progress'):
            value = snapshot.get(key)
            if value is None:
                value = state.get(key)
            if value is not None:
                merged[key] = value
        return merged

    # ---------------------------------------------------------------- 日志

    def logs(self, after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        """增量拉取日志。seq 单调递增，两次调用传上次的 lastSeq 即可不重复。"""
        self._load_persisted_logs()
        with self._lock:
            items = [entry for entry in self._logs if entry['seq'] > int(after_seq)]
        return items[: max(1, int(limit))]

    def _on_line(self, text: str) -> None:
        """stdout 回调入口。任何解析/落盘异常都不许外泄。

        ``runner._pump`` 已经对单行回调做了兜底，这里是第二道保险 ——
        这一层出问题同样会连带整个任务，而一类日志解析失败没有理由杀任务。
        """
        try:
            self._handle_line(text)
        except Exception:
            pass

    def _handle_line(self, text: str) -> None:
        parsed = LOG_LINE_RE.match(text)
        # 级别取值：日志行自带的 [DEBUG]/[WARN]/[ERROR] 优先
        # （runner.classify 只认 error/warn/ok，会把 DEBUG 一律当 info，
        #   debugLogs 打开时这些行会把日志文件撑爆 —— 2026-11-23 撞过一次）
        declared = (parsed.group('level') or '').upper() if parsed else ''
        if declared == 'DEBUG':
            level = 'debug'
        elif declared == 'ERROR':
            level = 'error'
        elif declared == 'WARN':
            level = 'warn'
        else:
            level = mr.classify(text)

        with self._lock:
            self._seq += 1
            entry = {'seq': self._seq, 'level': level, 'text': text}
            self._logs.append(entry)
        self._persist_log(entry)

        if not parsed:
            return
        tag = (parsed.group('tag') or '').upper()
        msg = parsed.group('msg') or ''
        changed = False

        # M-5：每一行都刷新「当前在做什么」和实时余额，让 updatedAt 不再停在启动时刻。
        with self._lock:
            if tag and self._progress.get('currentTask') != tag:
                self._progress['currentTask'] = tag
            self._progress['lastLogAt'] = _utc_now()

        balance_hit = LIVE_BALANCE_RE.search(msg) or BALANCE_RE.search(msg)
        if balance_hit:
            try:
                live_balance = int(balance_hit.group(1))
            except (TypeError, ValueError):
                live_balance = None
            if live_balance is not None:
                with self._lock:
                    progress = dict(self._progress.get('progress') or {})
                    if progress.get('balance') != live_balance:
                        progress['balance'] = live_balance
                        self._progress['progress'] = progress

        if tag == 'ACCOUNT-START':
            match = ACCOUNT_START_RE.search(msg)
            if match:
                with self._lock:
                    self._progress['currentAccount'] = match.group(1)
                changed = True
        elif tag in ('ACCOUNT-END', 'ACCOUNT-SKIP'):
            gained = GAINED_RE.search(msg)
            with self._lock:
                if gained:
                    self._progress['pointsGained'] = int(self._progress.get('pointsGained') or 0) + int(
                        gained.group(1)
                    )
                progress = self._progress.get('progress') or {'done': 0, 'total': 0}
                progress = dict(progress)
                progress['done'] = int(progress.get('done') or 0) + 1
                self._progress['progress'] = progress
            changed = True
        elif tag == 'RUN-END':
            gained = GAINED_RE.search(msg)
            balance = BALANCE_RE.search(msg)
            done = ACCOUNTS_DONE_RE.search(msg)
            failed = FAILED_RE.search(msg)
            with self._lock:
                if gained:
                    self._progress['pointsGained'] = int(gained.group(1))
                progress = dict(self._progress.get('progress') or {'done': 0, 'total': 0})
                if done:
                    progress['total'] = int(done.group(1))
                    progress['done'] = int(done.group(1))
                if failed:
                    progress['failed'] = int(failed.group(1))
                if balance:
                    progress['balance'] = int(balance.group(1))
                self._progress['progress'] = progress
                self._progress['currentAccount'] = None
            changed = True

        if changed:
            # 账号级事件（进/出账号、跑完）：立即落盘
            with self._state_lock:
                self._last_state_write = time.time()
            self._write_running_state()
        else:
            # 普通业务行：节流落盘。
            # 不依赖 TAG 变化 —— 同一条 TAG 上停留很久（例如一直在 FLOW 等待）时，
            # currentTask/balance 可能都不变，但 lastLogAt 一直要动，
            # 它是「引擎还活着」最直接的信号。节流本身已经是廉价的时间比较。
            self._write_state_throttled(3.0)

    def _on_exit(self, code: int) -> None:
        self._stop_heartbeat()
        self._close_log_sink()
        self._write_state(
            {
                'state': 'idle',
                'pid': None,
                'startedAt': None,
                'lastExit': {'code': code, 'at': _utc_now()},
                **self._snapshot(),
            }
        )

    # ------------------------------------------------------------ 配置读写

    def get_config(self) -> Dict[str, Any]:
        return cs.load(self.config_path, self.example_path)

    def put_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """深合并写入（只覆盖传入的键，其余原样保留）。"""
        config = cs.load(self.config_path, self.example_path)
        config = cs._deep_merge(config, patch or {})  # noqa: SLF001 - 复用同一套合并规则
        cs.save(self.config_path, config)
        return config

    def get_simple(self) -> Dict[str, Any]:
        return cs.get_simple(self.get_config())

    def put_simple(self, simple: Dict[str, Any]) -> Dict[str, Any]:
        config = cs.load(self.config_path, self.example_path)
        cs.apply_simple(config, simple or {})
        cs.save(self.config_path, config)
        return cs.get_simple(config)

    # ------------------------------------------------------------ 账号读写

    def list_accounts(self) -> List[Dict[str, Any]]:
        """账号摘要（不含任何密钥明文）。"""
        return ev.list_accounts(self.env_path)

    def upsert_account(self, slot: Optional[int], data: Dict[str, Any]) -> Dict[str, Any]:
        """新增/编辑一个账号。

        * slot 为 None 时自动分配最小空闲槽位
        * 键可以用小写别名（email/password/totp_secret...），映射成 .env 大写字段
        * 未传的字段保持原值；传空串表示清空（会被注释掉）
        """
        mapped: Dict[str, Optional[str]] = {}
        for key, value in (data or {}).items():
            name = ACCOUNT_FIELD_ALIASES.get(str(key).lower()) or (
                str(key).upper() if str(key).upper() in ev.FIELD_ORDER else None
            )
            if not name:
                continue
            mapped[name] = None if value is None else str(value)

        if slot is None:
            slot = ev.next_free_slot(self.env_path)
        if not mapped:
            raise ValueError('没有可写入的账号字段')

        ev.upsert_account(self.env_path, int(slot), mapped)
        for account in ev.list_accounts(self.env_path):
            if int(account['slot']) == int(slot):
                return account
        return {'slot': int(slot)}

    def delete_account(self, slot: int) -> bool:
        """注释掉该槽位（不物理删除，可恢复）。"""
        return ev.delete_account(self.env_path, int(slot))

    # ------------------------------------------------------------ 元数据

    def presets(self) -> Dict[str, Any]:
        """把 worker 定义与档位定义交给 UI，避免 UI 硬编码中文。"""
        workers = [{'key': key, 'label': label, 'tip': tip} for key, label, tip in cs.WORKER_FIELDS]
        worker_presets = [
            {
                'name': name,
                'hint': cs.PRESET_HINTS.get(name, ''),
                'workers': cs.PRESET_WORKERS[name],
            }
            for name in cs.PRESET_ORDER
        ]
        delay_presets = [
            {'name': name, 'hint': cs.DELAY_HINTS.get(name, ''), 'values': cs.DELAY_PRESETS[name]}
            for name in cs.DELAY_ORDER
        ]
        return {
            'workers': workers,
            'workerPresets': worker_presets,
            'delayPresets': delay_presets,
            'delayOrder': list(cs.DELAY_ORDER),
            'presetOrder': list(cs.PRESET_ORDER),
            'accountFields': list(ev.FIELD_ORDER),
            'secretFields': sorted(ev.SECRET_FIELDS),
        }

    def stages(self) -> Dict[str, Any]:
        """一个账号跑一遍会经历的**固定、有序**业务阶段。

        只读元数据。UI 用它渲染「阶段」栏：未跑到的阶段显示灰色，
        这样用户一眼能看到总共几步、现在第几步。

        为什么不让 UI 从日志推断：日志推断出来的顺序是「出现顺序」，
        同一个 TAG 反复出现会变成多个条目，而且没跑到的阶段根本不会出现。

        返回结构：
          stages       有序阶段数组，每项 code/label/order/optional/tags
          boundaryTags 不属于任何阶段的边界/异常 TAG（账号起止、整轮起止、异常）
        """
        return {
            'stages': [dict(stage) for stage in STAGES],
            'boundaryTags': list(BOUNDARY_TAGS),
            'note': (
                '顺序取自 src/index.ts 的 Main()；optional=true 表示该阶段受配置开关或账号状态控制'
                '（如 doReadToEarn 关闭、或无 accessToken 时 App 类任务整组跳过）'
            ),
        }
