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
from ctypes import wintypes, c_bool, c_int, c_uint32, c_uint64, c_uint8, c_void_p, c_wchar_p, c_char_p

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

        self._pending: Dict[Tuple[int, int, int, int], Tuple[str, float]] = {}
        self._last_emitted: Dict[Tuple[int, int, int, int], str] = {}
        self._pending_lock = threading.Lock()
        self._sync_queue: "queue.Queue[ThreadParam]" = queue.Queue()
        self._synced_keys: set[Tuple[int, int, int, int]] = set()

        self._luna = None
        self._callbacks = []
        self._luna_paths = {}
        self._helper_proc = None
        self._helper_queue: "queue.Queue[str]" = queue.Queue()
        self._helper_reader = None
        self._helper_mode = False
        self._target_bit: Optional[str] = None
        self._safe_mode = os.environ.get("LUNA_SAFE_MODE") == "1"
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
            self.wait(1500)
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
        self.status.emit(line)

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
        self.status.emit("Luna: Luna_CheckIfNeedInject...")
        if self._luna.Luna_CheckIfNeedInject(pid):
            self._inject(pid, target_bit)

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
            self.status.emit("DLL injection failed, trying elevated injection...")
            ctypes.windll.shell32.ShellExecuteW(None, "runas", proxy, f'dllinject {pid} "{hook}"', None, 0)
            # Wait briefly and re-check injection status
            for _ in range(25):
                time.sleep(0.2)
                try:
                    if not self._luna.Luna_CheckIfNeedInject(pid):
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
                tp = self._sync_queue.get_nowait()
                key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
                if key in self._synced_keys:
                    continue
                self._synced_keys.add(key)
                try:
                    self._luna.Luna_SyncThread(tp, True)
                except Exception:
                    pass
        except queue.Empty:
            return

    def _detach(self) -> None:
        if not self._luna or self._pid is None:
            return
        try:
            self._luna.Luna_DetachProcess(self._pid)
        except Exception:
            pass

    # Callbacks
    def _on_proc_connect(self, pid):
        if self._auto_pc_hooks and not self._safe_mode:
            try:
                self._luna.Luna_InsertPCHooks(pid, 0)
                self._luna.Luna_InsertPCHooks(pid, 1)
            except Exception:
                pass
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
            self._sync_queue.put(tp_copy)
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
            key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
            with self._pending_lock:
                self._pending[key] = (text, time.time())
        except Exception:
            return

    def _on_host_info(self, code, msg):
        if msg:
            self.status.emit(str(msg))

    def _on_hook_insert(self, pid, addr, hcode):
        return

    def _on_embed(self, text, tp):
        return

    def _on_i18n_query(self, querytext):
        try:
            if hasattr(self._luna, "Luna_AllocString"):
                return self._luna.Luna_AllocString(querytext)
        except Exception:
            pass
        return None
