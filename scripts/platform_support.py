"""Small platform adapters. Never invoke an arbitrary command shell or change policy."""
from __future__ import annotations

import contextlib
import errno
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def is_windows():
    return sys.platform == "win32"


def default_data_dir():
    if is_windows():
        local = os.environ.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "Codex Usage Meter"
    return Path.home() / "Library/Application Support/Codex Usage Meter"


def _npm_command(shim):
    """Resolve the official npm layout directly; the .cmd contents are never run."""
    path = Path(shim)
    if path.name.lower() != "codex.cmd":
        return None
    try:
        if path.stat().st_size > 32_768:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    target = r"node_modules\@openai\codex\bin\codex.js"
    if "\0" in text or not re.search(r"%(?:dp0|~dp0)%?[\\/]?" + re.escape(target), text, re.IGNORECASE):
        return None
    script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not script.is_file():
        return None
    adjacent_node = path.parent / "node.exe"
    node = str(adjacent_node) if adjacent_node.is_file() else shutil.which("node.exe")
    if node and Path(node).suffix.lower() == ".exe" and Path(node).is_file():
        return [node, str(script)]
    return None


def codex_command():
    """Return an argument prefix for the installed CLI, including safe npm shims."""
    if is_windows():
        native = shutil.which("codex.exe")
        if native and Path(native).suffix.lower() == ".exe" and Path(native).is_file():
            return [native]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            native = Path(local) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
            if native.is_file():
                return [str(native)]
        candidate = shutil.which("codex") or shutil.which("codex.cmd")
        if candidate:
            path = Path(candidate)
            if path.suffix.lower() == ".exe" and path.is_file():
                return [candidate]
            command = _npm_command(candidate)
            if command:
                return command
        raise RuntimeError("找不到可运行的 Codex CLI，请先安装官方 Windows CLI 并登录，再重新打开终端")
    candidate = shutil.which("codex")
    if candidate:
        return [candidate]
    for name in ("ChatGPT", "Codex"):
        candidate = Path("/Applications") / (name + ".app") / "Contents/Resources/codex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    raise RuntimeError("找不到 Codex，请先安装并登录 Codex")


def subprocess_options(*, background=False):
    """Avoid Windows console popups; detach only long-lived local services."""
    options = {"close_fds": True}
    if is_windows():
        if background:
            options["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008) |
                                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        else:
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    elif background:
        options["start_new_session"] = True
    return options


@contextlib.contextmanager
def file_lock(path, timeout=10.0):
    """Serialize separate processes with a bounded wait and guaranteed release."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("文件锁等待时间无效")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        if is_windows():
            import msvcrt
            # Byte-range locks may extend beyond EOF; no pre-lock write is needed.
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) and getattr(exc, "winerror", None) not in (32, 33):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待本地用量文件锁超时") from exc
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            release()
