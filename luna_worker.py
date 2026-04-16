from __future__ import annotations

import os
import sys
import time
import re
import subprocess
import threading
import json
import queue
from pathlib import Path
from typing import Dict, Tuple, Optional

import ctypes
from ctypes import wintypes, c_bool, c_int, c_uint32, c_uint64, c_uint8, c_void_p, c_wchar_p, c_char_p, c_float

from PySide6 import QtCore


if not sys.platform.startswith("win32"):
    raise RuntimeError("Luna hook backend is Windows-only.")

import win32process


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400


def _open_process(pid: int):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    return handle


def _close_handle(handle) -> None:
    if not handle:
        return
    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)


def _is_process_64(pid: int) -> Optional[bool]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = _open_process(pid)
    if not handle:
        return None
    try:
        is_wow64_process2 = getattr(kernel32, "IsWow64Process2", None)
        if is_wow64_process2:
            process_machine = wintypes.USHORT()
            native_machine = wintypes.USHORT()
            ok = is_wow64_process2(handle, ctypes.byref(process_machine), ctypes.byref(native_machine))
            if not ok:
                return None
            # process_machine == 0 means not WOW64 (same arch as OS)
            if native_machine.value == 0:
                return False
            return process_machine.value == 0
        is_wow64 = wintypes.BOOL()
        ok = kernel32.IsWow64Process(handle, ctypes.byref(is_wow64))
        if not ok:
            return None
        if sys.maxsize <= 2**32:
            return False
        return not bool(is_wow64.value)
    finally:
        _close_handle(handle)


def _default_luna_root() -> Path:
    base = Path(__file__).resolve().parent
    return base / "LunaTranslator_x64_win10"


