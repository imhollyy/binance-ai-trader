"""Best-effort OS process identity tokens for supervisor attestation."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def process_start_token(pid: int) -> str | None:
    """Return a stable process creation token on supported local OS families."""
    if type(pid) is not int or pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(")") + 2 :].split()
            if fields[0] in {"Z", "X"}:
                return None
            return f"linux:{fields[19]}"
        except (OSError, IndexError):
            return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel.GetExitCodeProcess.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            try:
                ok = kernel.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                if not ok:
                    return None
                exit_code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                if exit_code.value != 259:  # STILL_ACTIVE
                    return None
                ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
                return f"windows:{ticks}"
            finally:
                kernel.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
            return None
    return None


def process_matches(pid: int | None, token: str | None) -> bool:
    """Reject missing, reused, or unsupported process identities."""
    return bool(pid and token and process_start_token(pid) == token)
