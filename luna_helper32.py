from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import io
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Tuple


if sys.maxsize > 2**32:
    print(json.dumps({"type": "status", "message": "Helper must run in 32-bit Python."}), flush=True)
    sys.exit(1)


from ctypes import wintypes, c_bool, c_int, c_uint32, c_uint64, c_void_p, c_wchar_p, c_char_p


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400


def _emit_status(message: str) -> None:
    print(json.dumps({"type": "status", "message": message}, ensure_ascii=False), flush=True)


def _emit_text(text: str) -> None:
    print(json.dumps({"type": "text", "text": text}, ensure_ascii=False), flush=True)


def _configure_stdout() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            return
    except Exception:
        pass
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


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


def _is_process_64(pid: int):
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
            if native_machine.value == 0:
                return False
            return process_machine.value == 0
        is_wow64 = wintypes.BOOL()
        ok = kernel32.IsWow64Process(handle, ctypes.byref(is_wow64))
        if not ok:
            return None
        # On 64-bit OS: wow64=True => 32-bit; wow64=False => 64-bit
        return not bool(is_wow64.value)
    finally:
        _close_handle(handle)


def _find_luna_root(target_bit: str) -> Path:
    env_root = os.environ.get("LUNA_TRANSLATOR_DIR")
    if env_root:
        return Path(env_root).expanduser()
    base = Path(__file__).resolve().parent
    dedicated = base / "LunaHook"
    if (dedicated / "files").exists():
        return dedicated
    if target_bit == "32":
        candidates = [
            base / "LunaTranslator_x86_win7",
            base / "LunaTranslator_x86_winxp",
        ]
    else:
        candidates = [
            base / "LunaTranslator_x64_win10",
            base / "LunaTranslator_x64_win7",
        ]
    for cand in candidates:
        if cand.exists():
            return cand
    return base / "LunaTranslator_x64_win10"


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


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    return text.strip()


def _is_noise(text: str) -> bool:
    if not text:
        return True
    lower = text.lower()
    if "kernel32.dll" in lower or "user32.dll" in lower or "gdi32.dll" in lower:
        return True
    if "driverstore" in lower or "nvldumd" in lower or "nvfbc" in lower:
        return True
    if "d3d" in lower or "d3dx" in lower:
        return True
    if "\\windows\\" in lower and ".dll" in lower:
        return True
    if ".dll" in lower and len(text) > 60:
        return True
    if len(text) > 1000 and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
        return True
    return False