def _find_python32() -> Optional[str]:
    env_path = os.environ.get("PYTHON32_EXE") or os.environ.get("PYTHON32")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return str(p)
    # Try py launcher
    for args in (
        ["py", "-3-32", "-c", "import sys;print(sys.executable)"],
        ["py", "-32", "-c", "import sys;print(sys.executable)"],
    ):
        try:
            res = subprocess.run(args, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                out = (res.stdout or "").strip().splitlines()
                if out:
                    candidate = Path(out[-1].strip())
                    if candidate.exists():
                        return str(candidate)
        except Exception:
            pass
    # Common install locations
    versions = ["313", "312", "311", "310", "39", "38", "37"]
    bases = [
        "C:\\Python{ver}-32",
        "C:\\Python{ver}",
        "C:\\Program Files (x86)\\Python{ver}-32",
        "C:\\Program Files (x86)\\Python{ver}",
    ]
    for ver in versions:
        for base in bases:
            cand = Path(base.format(ver=ver)) / "python.exe"
            if cand.exists():
                return str(cand)
    return None


def _find_luna_root(target_bit: str) -> Path:
    env_root = os.environ.get("LUNA_TRANSLATOR_DIR")
    if env_root:
        return Path(env_root).expanduser()

    base = Path(__file__).resolve().parent
    candidates = []
    if target_bit == "64":
        candidates = [
            base / "LunaTranslator_x64_win10",
        ]

    for cand in candidates:
        if cand.exists():
            return cand
    return _default_luna_root()


class ThreadParam(ctypes.Structure):
    _fields_ = [
        ("processId", c_uint32),
        ("addr", c_uint64),
        ("ctx", c_uint64),
        ("ctx2", c_uint64),
    ]


ProcessEvent = ctypes.CFUNCTYPE(None, wintypes.DWORD)
ThreadEventMaybeEmbed = ctypes.CFUNCTYPE(None, c_wchar_p, c_char_p, ThreadParam, c_bool)
ThreadEvent = ctypes.CFUNCTYPE(None, c_wchar_p, c_char_p, ThreadParam)
OutputCallback = ctypes.CFUNCTYPE(None, c_wchar_p, c_char_p, ThreadParam, c_wchar_p)
HostInfoHandler = ctypes.CFUNCTYPE(None, c_int, c_wchar_p)
HookInsertHandler = ctypes.CFUNCTYPE(None, wintypes.DWORD, c_uint64, c_wchar_p)
EmbedCallback = ctypes.CFUNCTYPE(None, c_wchar_p, ThreadParam)
I18NQueryCallback = ctypes.CFUNCTYPE(c_void_p, c_wchar_p)


class LunaHookWorker(QtCore.QThread):
    text_ready = QtCore.Signal(str)
    status = QtCore.Signal(str)
    embed_text_requested = QtCore.Signal(str, str)

    def __init__(
        self,
        hwnd: int,
        *,
        codepage: int = 932,
        text_thread_delay: int = 500,
        max_buffer_size: int = 3000,
        max_history_size: int = 1000000,
        auto_pc_hooks: bool = True,
        flush_delay_ms: int = 120,
        enable_embed: bool = False,
        parent: Optional[QtCore.QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.hwnd = hwnd
        self._running = False
        self._pid: Optional[int] = None
        self._codepage = int(codepage)
        self._text_thread_delay = int(text_thread_delay)
        self._max_buffer_size = int(max_buffer_size)
        self._max_history_size = int(max_history_size)
        self._auto_pc_hooks = bool(auto_pc_hooks)
        self._flush_delay = max(10, int(flush_delay_ms)) / 1000.0
        self._enable_embed = bool(enable_embed)
        self._embed_timeout_ms = int(os.environ.get("LUNA_EMBED_TIMEOUT_MS", "12000"))
        self._embed_max_line = int(os.environ.get("LUNA_EMBED_MAX_LINE", "42"))

        self._pending: Dict[Tuple[int, int, int, int], Tuple[str, float]] = {}
        self._last_emitted: Dict[Tuple[int, int, int, int], str] = {}
        self._pending_lock = threading.Lock()
        self._sync_queue: "queue.Queue[tuple[ThreadParam, bool]]" = queue.Queue()
        self._synced_keys: set[Tuple[int, int, int, int]] = set()

        self._embed_pending_lock = threading.Lock()
        self._embed_pending: Dict[str, Tuple[ThreadParam, str]] = {}
        self._embed_submit_queue: "queue.Queue[tuple[ThreadParam, str, str]]" = queue.Queue()
        self._embed_seq = 0
        self._embed_enabled_keys: set[Tuple[int, int, int, int]] = set()
        self._embed_hook_addrs: Dict[int, set[int]] = {}
        self._embed_primary_addr: Dict[int, int] = {}
        self._ctx_pairs_by_pid: Dict[int, set[Tuple[int, int]]] = {}
        self._ctx_pairs_by_pid_code: Dict[Tuple[int, str], set[Tuple[int, int]]] = {}

        self._luna = None
        self._callbacks = []
        self._luna_paths = {}
        self._helper_proc = None
        self._helper_queue: "queue.Queue[str]" = queue.Queue()
        self._helper_reader = None
        self._helper_mode = False
        self._target_bit: Optional[str] = None
        self._safe_mode = os.environ.get("LUNA_SAFE_MODE") == "1"
        self._try_elevated_inject = os.environ.get("LUNA_TRY_ELEVATE") == "1"
        env_auto_pc_hooks = os.environ.get("LUNA_AUTO_PC_HOOKS")
        if env_auto_pc_hooks is not None:
            self._auto_pc_hooks = env_auto_pc_hooks == "1"
        if self._safe_mode:
            self._auto_pc_hooks = False

    def run(self) -> None:
        self._running = True
        try:
            pid = self._resolve_pid()
            if pid is None:
                return
            self._pid = pid
            target_bit = self._prepare_luna(pid)
            if not target_bit:
                return
            self._target_bit = target_bit
            if target_bit == "32" and sys.maxsize > 2**32:
                use_helper_env = os.environ.get("LUNA_USE_HELPER32")
                if use_helper_env == "0":
                    self.status.emit("Target is 32-bit. 64-bit runtime requires helper. Set LUNA_USE_HELPER32=1.")
                    return
                python32 = _find_python32()
                if not python32:
                    self.status.emit("Target is 32-bit. Set PYTHON32_EXE to a 32-bit Python path.")
                    self.status.emit("Example: PYTHON32_EXE=C:\\Python310-32\\python.exe")
                    return
                self.status.emit("Auto enabled 32-bit helper for 32-bit target.")
                if not self._start_helper32(pid, target_bit, python32=python32):
                    return
                self._helper_mode = True
                self.status.emit(f"Luna 32-bit helper attached (PID {pid}). Waiting for text...")
                self._run_helper_loop()
                return
            if not self._init_paths(target_bit):
                return
            self._start_luna(pid, target_bit)
            self.status.emit(f"Luna hook attached (PID {pid}). Waiting for text...")
            while self._running:
                self._flush_pending()
                self._flush_sync_queue()
                self._flush_embed_queue()
                self.msleep(50)
        except Exception as e:
            self.status.emit(f"Luna hook error: {e}")
        finally:
            self._stop_helper()
            self._detach()

    def stop(self) -> None:
        self._running = False
        self._stop_helper()
        try:
            self.wait(3000)
        except Exception:
            pass

    def _init_paths(self, target_bit: str) -> bool:
        runtime_bit = "64" if sys.maxsize > 2**32 else "32"
        luna_root = _find_luna_root(runtime_bit)
        files = luna_root / "files"
        hook_dir = files / "LunaHook"
        host = hook_dir / f"LunaHost{runtime_bit}.dll"
        hook_target = hook_dir / f"LunaHook{target_bit}.dll"
        proxy_target = files / f"shareddllproxy{target_bit}.exe"

        missing = []
        if not host.exists():
            missing.append(str(host))
        if not hook_target.exists():
            missing.append(str(hook_target))
        if not proxy_target.exists():
            missing.append(str(proxy_target))
        if missing:
            self.status.emit(
                "Missing LunaHook files. Expected: {}".format(
                    "; ".join(missing)
                )
            )
            return False

        self._luna_paths = {
            "root": luna_root,
            "files": files,
            "hook_dir": hook_dir,
            "host": host,
            "hook": {target_bit: hook_target},
            "proxy": {target_bit: proxy_target},
            "runtime_bit": runtime_bit,
        }
        return True

    def _resolve_pid(self) -> Optional[int]:
        try:
            _, pid = win32process.GetWindowThreadProcessId(self.hwnd)
        except Exception:
            pid = None
        if not pid:
            self.status.emit("Failed to resolve PID from window handle.")
            return None
        return pid

    def _prepare_luna(self, pid: int) -> Optional[str]:
        is64 = _is_process_64(pid)
        if is64 is None:
            self.status.emit("Cannot determine target process architecture.")
            return None
        target_bit = "64" if is64 else "32"
        return target_bit

    def _start_helper32(self, pid: int, target_bit: str, *, python32: Optional[str] = None) -> bool:
        python32 = python32 or _find_python32()
        if not python32:
            self.status.emit("Target is 32-bit. Set PYTHON32_EXE to a 32-bit Python path.")
            self.status.emit("Example: PYTHON32_EXE=C:\\Python310-32\\python.exe")
            return False
        helper_path = Path(__file__).resolve().parent / "luna_helper32.py"
        if not helper_path.exists():
            self.status.emit(f"Missing helper: {helper_path}")
            return False
        cmd = [
            python32,
            "-u",
            str(helper_path),
            "--pid",
            str(pid),
            "--codepage",
            str(self._codepage),
            "--text-thread-delay",
            str(self._text_thread_delay),
            "--max-buffer-size",
            str(self._max_buffer_size),
            "--max-history-size",
            str(self._max_history_size),
            "--flush-delay-ms",
            str(int(self._flush_delay * 1000)),
        ]
        if self._enable_embed:
            cmd.append("--enable-embed")
        if self._auto_pc_hooks:
            cmd.append("--auto-pc-hooks")
        env = os.environ.copy()
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._helper_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._helper_reader = threading.Thread(target=self._read_helper_stdout, daemon=True)
        self._helper_reader.start()
        return True

    def _read_helper_stdout(self) -> None:
        if not self._helper_proc or not self._helper_proc.stdout:
            return
        for line in self._helper_proc.stdout:
            line = line.strip()
            if not line:
                continue
            self._helper_queue.put(line)

    def _run_helper_loop(self) -> None:
        while self._running:
            try:
                while True:
                    line = self._helper_queue.get_nowait()
                    self._handle_helper_line(line)
            except queue.Empty:
                pass
            if self._helper_proc and self._helper_proc.poll() is not None:
                code = self._helper_proc.returncode
                self.status.emit(f"Luna helper exited with code {code}")
                break
            self.msleep(50)

    def _handle_helper_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except Exception:
            self.status.emit(line)
            return
        mtype = payload.get("type")
        if mtype == "text":
            text = payload.get("text") or ""
            if text:
                self.text_ready.emit(text)
            return
        if mtype == "status":
            msg = payload.get("message") or ""
            if msg:
                self.status.emit(msg)
            return
        if mtype == "embed_request":
            request_id = payload.get("request_id") or ""
            text = payload.get("text") or ""
            if request_id and text:
                self.embed_text_requested.emit(request_id, text)
            return
        if mtype == "debug":
            try:
                self.status.emit("[helper-debug] " + json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.status.emit(str(payload))
            return
        self.status.emit(line)

    def submit_embed_translation(self, request_id: str, translation: str) -> None:
        if not request_id:
            return
        if self._helper_mode:
            proc = self._helper_proc
            if not proc or not proc.stdin:
                return
            payload = {
                "type": "embed_result",
                "request_id": request_id,
                "translation": translation or "",
            }
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception:
                pass
            return
        with self._embed_pending_lock:
            pending = self._embed_pending.pop(request_id, None)
        if not pending:
            return
        tp, src_text = pending
        self._embed_submit_queue.put((tp, src_text, translation or ""))

    def _stop_helper(self) -> None:
        proc = self._helper_proc
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        self._helper_proc = None
        self._helper_reader = None

    def _start_luna(self, pid: int, target_bit: str) -> None:
        self.status.emit("Luna: loading host dll...")
        self._luna = ctypes.CDLL(str(self._luna_paths["host"]))

        self._luna.Luna_SyncThread.argtypes = (ThreadParam, c_bool)
        self._luna.Luna_InsertPCHooks.argtypes = (wintypes.DWORD, c_int)
        self._luna.Luna_Settings.argtypes = (c_int, c_bool, c_int, c_int, c_int)
        self._luna.Luna_Start.argtypes = (
            ProcessEvent,
            ProcessEvent,
            ThreadEventMaybeEmbed,
            ThreadEvent,
            OutputCallback,
            HostInfoHandler,
            HookInsertHandler,
            EmbedCallback,
            I18NQueryCallback,
        )
        self._luna.Luna_ConnectProcess.argtypes = (wintypes.DWORD,)
        self._luna.Luna_CheckIfNeedInject.argtypes = (wintypes.DWORD,)
        self._luna.Luna_CheckIfNeedInject.restype = c_bool
        self._luna.Luna_DetachProcess.argtypes = (wintypes.DWORD,)
        self._luna.Luna_ResetLang.argtypes = ()
        if hasattr(self._luna, "Luna_EmbedSettings"):
            self._luna.Luna_EmbedSettings.argtypes = (
                wintypes.DWORD,
                c_uint32,
                c_uint8,
                c_bool,
                c_wchar_p,
                c_uint32,
                c_bool,
                c_bool,
                c_bool,
                c_float,
            )
        if hasattr(self._luna, "Luna_UseEmbed"):
            self._luna.Luna_UseEmbed.argtypes = (ThreadParam, c_bool)
        if hasattr(self._luna, "Luna_CheckIsUsingEmbed"):
            self._luna.Luna_CheckIsUsingEmbed.argtypes = (ThreadParam,)
            self._luna.Luna_CheckIsUsingEmbed.restype = c_bool
        if hasattr(self._luna, "Luna_EmbedCallback"):
            self._luna.Luna_EmbedCallback.argtypes = (ThreadParam, c_wchar_p, c_wchar_p)
        if hasattr(self._luna, "Luna_AllocString"):
            self._luna.Luna_AllocString.argtypes = (c_wchar_p,)
            self._luna.Luna_AllocString.restype = c_void_p

        cb_proc_connect = ProcessEvent(self._on_proc_connect)
        cb_proc_remove = ProcessEvent(self._on_proc_remove)
        cb_new_hook = ThreadEventMaybeEmbed(self._on_new_hook)
        cb_remove_hook = ThreadEvent(self._on_remove_hook)
        cb_output = OutputCallback(self._on_output)
        cb_host_info = None if self._safe_mode else HostInfoHandler(self._on_host_info)
        cb_hook_insert = None if self._safe_mode else HookInsertHandler(self._on_hook_insert)
        cb_embed = None if self._safe_mode else EmbedCallback(self._on_embed)
        cb_i18n = None if self._safe_mode else I18NQueryCallback(self._on_i18n_query)
        self._callbacks = [
            cb_proc_connect,
            cb_proc_remove,
            cb_new_hook,
            cb_remove_hook,
            cb_output,
            cb_host_info,
            cb_hook_insert,
            cb_embed,
            cb_i18n,
        ]

        self.status.emit("Luna: Luna_Start...")
        self._luna.Luna_Start(*self._callbacks)
        self.status.emit("Luna: Luna_Settings...")
        self._luna.Luna_Settings(
            self._text_thread_delay,
            False,
            self._codepage,
            self._max_buffer_size,
            self._max_history_size,
        )
        if os.environ.get("LUNA_RESET_LANG") == "1":
            self.status.emit("Luna: Luna_ResetLang...")
            self._luna.Luna_ResetLang()
        else:
            self.status.emit("Luna: skip ResetLang (set LUNA_RESET_LANG=1 to enable)")

        self.status.emit("Luna: Luna_ConnectProcess...")
        self._luna.Luna_ConnectProcess(pid)
        self._apply_embed_settings(pid)
        self.status.emit("Luna: Luna_CheckIfNeedInject...")
        need_inject = bool(self._luna.Luna_CheckIfNeedInject(pid))
        if need_inject:
            self._inject(pid, target_bit)

        # Robust reconnect path:
        # - If target was already injected from a previous tool run, callbacks/hooks may not be fully re-bound.
        # - If we injected just now, reconnect ensures the host observes active hooks consistently.
        try:
            self.status.emit("Luna: reconnecting process for hook activation...")
            self._luna.Luna_ConnectProcess(pid)
            self._apply_embed_settings(pid)
        except Exception:
            pass

        if self._auto_pc_hooks and (not self._safe_mode):
            try:
                self._luna.Luna_InsertPCHooks(pid, 0)
                self._luna.Luna_InsertPCHooks(pid, 1)
            except Exception:
                pass

    def _apply_embed_settings(self, pid: int) -> None:
        if not self._enable_embed:
            return
        if not self._luna or not hasattr(self._luna, "Luna_EmbedSettings"):
            return
        try:
            self._luna.Luna_EmbedSettings(
                pid,
                max(500, self._embed_timeout_ms),
                2,
                False,
                "",
                0,
                True,
                False,
                False,
                0.0,
            )
            self.status.emit("Embed translation enabled.")
        except Exception as e:
            self.status.emit(f"Failed to apply embed settings: {e}")

    def _inject(self, pid: int, target_bit: str) -> None:
        proxy = str(self._luna_paths["proxy"][target_bit])
        hook = str(self._luna_paths["hook"][target_bit])
        try:
            result = subprocess.run(
                [proxy, "dllinject", str(pid), hook],
                check=False,
                capture_output=True,
                text=True,
            )
            ret = result.returncode
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if out:
                self.status.emit(out)
            if err:
                self.status.emit(err)
            if ret == 0:
                self.status.emit("Injected LunaHook DLL.")
                return
            if os.environ.get("LUNA_NO_ELEVATE") == "1":
                self.status.emit("DLL injection failed, skipping elevation (LUNA_NO_ELEVATE=1).")
                return
            if not self._try_elevated_inject:
                self.status.emit("DLL injection failed. Skipping elevated injection by default (set LUNA_TRY_ELEVATE=1 to enable).")
                return
            self.status.emit("DLL injection failed, trying elevated injection...")
            ctypes.windll.shell32.ShellExecuteW(None, "runas", proxy, f'dllinject {pid} "{hook}"', None, 0)
            # Wait briefly and re-check injection status
            for _ in range(25):
                time.sleep(0.2)
                try:
                    luna = self._luna
                    if luna and (not luna.Luna_CheckIfNeedInject(pid)):
                        self.status.emit("Injected LunaHook DLL (elevated).")
                        return
                except Exception:
                    break
        except Exception as e:
            self.status.emit(f"DLL injection error: {e}")

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
        return text.strip()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        now = time.time()
        emit_list = []
        with self._pending_lock:
            for key, (text, ts) in list(self._pending.items()):
                if now - ts < self._flush_delay:
                    continue
                last = self._last_emitted.get(key)
                if text and text != last:
                    self._last_emitted[key] = text
                    emit_list.append(text)
                self._pending.pop(key, None)
        for text in emit_list:
            self.text_ready.emit(text)

    def _flush_sync_queue(self) -> None:
        if not self._luna:
            return
        try:
            while True:
                tp, is_embedable = self._sync_queue.get_nowait()
                key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
                if key in self._synced_keys:
                    continue
                self._synced_keys.add(key)
                try:
                    self._luna.Luna_SyncThread(tp, True)
                except Exception:
                    pass
                if self._enable_embed and is_embedable and hasattr(self._luna, "Luna_UseEmbed"):
                    try:
                        self._luna.Luna_UseEmbed(tp, True)
                    except Exception:
                        pass
        except queue.Empty:
            return

    def _flush_embed_queue(self) -> None:
        if not self._luna or not hasattr(self._luna, "Luna_EmbedCallback"):
            return
        try:
            while True:
                tp, src_text, trans = self._embed_submit_queue.get_nowait()
                try:
                    trans = self._split_embed_lines(trans)
                    src_raw = src_text or ""
                    src_clean = self._clean_text(src_raw)
                    trans_send = trans or ""
                    pid_i = int(tp.processId)
                    embed_addr = self._embed_primary_addr.get(pid_i)

                    targets = []
                    if embed_addr:
                        tp_embed = ThreadParam()
                        tp_embed.processId = int(tp.processId)
                        tp_embed.addr = int(embed_addr)
                        tp_embed.ctx = int(tp.ctx)
                        tp_embed.ctx2 = int(tp.ctx2)
                        targets.append(tp_embed)
                    targets.append(tp)

                    seen = set()
                    for target in targets:
                        k = (int(target.processId), int(target.addr), int(target.ctx), int(target.ctx2))
                        if k in seen:
                            continue
                        seen.add(k)
                        try:
                            self._luna.Luna_EmbedCallback(target, src_raw, trans_send)
                        except Exception:
                            pass
                        if src_clean and src_clean != src_raw:
                            try:
                                self._luna.Luna_EmbedCallback(target, src_clean, trans_send)
                            except Exception:
                                pass

                    if targets:
                        try:
                            self._luna.Luna_EmbedCallback(targets[0], "", trans_send)
                        except Exception:
                            pass
                except Exception:
                    pass
        except queue.Empty:
            return

    def _split_embed_lines(self, text: str) -> str:
        if not text:
            return ""
        limit = self._embed_max_line
        if limit <= 0:
            return text
        chunks = []
        for line in str(text).split("\n"):
            if not line:
                chunks.append("")
                continue
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            chunks.append(line)
        return "\n".join(chunks)

    def _detach(self) -> None:
        if not self._luna or self._pid is None:
            return
        try:
            self._luna.Luna_DetachProcess(self._pid)
        except Exception:
            pass

    # Callbacks
    def _on_proc_connect(self, pid):
        luna = self._luna
        if self._auto_pc_hooks and (not self._safe_mode) and luna:
            try:
                luna.Luna_InsertPCHooks(pid, 0)
                luna.Luna_InsertPCHooks(pid, 1)
            except Exception:
                pass
            self._apply_embed_settings(pid)
        self.status.emit(f"Process connected: {pid}")

    def _on_proc_remove(self, pid):
        self.status.emit(f"Process removed: {pid}")

    def _on_new_hook(self, hc, hn, tp, isembedable):
        # Defer sync to avoid re-entrant calls inside native callback.
        try:
            tp_copy = ThreadParam()
            tp_copy.processId = int(tp.processId)
            tp_copy.addr = int(tp.addr)
            tp_copy.ctx = int(tp.ctx)
            tp_copy.ctx2 = int(tp.ctx2)
            pid_i = int(tp_copy.processId)
            ctx_pair = (int(tp_copy.ctx), int(tp_copy.ctx2))
            self._ctx_pairs_by_pid.setdefault(pid_i, set()).add(ctx_pair)
            hc_text = ""
            hn_text = ""
            try:
                hc_text = str(hc or "")
            except Exception:
                hc_text = ""
            try:
                if isinstance(hn, (bytes, bytearray)):
                    hn_text = hn.decode("utf-8", errors="ignore")
                else:
                    hn_text = str(hn or "")
            except Exception:
                hn_text = ""
            hc_low = hc_text.lower()
            hn_low = hn_text.lower()
            force_embed = ("embed" in hc_low) or ("embed" in hn_low)
            force_embed = force_embed or ("qlie" in hc_low) or ("qlie" in hn_low)
            force_embed = force_embed or (int(tp_copy.addr) in self._embed_hook_addrs.get(int(tp_copy.processId), set()))
            if force_embed:
                for code_key in {hc_text.strip().lower(), hn_text.strip().lower()}:
                    if code_key:
                        self._ctx_pairs_by_pid_code.setdefault((pid_i, code_key), set()).add(ctx_pair)
                self.status.emit(
                    f"Embed candidate hook: pid={int(tp_copy.processId)} addr=0x{int(tp_copy.addr):X} hc={hc_text}"
                )
            self._sync_queue.put((tp_copy, bool(isembedable) or force_embed))
        except Exception:
            pass

    def _on_remove_hook(self, hc, hn, tp):
        return

    def _on_output(self, hc, hn, tp, output):
        if not self._running:
            return
        try:
            text = self._clean_text(output)
            if not text:
                return
            raw_text = "" if output is None else str(output)
            key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
            with self._pending_lock:
                self._pending[key] = (text, time.time())
            embed_addrs = self._embed_hook_addrs.get(int(tp.processId), set())
            hc_low = ""
            hn_low = ""
            try:
                hc_low = str(hc or "").lower()
            except Exception:
                hc_low = ""
            try:
                if isinstance(hn, (bytes, bytearray)):
                    hn_low = hn.decode("utf-8", errors="ignore").lower()
                else:
                    hn_low = str(hn or "").lower()
            except Exception:
                hn_low = ""
            is_qlie_output = ("qlie" in hc_low) or ("qlie" in hn_low)
            luna = self._luna
            if (
                self._enable_embed
                and (int(tp.addr) in embed_addrs or is_qlie_output)
                and luna
                and hasattr(luna, "Luna_UseEmbed")
                and key not in self._embed_enabled_keys
            ):
                try:
                    tp_enable = ThreadParam()
                    tp_enable.processId = int(tp.processId)
                    tp_enable.addr = int(tp.addr)
                    tp_enable.ctx = int(tp.ctx)
                    tp_enable.ctx2 = int(tp.ctx2)
                    luna.Luna_UseEmbed(tp_enable, True)
                    self._embed_enabled_keys.add(key)
                    using = None
                    if hasattr(luna, "Luna_CheckIsUsingEmbed"):
                        try:
                            using = bool(luna.Luna_CheckIsUsingEmbed(tp_enable))
                        except Exception:
                            using = None
                    if using is None:
                        self.status.emit(f"Embed enabled on output thread: {key}")
                    else:
                        self.status.emit(f"Embed enabled on output thread: {key} using={using}")
                except Exception:
                    pass
            if self._enable_embed and (int(tp.addr) in embed_addrs or is_qlie_output):
                tp_copy = ThreadParam()
                tp_copy.processId = int(tp.processId)
                tp_copy.addr = int(tp.addr)
                tp_copy.ctx = int(tp.ctx)
                tp_copy.ctx2 = int(tp.ctx2)
                self._embed_seq += 1
                request_id = "out-{}-{}-{}-{}-{}".format(
                    int(tp_copy.processId),
                    int(tp_copy.addr),
                    int(tp_copy.ctx),
                    int(tp_copy.ctx2),
                    self._embed_seq,
                )
                with self._embed_pending_lock:
                    self._embed_pending[request_id] = (tp_copy, raw_text)
                self.embed_text_requested.emit(request_id, text)
        except Exception:
            return

    def _on_host_info(self, code, msg):
        if msg:
            m = str(msg)
            try:
                low = m.lower()
                if "embedqlie" in low and self._pid:
                    hit = re.search(r"([0-9a-fA-F]{6,16})", m)
                    if hit:
                        addr_i = int(hit.group(1), 16)
                        pid_i = int(self._pid)
                        self._embed_primary_addr[pid_i] = addr_i
                        self._embed_hook_addrs.setdefault(pid_i, set()).add(addr_i)
                        self.status.emit(f"Embed primary addr bound from host info: pid={pid_i} addr=0x{addr_i:X}")
            except Exception:
                pass
            self.status.emit(m)

    def _on_hook_insert(self, pid, addr, hcode):
        try:
            hcode_text = str(hcode or "")
            hcode_low = hcode_text.lower()
            if ("embed" in hcode_low) or ("qlie" in hcode_low):
                pid_i = int(pid)
                addr_i = int(addr)
                addrs = self._embed_hook_addrs.setdefault(pid_i, set())
                addrs.add(addr_i)
                if ("embedqlie" in hcode_low) or (pid_i not in self._embed_primary_addr):
                    self._embed_primary_addr[pid_i] = addr_i
                self.status.emit(
                    f"Embed/QLIE hook detected: pid={pid_i} addr=0x{addr_i:X} code={hcode_text}"
                )
                luna = self._luna
                if self._enable_embed and luna and hasattr(luna, "Luna_UseEmbed"):
                    code_key = hcode_text.strip().lower()
                    ctx_pairs = set(self._ctx_pairs_by_pid_code.get((pid_i, code_key), set()))
                    if not ctx_pairs:
                        ctx_pairs = set(self._ctx_pairs_by_pid.get(pid_i, set()))
                    used = 0
                    for ctx, ctx2 in list(ctx_pairs)[:256]:
                        try:
                            tp_bind = ThreadParam()
                            tp_bind.processId = pid_i
                            tp_bind.addr = addr_i
                            tp_bind.ctx = int(ctx)
                            tp_bind.ctx2 = int(ctx2)
                            luna.Luna_UseEmbed(tp_bind, True)
                            used += 1
                        except Exception:
                            pass
                    if used:
                        self.status.emit(
                            f"Embed bound contexts: pid={pid_i} addr=0x{addr_i:X} count={used}"
                        )
        except Exception:
            pass
        return

    def _on_embed(self, text, tp):
        if not self._enable_embed:
            return
        raw_text = "" if text is None else str(text)
        cleaned = self._clean_text(text)
        if not cleaned:
            return
        try:
            tp_copy = ThreadParam()
            tp_copy.processId = int(tp.processId)
            tp_copy.addr = int(tp.addr)
            tp_copy.ctx = int(tp.ctx)
            tp_copy.ctx2 = int(tp.ctx2)
            self._embed_seq += 1
            request_id = "{}-{}-{}-{}-{}".format(
                int(tp_copy.processId),
                int(tp_copy.addr),
                int(tp_copy.ctx),
                int(tp_copy.ctx2),
                self._embed_seq,
            )
            with self._embed_pending_lock:
                self._embed_pending[request_id] = (tp_copy, raw_text)
            self.status.emit(f"Embed request: {request_id}")
            self.embed_text_requested.emit(request_id, cleaned)
        except Exception:
            return

    def _on_i18n_query(self, querytext):
        try:
            luna = self._luna
            if luna and hasattr(luna, "Luna_AllocString"):
                return luna.Luna_AllocString(querytext)
        except Exception:
            pass
        return None
