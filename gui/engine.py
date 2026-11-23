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

# 日志行固定格式：[时间] [账号] [LEVEL] 平台 [TAG] 正文
LOG_LINE_RE = re.compile(
    r'^\[[^\]]*\]\s*\[[^\]]*\]\s*\[(?P<level>INFO|WARN|ERROR|DEBUG)\]\s*'
    r'(?P<platform>DESKTOP|MOBILE|MAIN)\s*\[(?P<tag>[A-Za-z0-9_.-]+)\]\s*(?P<msg>.*)$'
)
ACCOUNT_START_RE = re.compile(r'开始处理账户:\s*([^\s|]+)')
GAINED_RE = re.compile(r'获得积分=(-?\d+)')
BALANCE_RE = re.compile(r'现余额=(\d+)')
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

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def pid_alive(pid: Optional[int]) -> bool:
    """进程是否还活着。

    Windows 上不能用 ``os.kill(pid, 0)``：CPython 在 Windows 会把非
    CTRL_C/CTRL_BREAK 的信号直接转成 TerminateProcess，等于把进程杀掉。
    这里走 OpenProcess + GetExitCodeProcess。
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

        # 日志落盘：网关每次访问 adapter.native 都会新建一个 MrsEngine，
        # 纯内存缓冲会让 logs() 永远返回空。这里把日志同时写到
        # logs/engine-log.jsonl，新实例或网关重启后能接着增量拉取。
        self.log_dir = os.path.join(self.root, 'logs')
        self.log_path = os.path.join(self.log_dir, 'engine-log.jsonl')
        self._log_loaded = False

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
        ``logs()`` 永远是空的。
        """
        if self._log_loaded:
            return
        self._log_loaded = True
        try:
            with open(self.log_path, encoding='utf-8', errors='replace') as fh:
                raw_lines = fh.readlines()
        except Exception:
            return
        tail = raw_lines[-self._logs.maxlen :]
        with self._lock:
            for raw in tail:
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                seq = int(item.get('seq') or 0)
                self._logs.append(
                    {'seq': seq, 'level': item.get('level') or 'info', 'text': item.get('text') or ''}
                )
                self._seq = max(self._seq, seq)

    def _persist_log(self, entry: Dict[str, Any]) -> None:
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception:
            pass

    def _reset_log_sink(self) -> None:
        """新一轮运行：清空内存缓冲、seq 归零、日志文件重建。

        ``start()`` 把 seq 归零是网关用来判断「新一轮」的信号
        （见 gateway/adapters/mrs.py 的 _last_max_seq），这里保持一致。
        """
        with self._lock:
            self._logs.clear()
            self._seq = 0
        self._log_loaded = True
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(self.log_path, 'w', encoding='utf-8'):
                pass
        except Exception:
            pass

    def _running_pid(self) -> Optional[int]:
        """当前是否在跑：优先内存里的运行器，其次状态文件 + 进程存活。"""
        with self._lock:
            runner = self._runner
        if runner is not None and runner.running:
            proc = runner.proc
            return int(proc.pid) if proc is not None else None
        state = self._read_state()
        if state.get('state') == 'running':
            pid = state.get('pid')
            if pid_alive(pid):
                return int(pid)
        return None

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
                'pointsGained': 0,
                'progress': {'done': 0, 'total': total},
            }
        self._reset_log_sink()

        started_at = _utc_now()
        self._write_state(
            {
                'state': 'running',
                'pid': None,  # 立即补写真实 pid
                'startedAt': started_at,
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
        return {'pid': pid, 'scope': self._snapshot().get('scope'), 'total': total}

    def stop(self, *, cleanup_browser: bool = True) -> None:
        """停整个进程树。

        BotRunner.stop() 走 taskkill /T /F；但 patchright 拉起的 Chrome 有时
        会脱离进程树，这里按 playwright 的临时目录特征再补一刀（只动
        playwright/patchright 自己的浏览器，不碰用户日常 Chrome）。
        """
        with self._lock:
            runner = self._runner

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
        level = mr.classify(text)
        with self._lock:
            self._seq += 1
            entry = {'seq': self._seq, 'level': level, 'text': text}
            self._logs.append(entry)
        self._persist_log(entry)

        parsed = LOG_LINE_RE.match(text)
        if not parsed:
            return
        tag = (parsed.group('tag') or '').upper()
        msg = parsed.group('msg') or ''
        changed = False

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
            self._write_state(
                {
                    'state': 'running' if self._running_pid() else 'idle',
                    'pid': self._running_pid(),
                    'startedAt': self._read_state().get('startedAt'),
                    **self._snapshot(),
                }
            )

    def _on_exit(self, code: int) -> None:
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