def main() -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--codepage", type=int, default=932)
    parser.add_argument("--text-thread-delay", type=int, default=500)
    parser.add_argument("--max-buffer-size", type=int, default=3000)
    parser.add_argument("--max-history-size", type=int, default=1000000)
    parser.add_argument("--auto-pc-hooks", action="store_true", default=False)
    parser.add_argument("--flush-delay-ms", type=int, default=120)
    args = parser.parse_args()

    pid = args.pid
    is64 = _is_process_64(pid)
    if is64 is None:
        _emit_status("Cannot determine target process architecture.")
        return 2
    if is64:
        _emit_status("Target process is 64-bit. Use 64-bit hook backend.")
        return 3

    luna_root = _find_luna_root("32")
    files = luna_root / "files"
    hook_dir = files / "LunaHook"
    host = hook_dir / "LunaHost32.dll"
    hook = hook_dir / "LunaHook32.dll"
    proxy = files / "shareddllproxy32.exe"

    missing = []
    if not host.exists():
        missing.append(str(host))
    if not hook.exists():
        missing.append(str(hook))
    if not proxy.exists():
        missing.append(str(proxy))
    if missing:
        _emit_status("Missing LunaHook files for 32-bit target: " + "; ".join(missing))
        return 4

    stop_event = threading.Event()
    pending: Dict[Tuple[int, int, int, int], Tuple[str, float]] = {}
    last_emitted: Dict[Tuple[int, int, int, int], str] = {}
    pending_lock = threading.Lock()
    flush_delay = max(10, int(args.flush_delay_ms)) / 1000.0
    recent_texts: Dict[str, float] = {}
    recent_window = 1.2

    def flush_loop():
        while not stop_event.is_set():
            now = time.time()
            emit_list = []
            with pending_lock:
                for key, (text, ts) in list(pending.items()):
                    if now - ts < flush_delay:
                        continue
                    last = last_emitted.get(key)
                    if text and text != last:
                        last_emitted[key] = text
                        last_seen = recent_texts.get(text)
                        if last_seen is None or (now - last_seen) >= recent_window:
                            recent_texts[text] = now
                            emit_list.append(text)
                    pending.pop(key, None)
                for t, tstamp in list(recent_texts.items()):
                    if now - tstamp > 5.0:
                        recent_texts.pop(t, None)
            for text in emit_list:
                _emit_text(text)
            time.sleep(0.05)

    def watch_stdin():
        try:
            for line in sys.stdin:
                if line.strip().lower() == "quit":
                    stop_event.set()
                    break
        except Exception:
            stop_event.set()

    luna = ctypes.CDLL(str(host))
    luna.Luna_SyncThread.argtypes = (ThreadParam, c_bool)
    luna.Luna_InsertPCHooks.argtypes = (wintypes.DWORD, c_int)
    luna.Luna_Settings.argtypes = (c_int, c_bool, c_int, c_int, c_int)
    luna.Luna_Start.argtypes = (
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
    luna.Luna_ConnectProcess.argtypes = (wintypes.DWORD,)
    luna.Luna_CheckIfNeedInject.argtypes = (wintypes.DWORD,)
    luna.Luna_CheckIfNeedInject.restype = c_bool
    luna.Luna_DetachProcess.argtypes = (wintypes.DWORD,)
    luna.Luna_ResetLang.argtypes = ()
    if hasattr(luna, "Luna_AllocString"):
        luna.Luna_AllocString.argtypes = (c_wchar_p,)
        luna.Luna_AllocString.restype = c_void_p

    def _insert_pc_hooks(pid_):
        time.sleep(0.6)
        try:
            hook_ids = [0]
            if os.environ.get("LUNA_PC_HOOKS_BOTH") == "1":
                hook_ids.append(1)
            for hook_id in hook_ids:
                luna.Luna_InsertPCHooks(pid_, hook_id)
                time.sleep(0.1)
        except Exception:
            pass

    def on_proc_connect(pid_):
        if args.auto_pc_hooks:
            threading.Thread(target=_insert_pc_hooks, args=(pid_,), daemon=True).start()
        _emit_status(f"Process connected: {pid_}")

    def on_proc_remove(pid_):
        _emit_status(f"Process removed: {pid_}")

    def on_new_hook(hc, hn, tp, isembedable):
        try:
            luna.Luna_SyncThread(tp, True)
        except Exception:
            pass

    def on_remove_hook(hc, hn, tp):
        return

    def on_output(hc, hn, tp, output):
        text = _clean_text(output)
        if not text or _is_noise(text):
            return
        key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
        with pending_lock:
            pending[key] = (text, time.time())

    def on_host_info(code, msg):
        if msg:
            _emit_status(str(msg))

    def on_hook_insert(pid_, addr, hcode):
        return

    def on_embed(text, tp):
        return

    def on_i18n_query(querytext):
        try:
            if hasattr(luna, "Luna_AllocString"):
                return luna.Luna_AllocString(querytext)
        except Exception:
            pass
        return None

    callbacks = [
        ProcessEvent(on_proc_connect),
        ProcessEvent(on_proc_remove),
        ThreadEventMaybeEmbed(on_new_hook),
        ThreadEvent(on_remove_hook),
        OutputCallback(on_output),
        HostInfoHandler(on_host_info),
        HookInsertHandler(on_hook_insert),
        EmbedCallback(on_embed),
        I18NQueryCallback(on_i18n_query),
    ]

    luna.Luna_Start(*callbacks)
    luna.Luna_Settings(
        int(args.text_thread_delay),
        False,
        int(args.codepage),
        int(args.max_buffer_size),
        int(args.max_history_size),
    )
    luna.Luna_ResetLang()

    luna.Luna_ConnectProcess(pid)
    if luna.Luna_CheckIfNeedInject(pid):
        ret = subprocess.run([str(proxy), "dllinject", str(pid), str(hook)], check=False).returncode
        if ret == 0:
            _emit_status("Injected LunaHook DLL.")
        else:
            _emit_status("DLL injection failed, trying elevated injection...")
            try:
                ctypes.windll.shell32.ShellExecuteW(
                    None,
                    "runas",
                    str(proxy),
                    f'dllinject {pid} "{hook}"',
                    None,
                    0,
                )
            except Exception as e:
                _emit_status(f"Elevation failed: {e}")

    threading.Thread(target=flush_loop, daemon=True).start()
    threading.Thread(target=watch_stdin, daemon=True).start()

    try:
        while not stop_event.is_set():
            time.sleep(0.05)
    finally:
        try:
            luna.Luna_DetachProcess(pid)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
