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
import queue
from pathlib import Path
from typing import Dict, Tuple


if sys.maxsize > 2**32:
    print(json.dumps({"type": "status", "message": "Helper must run in 32-bit Python."}), flush=True)
    sys.exit(1)


from ctypes import wintypes, c_bool, c_int, c_uint32, c_uint64, c_uint8, c_void_p, c_wchar_p, c_char_p, c_float


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_QUERY_INFORMATION = 0x0400


def _emit_status(message: str) -> None:
    print(json.dumps({"type": "status", "message": message}, ensure_ascii=False), flush=True)


def _emit_text(text: str) -> None:
    print(json.dumps({"type": "text", "text": text}, ensure_ascii=False), flush=True)


def _emit_embed_request(request_id: str, text: str) -> None:
    print(
        json.dumps(
            {"type": "embed_request", "request_id": request_id, "text": text},
            ensure_ascii=False,
        ),
        flush=True,
    )


def _emit_debug(event: str, **payload) -> None:
    data = {"type": "debug", "event": event}
    data.update(payload)
    print(json.dumps(data, ensure_ascii=False), flush=True)


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
    parser.add_argument("--enable-embed", action="store_true", default=False)
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
    embed_pending_lock = threading.Lock()
    embed_pending: Dict[str, Tuple[ThreadParam, str]] = {}
    embed_result_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
    embed_seq = 0
    native_embed_req_count = 0
    out_embed_req_count = 0
    unsupported_embed_warned = False
    embed_enabled_keys: set[Tuple[int, int, int, int]] = set()
    embed_primary_enabled_keys: set[Tuple[int, int, int, int]] = set()
    embed_hook_addrs: Dict[int, set[int]] = {}
    embed_primary_addr: Dict[int, int] = {}
    ctx_pairs_by_pid: Dict[int, set[Tuple[int, int]]] = {}
    ctx_pairs_by_pid_code: Dict[Tuple[int, str], set[Tuple[int, int]]] = {}

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
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.lower() == "quit":
                    stop_event.set()
                    break
                try:
                    payload = json.loads(stripped)
                except Exception:
                    continue
                if payload.get("type") != "embed_result":
                    continue
                request_id = payload.get("request_id") or ""
                trans = payload.get("translation") or ""
                if request_id:
                    embed_result_queue.put((request_id, trans))
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
    if hasattr(luna, "Luna_EmbedSettings"):
        luna.Luna_EmbedSettings.argtypes = (
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
    if hasattr(luna, "Luna_UseEmbed"):
        luna.Luna_UseEmbed.argtypes = (ThreadParam, c_bool)
    if hasattr(luna, "Luna_CheckIsUsingEmbed"):
        luna.Luna_CheckIsUsingEmbed.argtypes = (ThreadParam,)
        luna.Luna_CheckIsUsingEmbed.restype = c_bool
    if hasattr(luna, "Luna_EmbedCallback"):
        luna.Luna_EmbedCallback.argtypes = (ThreadParam, c_wchar_p, c_wchar_p)
    if hasattr(luna, "Luna_AllocString"):
        luna.Luna_AllocString.argtypes = (c_wchar_p,)
        luna.Luna_AllocString.restype = c_void_p

    def _apply_embed_settings(pid_):
        if not args.enable_embed:
            return
        if not hasattr(luna, "Luna_EmbedSettings"):
            return
        try:
            luna.Luna_EmbedSettings(
                pid_,
                int(os.environ.get("LUNA_EMBED_TIMEOUT_MS", "12000")),
                2,
                False,
                "",
                0,
                True,
                False,
                False,
                0.0,
            )
            _emit_status("Embed translation enabled.")
        except Exception as e:
            _emit_status(f"Failed to apply embed settings: {e}")

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
        _apply_embed_settings(pid_)
        _emit_status(f"Process connected: {pid_}")

    def on_proc_remove(pid_):
        _emit_status(f"Process removed: {pid_}")

    def on_new_hook(hc, hn, tp, isembedable):
        try:
            luna.Luna_SyncThread(tp, True)
        except Exception:
            pass
        pid_i = int(tp.processId)
        ctx_pair = (int(tp.ctx), int(tp.ctx2))
        ctx_pairs_by_pid.setdefault(pid_i, set()).add(ctx_pair)
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
        force_embed = force_embed or (int(tp.addr) in embed_hook_addrs.get(int(tp.processId), set()))
        if force_embed:
            for code_key in {hc_text.strip().lower(), hn_text.strip().lower()}:
                if code_key:
                    ctx_pairs_by_pid_code.setdefault((pid_i, code_key), set()).add(ctx_pair)
            _emit_status(
                f"Embed candidate hook: pid={int(tp.processId)} addr=0x{int(tp.addr):X} hc={hc_text}"
            )
        if args.enable_embed and (isembedable or force_embed) and hasattr(luna, "Luna_UseEmbed"):
            try:
                luna.Luna_UseEmbed(tp, True)
            except Exception:
                pass

    def on_remove_hook(hc, hn, tp):
        return

    def on_output(hc, hn, tp, output):
        nonlocal embed_seq, out_embed_req_count, native_embed_req_count, unsupported_embed_warned
        text = _clean_text(output)
        if not text or _is_noise(text):
            return
        raw_text = "" if output is None else str(output)
        key = (int(tp.processId), int(tp.addr), int(tp.ctx), int(tp.ctx2))
        with pending_lock:
            pending[key] = (text, time.time())
        embed_addrs = embed_hook_addrs.get(int(tp.processId), set())
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
        _emit_debug(
            "output_text",
            pid=int(tp.processId),
            addr=f"0x{int(tp.addr):X}",
            ctx=int(tp.ctx),
            ctx2=int(tp.ctx2),
            hook_code=("" if hc is None else str(hc)),
            hook_name=(hn.decode("utf-8", errors="ignore") if isinstance(hn, (bytes, bytearray)) else str(hn or "")),
            raw_text=raw_text,
            clean_text=text,
            is_qlie_output=bool(is_qlie_output),
        )
        pid_i = int(tp.processId)
        primary_addr = embed_primary_addr.get(pid_i)
        if (
            args.enable_embed
            and is_qlie_output
            and primary_addr
            and hasattr(luna, "Luna_UseEmbed")
        ):
            ctx_candidates = {
                int(tp.ctx),
                int(tp.ctx) & 0xFFFF,
            }
            ctx2_candidates = {
                int(tp.ctx2),
                int(tp.ctx2) & 0xFFFF,
                0,
                1,
            }
            for alt_ctx in ctx_candidates:
                for alt_ctx2 in ctx2_candidates:
                    pkey = (pid_i, int(primary_addr), int(alt_ctx), int(alt_ctx2))
                    if pkey in embed_primary_enabled_keys:
                        continue
                    try:
                        tp_primary = ThreadParam()
                        tp_primary.processId = pid_i
                        tp_primary.addr = int(primary_addr)
                        tp_primary.ctx = int(alt_ctx)
                        tp_primary.ctx2 = int(alt_ctx2)
                        try:
                            luna.Luna_SyncThread(tp_primary, True)
                        except Exception:
                            pass
                        luna.Luna_UseEmbed(tp_primary, True)
                        embed_primary_enabled_keys.add(pkey)
                        using_primary = None
                        if hasattr(luna, "Luna_CheckIsUsingEmbed"):
                            try:
                                using_primary = bool(luna.Luna_CheckIsUsingEmbed(tp_primary))
                            except Exception:
                                using_primary = None
                        if using_primary is None:
                            _emit_status(
                                f"Embed enabled on primary thread: {(pid_i, int(primary_addr), int(alt_ctx), int(alt_ctx2))}"
                            )
                        else:
                            _emit_status(
                                f"Embed enabled on primary thread: {(pid_i, int(primary_addr), int(alt_ctx), int(alt_ctx2))} using={using_primary}"
                            )
                    except Exception:
                        pass
        if (
            args.enable_embed
            and (int(tp.addr) in embed_addrs or is_qlie_output)
            and hasattr(luna, "Luna_UseEmbed")
            and key not in embed_enabled_keys
        ):
            try:
                tp_enable = ThreadParam()
                tp_enable.processId = int(tp.processId)
                tp_enable.addr = int(tp.addr)
                tp_enable.ctx = int(tp.ctx)
                tp_enable.ctx2 = int(tp.ctx2)
                luna.Luna_UseEmbed(tp_enable, True)
                embed_enabled_keys.add(key)
                using = None
                if hasattr(luna, "Luna_CheckIsUsingEmbed"):
                    try:
                        using = bool(luna.Luna_CheckIsUsingEmbed(tp_enable))
                    except Exception:
                        using = None
                if using is None:
                    _emit_status(f"Embed enabled on output thread: {key}")
                else:
                    _emit_status(f"Embed enabled on output thread: {key} using={using}")
            except Exception:
                pass
        if args.enable_embed and (int(tp.addr) in embed_addrs or is_qlie_output):
            try:
                tp_copy = ThreadParam()
                tp_copy.processId = int(tp.processId)
                tp_copy.addr = int(tp.addr)
                tp_copy.ctx = int(tp.ctx)
                tp_copy.ctx2 = int(tp.ctx2)
                embed_seq += 1
                request_id = "out-{}-{}-{}-{}-{}".format(
                    int(tp_copy.processId),
                    int(tp_copy.addr),
                    int(tp_copy.ctx),
                    int(tp_copy.ctx2),
                    embed_seq,
                )
                with embed_pending_lock:
                    embed_pending[request_id] = (tp_copy, raw_text)
                _emit_embed_request(request_id, text)
                _emit_debug(
                    "embed_request_fallback",
                    request_id=request_id,
                    pid=int(tp_copy.processId),
                    addr=f"0x{int(tp_copy.addr):X}",
                    ctx=int(tp_copy.ctx),
                    ctx2=int(tp_copy.ctx2),
                    source_text=raw_text,
                    cleaned_text=text,
                )
                out_embed_req_count += 1
                if (
                    (not unsupported_embed_warned)
                    and out_embed_req_count >= 3
                    and native_embed_req_count == 0
                ):
                    unsupported_embed_warned = True
                    _emit_status(
                        "No native EmbedCallback requests detected (only out-* fallback). "
                        "This title likely does not support in-place replacement via Luna embed on current hook path."
                    )
            except Exception:
                pass

    def on_host_info(code, msg):
        if msg:
            m = str(msg)
            _emit_debug("host_info", code=int(code), message=m)
            try:
                low = m.lower()
                if "embedqlie" in low:
                    hit = re.search(r"([0-9a-fA-F]{6,16})", m)
                    if hit:
                        addr_i = int(hit.group(1), 16)
                        pid_i = int(pid)
                        embed_primary_addr[pid_i] = addr_i
                        embed_hook_addrs.setdefault(pid_i, set()).add(addr_i)
                        _emit_status(f"Embed primary addr bound from host info: pid={pid_i} addr=0x{addr_i:X}")
            except Exception:
                pass
            _emit_status(m)

    def on_hook_insert(pid_, addr, hcode):
        try:
            hcode_text = str(hcode or "")
            hcode_low = hcode_text.lower()
            _emit_debug(
                "hook_insert",
                pid=int(pid_),
                addr=f"0x{int(addr):X}",
                hook_code=hcode_text,
            )
            if ("embed" in hcode_low) or ("qlie" in hcode_low):
                pid_i = int(pid_)
                addr_i = int(addr)
                addrs = embed_hook_addrs.setdefault(pid_i, set())
                addrs.add(addr_i)
                if ("embedqlie" in hcode_low) or (pid_i not in embed_primary_addr):
                    embed_primary_addr[pid_i] = addr_i
                _emit_status(
                    f"Embed/QLIE hook detected: pid={pid_i} addr=0x{addr_i:X} code={hcode_text}"
                )
                if args.enable_embed and hasattr(luna, "Luna_UseEmbed"):
                    code_key = hcode_text.strip().lower()
                    ctx_pairs = set(ctx_pairs_by_pid_code.get((pid_i, code_key), set()))
                    if not ctx_pairs:
                        ctx_pairs = set(ctx_pairs_by_pid.get(pid_i, set()))
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
                        _emit_status(
                            f"Embed bound contexts: pid={pid_i} addr=0x{addr_i:X} count={used}"
                        )
        except Exception:
            pass
        return

    def on_embed(text, tp):
        nonlocal embed_seq, native_embed_req_count
        if not args.enable_embed:
            return
        raw_text = "" if text is None else str(text)
        cleaned = _clean_text(text)
        if not cleaned:
            return
        try:
            tp_copy = ThreadParam()
            tp_copy.processId = int(tp.processId)
            tp_copy.addr = int(tp.addr)
            tp_copy.ctx = int(tp.ctx)
            tp_copy.ctx2 = int(tp.ctx2)
            embed_seq += 1
            request_id = "{}-{}-{}-{}-{}".format(
                int(tp_copy.processId),
                int(tp_copy.addr),
                int(tp_copy.ctx),
                int(tp_copy.ctx2),
                embed_seq,
            )
            with embed_pending_lock:
                embed_pending[request_id] = (tp_copy, raw_text)
            native_embed_req_count += 1
            _emit_status(f"Embed request: {request_id}")
            _emit_debug(
                "embed_request_native",
                request_id=request_id,
                pid=int(tp_copy.processId),
                addr=f"0x{int(tp_copy.addr):X}",
                ctx=int(tp_copy.ctx),
                ctx2=int(tp_copy.ctx2),
                source_text=raw_text,
                cleaned_text=cleaned,
            )
            _emit_embed_request(request_id, cleaned)
        except Exception:
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
    _apply_embed_settings(pid)
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

    def _send_embed_callback_variants(tp0: ThreadParam, src_text: str, trans_text: str) -> None:
        if not hasattr(luna, "Luna_EmbedCallback"):
            return
        src_raw = src_text or ""
        src_clean = _clean_text(src_raw)
        trans_send = trans_text or ""

        pid_i = int(tp0.processId)
        embed_addr = embed_primary_addr.get(pid_i)
        targets = []

        if embed_addr:
            tp_embed = ThreadParam()
            tp_embed.processId = int(tp0.processId)
            tp_embed.addr = int(embed_addr)
            tp_embed.ctx = int(tp0.ctx)
            tp_embed.ctx2 = int(tp0.ctx2)
            targets.append(tp_embed)

        targets.append(tp0)

        sent = 0
        seen = set()
        for target in targets:
            k = (int(target.processId), int(target.addr), int(target.ctx), int(target.ctx2))
            if k in seen:
                continue
            seen.add(k)
            try:
                luna.Luna_EmbedCallback(target, src_raw, trans_send)
                sent += 1
            except Exception:
                pass
            if src_clean and src_clean != src_raw:
                try:
                    luna.Luna_EmbedCallback(target, src_clean, trans_send)
                    sent += 1
                except Exception:
                    pass

        if targets:
            try:
                luna.Luna_EmbedCallback(targets[0], "", trans_send)
                sent += 1
            except Exception:
                pass

        _emit_status(f"Embed callback sent (variants={sent}).")
        _emit_debug(
            "embed_callback_sent",
            pid=int(tp0.processId),
            addr=f"0x{int(tp0.addr):X}",
            ctx=int(tp0.ctx),
            ctx2=int(tp0.ctx2),
            source_text=src_text,
            source_clean=_clean_text(src_text),
            translated_text=trans_text,
            variants=int(sent),
            primary_addr=(f"0x{int(embed_addr):X}" if embed_addr else None),
        )

    try:
        while not stop_event.is_set():
            try:
                while True:
                    request_id, trans = embed_result_queue.get_nowait()
                    with embed_pending_lock:
                        pending_embed = embed_pending.pop(request_id, None)
                    if not pending_embed:
                        continue
                    tp0, src_text = pending_embed
                    if hasattr(luna, "Luna_EmbedCallback"):
                        try:
                            _send_embed_callback_variants(tp0, src_text, trans)
                        except Exception:
                            pass
            except queue.Empty:
                pass
            time.sleep(0.05)
    finally:
        try:
            luna.Luna_DetachProcess(pid)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
