"""Bounded UTF-8 JSONL transport for owned local subprocesses on Windows/macOS.

Windows select() cannot watch subprocess pipes. Dedicated I/O threads keep both
reads and writes behind the same deadline without changing the child sandbox.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

from platform_support import subprocess_options

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 16 * 1024


def command_prefix(value):
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    if not values or len(values) > 32:
        raise ValueError("本地程序命令无效")
    result = []
    for item in values:
        try:
            item = os.fspath(item)
        except TypeError:
            raise ValueError("本地程序命令无效") from None
        if not isinstance(item, str) or not item or len(item) > 4096 or "\x00" in item:
            raise ValueError("本地程序命令无效")
        result.append(item)
    return result


class JsonRpcProcess:
    """Own one child process, discard stderr, and retain bounded JSON responses."""

    def __init__(self, command, *, timeout=15.0):
        self.deadline = time.monotonic() + timeout
        self.incoming = queue.Queue(maxsize=8)
        self.outgoing = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.proc = subprocess.Popen(command_prefix(command), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     **subprocess_options())
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.reader.start()
        self.writer.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _put(self, value):
        while not self.stopping.is_set():
            try:
                self.incoming.put(value, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read(self):
        received = 0
        try:
            while not self.stopping.is_set():
                line = self.proc.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                received += len(line)
                if received > MAX_OUTPUT_BYTES or len(line) > MAX_MESSAGE_BYTES:
                    break
                if not line.endswith(b"\n"):
                    break
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if isinstance(response, dict):
                    self._put(response)
        except (OSError, ValueError):
            pass
        finally:
            self._put(None)

    def _write(self):
        while not self.stopping.is_set():
            try:
                job = self.outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            data, done, failures = job
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                failures.append(True)
            finally:
                done.set()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("本地用量连接超时")
        return remaining

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=True) + "\n").encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("本地用量请求过大")
        done, failures = threading.Event(), []
        try:
            self.outgoing.put((data, done, failures), timeout=self._remaining())
        except queue.Full:
            raise TimeoutError("本地用量连接超时") from None
        if not done.wait(self._remaining()):
            raise TimeoutError("本地用量连接超时")
        if failures:
            raise OSError("本地用量连接已关闭")

    def responses(self):
        while not self.stopping.is_set():
            try:
                response = self.incoming.get(timeout=self._remaining())
            except queue.Empty:
                raise TimeoutError("本地用量连接超时") from None
            if response is None:
                return
            yield response

    def close(self):
        self.stopping.set()
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.reader.join(timeout=0.25)
        self.writer.join(timeout=0.25)
        # Never wait on a buffered-stream lock still held by a blocked thread.
        for thread, stream in ((self.reader, self.proc.stdout), (self.writer, self.proc.stdin)):
            if not thread.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
