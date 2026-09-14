"""Read names for explicitly registered threads through public Codex metadata RPC.

Documentation: https://learn.chatgpt.com/docs/app-server#read-a-stored-thread-without-resuming
`thread/read` with `includeTurns: false` does not resume a thread or load it into
memory. Only matching thread IDs and sanitized `thread.name` values leave this
module. Preview text, messages, turns, and other metadata are discarded.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
import unicodedata
from typing import Any, Iterable


MAX_THREADS = 100
MAX_TITLE_LENGTH = 120
RPC_TIMEOUT_SECONDS = 15.0
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def normalize_title(value: Any) -> str | None:
    """Return a short plain title, or None; never infer one from message text."""
    if not isinstance(value, str):
        return None
    # Bound normalization work even if a malformed peer returns a huge title.
    characters = (
        " " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char
        for char in value[:4096]
    )
    title = " ".join("".join(characters).split())[:MAX_TITLE_LENGTH].rstrip()
    return title or None


def _identifiers(values: Iterable[str], limit: int | None = None) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("需要任务编号列表。")
    try:
        iterator = iter(values)
    except TypeError:
        raise ValueError("需要任务编号列表。") from None
    unique: dict[str, None] = {}
    for index, value in enumerate(iterator):
        if limit is not None and index >= limit:
            raise ValueError("每次最多读取 100 个已登记任务的名称。")
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError("任务编号格式无效。")
        unique[value] = None
    return list(unique)


def display_ids(ids: Iterable[str]) -> dict[str, str]:
    """Use at least six suffix characters, expanding collisions independently.

    Short IDs are shown in full. Duplicate input IDs share one label. A full ID
    that is itself another ID's suffix remains distinct as the longer ID grows.
    """
    values = _identifiers(ids)
    lengths = {value: min(6, len(value)) for value in values}
    while True:
        groups: dict[str, list[str]] = {}
        for value in values:
            groups.setdefault(value[-lengths[value]:], []).append(value)
        collisions = [group for group in groups.values() if len(group) > 1]
        if not collisions:
            return {value: value[-lengths[value]:] for value in values}
        for group in collisions:
            for value in group:
                if lengths[value] < len(value):
                    lengths[value] += 1


def _send(proc: subprocess.Popen, message: dict[str, Any]) -> None:
    # One small request is outstanding at a time. No shell interpretation.
    proc.stdin.write((json.dumps(message, ensure_ascii=True) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _responses(proc: subprocess.Popen, selector: selectors.BaseSelector, deadline: float):
    buffer = bytearray()
    received = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        for key, _ in selector.select(min(0.25, remaining)):
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                return
            received += len(chunk)
            if received > MAX_OUTPUT_BYTES:
                return
            buffer.extend(chunk)
            while True:
                boundary = buffer.find(b"\n")
                if boundary < 0:
                    break
                if boundary > MAX_MESSAGE_BYTES:
                    return
                raw = bytes(buffer[:boundary])
                del buffer[:boundary + 1]
                try:
                    response = json.loads(raw)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if isinstance(response, dict):
                    yield response
            if len(buffer) > MAX_MESSAGE_BYTES:
                return


def _close_process(proc: subprocess.Popen) -> None:
    """Close all owned pipes and reap the child, with at most one second waiting."""
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=0.5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
    finally:
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def fetch_conversation_titles(thread_ids: Iterable[str], codex_binary: str | os.PathLike[str]) -> dict[str, str]:
    """Read official names for at most 100 exact, caller-supplied thread IDs.

    `codex_binary` is an already resolved executable path; this function does no
    file discovery. It starts one documented stdio server, uses only initialize,
    initialized, and thread/read(includeTurns=False), and makes no model calls.
    Missing names and per-ID failures are omitted. Connection/protocol failures
    return names already read, if any. The RPC deadline is 15 seconds, followed
    by at most one second of process cleanup. Invalid input raises ValueError.
    """
    ids = _identifiers(thread_ids, MAX_THREADS)
    if not ids:
        return {}
    try:
        binary = os.fspath(codex_binary)
    except TypeError:
        raise ValueError("需要已定位的 Codex 程序路径。") from None
    if not isinstance(binary, str) or not binary or len(binary) > 4096 or "\x00" in binary:
        raise ValueError("Codex 程序路径无效。")

    names: dict[str, str] = {}
    proc = None
    selector = None
    deadline = time.monotonic() + RPC_TIMEOUT_SECONDS
    try:
        proc = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        os.set_blocking(proc.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        _send(proc, {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_usage_meter_titles", "version": "0.3.0"},
        }})

        initialized = False
        position = 0
        request_id = 2
        for response in _responses(proc, selector, deadline):
            response_id = response.get("id")
            # Reject bool IDs; in Python True otherwise equals request ID 1.
            if type(response_id) is not int:
                continue
            if not initialized:
                if response_id != 1:
                    continue
                if "error" in response or not isinstance(response.get("result"), dict):
                    return names
                initialized = True
                _send(proc, {"method": "initialized"})
            else:
                if response_id != request_id:
                    continue
                expected_id = ids[position]
                result = response.get("result")
                if "error" not in response and isinstance(result, dict):
                    thread = result.get("thread")
                    if isinstance(thread, dict) and thread.get("id") == expected_id:
                        title = normalize_title(thread.get("name"))
                        if title is not None:
                            names[expected_id] = title
                position += 1
                request_id += 1
                if position >= len(ids):
                    return names
            _send(proc, {"id": request_id, "method": "thread/read", "params": {
                "threadId": ids[position], "includeTurns": False,
            }})
    except (OSError, ValueError, subprocess.SubprocessError):
        # Failure details may contain local paths or server data; return neither.
        return names
    finally:
        if selector is not None:
            selector.close()
        if proc is not None:
            _close_process(proc)
    return names


__all__ = ["fetch_conversation_titles", "normalize_title", "display_ids"]
