from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

MEM_COMMIT = 0x1000

PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

PATCHABLE_PROTECT = {
    0x04,  # PAGE_READWRITE
    0x08,  # PAGE_WRITECOPY
}


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


class ProcessMemoryPatchWorker:
    def __init__(self, pid: int, status_cb=None, source_codepage: int = 932, debug_cb=None) -> None:
        self.pid = int(pid)
        self.status_cb = status_cb
        self.source_codepage = int(source_codepage or 932)
        self.debug_cb = debug_cb
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._mapping: Dict[str, str] = {}
        self._max_pairs = 300
        self._scan_interval = 0.8
        self._burst_interval = 0.05
        self._burst_duration_sec = 2.2
        self._max_scan_bytes = 96 * 1024 * 1024
        self._max_writes_per_cycle = 80
        self._max_writes_per_pair = 6
        self._last_stats_ts = 0.0
        self._cycles_without_hits = 0
        self._boost_until = 0.0
        self._zero_scan_cycles = 0
        self._hot_slots: List[Dict[str, object]] = []
        self._hot_slot_ttl_sec = 8.0
        self._max_hot_slots = 96
        self._enable_hot_rewrite = os.environ.get("JAMX_MEMORY_HOT_REWRITE", "0") == "1"

    def _emit(self, msg: str) -> None:
        if self.status_cb:
            try:
                self.status_cb(msg)
            except Exception:
                pass

    def _emit_debug(self, event: str, **payload) -> None:
        cb = self.debug_cb
        if not cb:
            return
        try:
            item = {"event": event}
            item.update(payload)
            cb(item)
        except Exception:
            pass

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._emit(f"Memory patch worker started (pid={self.pid}).")
        self._emit_debug(
            "started",
            pid=self.pid,
            source_codepage=self.source_codepage,
            scan_interval=self._scan_interval,
            max_scan_bytes=self._max_scan_bytes,
            max_writes_per_cycle=self._max_writes_per_cycle,
        )

    def stop(self) -> None:
        self._running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    def update_mapping(self, source_text: str, translated_text: str) -> bool:
        src = (source_text or "").strip()
        dst = (translated_text or "").strip()
        if not src or not dst or src == dst:
            return False
        variants = self._build_source_variants(src)
        if len(src) < 3:
            self._emit_debug("mapping_skip", reason="source_too_short", source_text=src)
            return False
        with self._lock:
            for item in variants:
                self._mapping[item] = dst
            if len(self._mapping) > self._max_pairs:
                keys = list(self._mapping.keys())[-self._max_pairs :]
                self._mapping = {k: self._mapping[k] for k in keys}
        self._boost_until = time.time() + self._burst_duration_sec
        self._emit_debug(
            "mapping_update",
            source_text=src,
            translated_text=dst,
            variant_count=len(variants),
            variants=variants,
            burst_until=self._boost_until,
        )
        return True

    def _build_source_variants(self, text: str) -> List[str]:
        base = (text or "").strip()
        if not base:
            return []
        out: List[str] = []
        seen = set()

        def _add(item: str) -> None:
            s = (item or "").strip()
            if not s or s in seen:
                return
            if len(s) < 2:
                return
            seen.add(s)
            out.append(s)

        _add(base)
        _add(base.replace("\r", "").replace("\n", ""))
        _add(base.replace(" ", "").replace("\u3000", ""))

        codec_names = [
            f"cp{self.source_codepage}",
            "cp932",
            "shift_jis",
            "cp936",
            "gbk",
            "big5",
            "utf-8",
            "cp1252",
            "latin1",
        ]
        for src_codec in codec_names:
            for dst_codec in ("utf-8", "cp932", "shift_jis", "cp936", "gbk"):
                if src_codec == dst_codec:
                    continue
                try:
                    recovered = base.encode(src_codec, errors="strict").decode(dst_codec, errors="strict")
                except Exception:
                    continue
                _add(recovered)
                _add(recovered.replace("\r", "").replace("\n", ""))
                _add(recovered.replace(" ", "").replace("\u3000", ""))
        return out

    def _open_process(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
            False,
            self.pid,
        )

    def _is_process_alive(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not hproc:
            return False
        kernel32.CloseHandle(hproc)
        return True

    def _close_handle(self, hproc) -> None:
        if hproc:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(hproc)

    def _iter_regions(self, hproc):
        mbi = MEMORY_BASIC_INFORMATION()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        addr = 0
        mbi_size = ctypes.sizeof(mbi)

        while True:
            res = kernel32.VirtualQueryEx(hproc, ctypes.c_void_p(addr), ctypes.byref(mbi), mbi_size)
            if not res:
                break
            base = int(ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value or 0)
            size = int(mbi.RegionSize)
            if size <= 0:
                break
            yield base, size, int(mbi.State), int(mbi.Protect)
            addr = base + size
            if addr <= base:
                break

    def _is_patchable(self, state: int, protect: int) -> bool:
        if state != MEM_COMMIT:
            return False
        if protect & PAGE_GUARD:
            return False
        if protect & PAGE_NOACCESS:
            return False
        base_protect = protect & 0xFF
        return base_protect in PATCHABLE_PROTECT

    def _normalize_replacement_bytes(self, src_b: bytes, dst_b: bytes, is_utf16: bool) -> bytes:
        if is_utf16 and len(src_b) % 2 == 1:
            src_b = src_b[:-1]
        if len(dst_b) > len(src_b):
            dst_b = dst_b[: len(src_b)]
            if is_utf16 and len(dst_b) % 2 == 1:
                dst_b = dst_b[:-1]
        if len(dst_b) < len(src_b):
            fill = b"\x00" if is_utf16 else b" "
            dst_b = dst_b + (fill * (len(src_b) - len(dst_b)))
        return dst_b

    def _read_region(self, hproc, base: int, size: int) -> Optional[bytes]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            hproc,
            ctypes.c_void_p(base),
            buf,
            size,
            ctypes.byref(read),
        )
        if not ok or read.value <= 0:
            return None
        return bytes(buf.raw[: read.value])

    def _write_region(self, hproc, base: int, data: bytes) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            hproc,
            ctypes.c_void_p(base),
            data,
            len(data),
            ctypes.byref(written),
        )
        return bool(ok and written.value == len(data))

    def _snapshot_pairs(self) -> List[Tuple[bytes, bytes, str, str, str]]:
        with self._lock:
            items = list(self._mapping.items())
        items.sort(key=lambda x: len(x[0]), reverse=True)
        out: List[Tuple[bytes, bytes, str, str, str]] = []
        codec_candidates = [f"cp{self.source_codepage}", "cp932", "shift_jis", "cp936", "gbk", "utf-8"]
        seen = set()
        for src, dst in items:
            if len(src) < 3:
                continue
            src_utf16 = src.encode("utf-16le", errors="ignore")
            dst_utf16 = dst.encode("utf-16le", errors="ignore")
            if src_utf16 and len(src_utf16) >= 8:
                patched_utf16 = self._normalize_replacement_bytes(src_utf16, dst_utf16, True)
                k = (src_utf16, patched_utf16, "utf16", src, dst)
                if k not in seen:
                    seen.add(k)
                    out.append(k)

            for codec in codec_candidates:
                try:
                    src_a = src.encode(codec, errors="strict")
                    dst_a = dst.encode(codec, errors="ignore")
                except Exception:
                    continue
                if not src_a or len(src_a) < 5:
                    continue
                patched_a = self._normalize_replacement_bytes(src_a, dst_a, False)
                k = (src_a, patched_a, codec, src, dst)
                if k not in seen:
                    seen.add(k)
                    out.append(k)
        return out

    def _refresh_hot_slots(self) -> None:
        now = time.time()
        kept: List[Dict[str, object]] = []
        for s in self._hot_slots:
            exp = s.get("expire_at", 0.0)
            exp_f = float(exp) if isinstance(exp, (int, float)) else 0.0
            if exp_f > now:
                kept.append(s)
        self._hot_slots = kept

    def _remember_hot_slot(self, address: int, dst_b: bytes, codec: str, src_text: str, dst_text: str) -> None:
        now = time.time()
        self._refresh_hot_slots()
        for s in self._hot_slots:
            addr_obj = s.get("address", -1)
            addr_i = int(addr_obj) if isinstance(addr_obj, int) else -1
            if addr_i == int(address):
                s["dst_b"] = dst_b
                s["expire_at"] = now + self._hot_slot_ttl_sec
                return
        self._hot_slots.append(
            {
                "address": int(address),
                "dst_b": dst_b,
                "codec": codec,
                "src_text": src_text,
                "dst_text": dst_text,
                "expire_at": now + self._hot_slot_ttl_sec,
            }
        )
        if len(self._hot_slots) > self._max_hot_slots:
            self._hot_slots = self._hot_slots[-self._max_hot_slots :]

    def _apply_hot_slots(self, hproc) -> int:
        self._refresh_hot_slots()
        if not self._hot_slots:
            return 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        applied = 0
        for slot in self._hot_slots:
            address_obj = slot.get("address", 0)
            address = int(address_obj) if isinstance(address_obj, int) else 0
            data = slot.get("dst_b")
            if not address or not isinstance(data, (bytes, bytearray)):
                continue
            written = ctypes.c_size_t(0)
            ok = kernel32.WriteProcessMemory(
                hproc,
                ctypes.c_void_p(address),
                bytes(data),
                len(data),
                ctypes.byref(written),
            )
            if ok and written.value == len(data):
                applied += 1
        return applied

    def _apply_once(self) -> tuple[int, int]:
        pairs = self._snapshot_pairs()
        if not pairs:
            return 0, 0

        hproc = self._open_process()
        if not hproc:
            return 0, 0

        try:
            scanned = 0
            write_count = 0
            hit_regions = 0
            match_records = []
            hot_rewrites = 0
            if self._enable_hot_rewrite:
                hot_rewrites = self._apply_hot_slots(hproc)
                if hot_rewrites > 0:
                    write_count += int(hot_rewrites)
            for base, size, state, protect in self._iter_regions(hproc):
                if scanned >= self._max_scan_bytes or write_count >= self._max_writes_per_cycle:
                    break
                if size <= 0 or size > 6 * 1024 * 1024:
                    continue
                if not self._is_patchable(state, protect):
                    continue

                chunk = self._read_region(hproc, base, size)
                if not chunk:
                    continue

                scanned += len(chunk)
                mutable = bytearray(chunk)
                changed = False

                for src_b, dst_b, codec, src_text, dst_text in pairs:
                    start = 0
                    pair_writes = 0
                    while True:
                        idx = mutable.find(src_b, start)
                        if idx < 0:
                            break
                        mutable[idx : idx + len(src_b)] = dst_b
                        changed = True
                        write_count += 1
                        pair_writes += 1
                        self._remember_hot_slot(base + idx, dst_b, codec, src_text, dst_text)
                        if len(match_records) < 120:
                            match_records.append(
                                {
                                    "address": f"0x{base + idx:X}",
                                    "region_base": f"0x{base:X}",
                                    "offset": int(idx),
                                    "codec": codec,
                                    "source_text": src_text,
                                    "target_text": dst_text,
                                }
                            )
                        if write_count >= self._max_writes_per_cycle:
                            break
                        if pair_writes >= self._max_writes_per_pair:
                            break
                        start = idx + len(src_b)
                    if write_count >= self._max_writes_per_cycle:
                        break

                if changed:
                    if self._write_region(hproc, base, bytes(mutable)):
                        hit_regions += 1
            self._emit_debug(
                "cycle",
                scanned_bytes=scanned,
                pair_count=len(pairs),
                write_count=write_count,
                hit_regions=hit_regions,
                hot_slots=len(self._hot_slots),
                hot_rewrites=hot_rewrites,
                matches=match_records,
            )
            self._emit_cycle_stats(scanned, len(pairs), write_count, hit_regions)
            return scanned, write_count
        finally:
            self._close_handle(hproc)

    def _emit_cycle_stats(self, scanned: int, pair_count: int, write_count: int, hit_regions: int) -> None:
        now = time.time()
        if write_count > 0:
            self._cycles_without_hits = 0
            self._last_stats_ts = now
            self._emit(
                f"Memory patch hit: writes={write_count}, regions={hit_regions}, "
                f"pairs={pair_count}, scanned={scanned // 1024}KB"
            )
            return
        self._cycles_without_hits += 1
        if (now - self._last_stats_ts) >= 8.0 and self._cycles_without_hits >= 6:
            self._last_stats_ts = now
            self._emit(
                f"Memory patch searching: pairs={pair_count}, scanned={scanned // 1024}KB, no hits yet"
            )

    def _loop(self) -> None:
        while self._running:
            try:
                scanned, _writes = self._apply_once()
                if scanned <= 0:
                    self._zero_scan_cycles += 1
                    if self._zero_scan_cycles >= 6 and (not self._is_process_alive()):
                        self._emit("Memory patch worker stopped: target process is no longer alive.")
                        self._emit_debug("stopped", reason="process_not_alive")
                        self._running = False
                        break
                else:
                    self._zero_scan_cycles = 0
            except Exception as e:
                self._emit_debug("error", message=str(e))
            now = time.time()
            interval = self._burst_interval if now < self._boost_until else self._scan_interval
            time.sleep(interval)
