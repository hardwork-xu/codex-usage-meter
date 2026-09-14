"""Bounded adapter for one hook-supplied Codex JSONL usage log.

The hook's transcript path is documented, but the log format is unstable. This
adapter never discovers paths, opens conversation databases, or returns message
content. Counters describe observed usage of this one local thread, not account
quota or automatically aggregated child-agent usage.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
from datetime import datetime
from typing import Any


MAX_LOG_BYTES = 128 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_PENDING_CONTEXTS = 128
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_COUNTERS = (
    ("total_tokens", "total"),
    ("input_tokens", "input"),
    ("cached_input_tokens", "cachedInput"),
    ("cache_write_input_tokens", "cacheWriteInput"),
    ("output_tokens", "output"),
    ("reasoning_output_tokens", "reasoningOutput"),
)
_SOURCE_NOTE = "本地记录格式可能随 Codex 更新；每条仅统计该任务自身，子代理另列。"


class UsageLogError(ValueError):
    """A safe, content-free error describing an unreadable or untrusted log."""


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _timestamp(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) and 0 <= value <= 253402300799 else None
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        seconds = parsed.timestamp()
        return seconds if 0 <= seconds <= 253402300799 else None
    except (ValueError, OverflowError, OSError):
        return None


def _coherent(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for source, target in _COUNTERS:
        number = value.get(source)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        result[target] = number
    if result["total"] != result["input"] + result["output"]:
        return None
    if result["cachedInput"] > result["input"]:
        return None
    if result["reasoningOutput"] > result["output"]:
        return None
    return result


def _delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int] | None:
    delta = {key: current[key] - previous[key] for key in current}
    if any(value < 0 for value in delta.values()):
        return None
    if delta["total"] != delta["input"] + delta["output"]:
        return None
    if delta["cachedInput"] > delta["input"]:
        return None
    if delta["reasoningOutput"] > delta["output"]:
        return None
    return delta


def _bounded_last(last: dict[str, int] | None, total: dict[str, int]) -> dict[str, int] | None:
    if last is None or any(last[key] > total[key] for key in total):
        return None
    return last


class _PricingMetadata:
    """Keep bounded metadata evidence independently of token-count quality."""

    def __init__(self, *, incomplete: bool = False):
        self.first: tuple[str | None, str | None] | None = None
        self.model_changed = False
        self.tier_changed = False
        self.incomplete = incomplete

    def observe(self, model: str | None, service_tier: str | None) -> None:
        if self.first is None:
            self.first = (model, service_tier)
            return
        # Missing metadata is evidence of uncertainty, not a Standard default.
        self.model_changed |= self.first[0] != model
        self.tier_changed |= self.first[1] != service_tier

    def export(self) -> dict[str, Any]:
        model, service_tier = self.first if self.first is not None else (None, None)
        if self.model_changed:
            model = None
        if self.tier_changed:
            service_tier = None
        if self.model_changed or self.tier_changed:
            status = "mixed"
        elif model is not None and service_tier is not None and not self.incomplete:
            status = "known"
        else:
            status = "unknown"
        return {"model": model, "serviceTier": service_tier, "pricingMetadataStatus": status}


class _Turn:
    def __init__(self, turn_id: str, thread_id: str, started_at: float | int | None, *, started: bool = True):
        self.id = turn_id
        self.thread_id = thread_id
        self.started_at = started_at
        self.ended_at: float | int | None = None
        self.status = "running" if started else "completed"
        self.started = started
        self.tokens: dict[str, int] | None = None
        self.reasons: list[str] = []
        self.pricing = _PricingMetadata()
        if not started:
            self.mark("未记录到本轮开始。")

    def mark(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def add(self, amount: dict[str, int]) -> None:
        if self.tokens is None:
            self.tokens = dict(amount)
        else:
            for key, value in amount.items():
                self.tokens[key] += value

    def export(self) -> dict[str, Any]:
        reasons = list(self.reasons)
        if self.tokens is None:
            reasons.append("本轮暂无可用的 token 记录。")
        note = " ".join(reasons)
        if not note:
            note = (
                "仅统计该任务自身，子代理另列；本轮仍在运行。"
                if self.status == "running"
                else "仅统计该任务自身，子代理另列。"
            )
        return {
            "id": self.id,
            "threadId": self.thread_id,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "status": self.status,
            "quality": "unavailable" if self.tokens is None else ("partial" if reasons else "complete"),
            "note": note,
            "tokens": self.tokens,
            **self.pricing.export(),
        }


class _Reader:
    def __init__(self, expected_thread_id: str):
        self.thread_id = expected_thread_id
        self.verified = False
        self.turns: dict[str, _Turn] = {}
        self.active: _Turn | None = None
        self.previous_total: dict[str, int] | None = None
        self.baseline_valid = False
        self.warnings: list[str] = []
        self.pending_contexts: dict[str, _PricingMetadata] = {}
        self.pending_context_overflow = False

    def warn(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    def gap(self, reason: str) -> None:
        self.baseline_valid = False
        self.warn(reason)
        if self.active is not None:
            self.active.mark(reason)

    def handle(self, record: Any) -> None:
        if not isinstance(record, dict):
            self.gap("已跳过无法识别的记录。")
            return
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "session_meta":
            if not isinstance(payload, dict) or payload.get("id") != self.thread_id:
                raise UsageLogError("用量记录与指定任务不匹配。")
            self.verified = True
            return
        if kind == "turn_context":
            if not self.verified:
                raise UsageLogError("任务上下文之前缺少任务身份记录。")
            if isinstance(payload, dict):
                self.context(payload)
            return
        # Ignore all messages, reasoning, tool bodies, and unknown record types.
        if kind == "compacted":
            if self.verified:
                self.gap("上下文压缩改变了统计基线，用量可能不完整。")
            return
        if kind != "event_msg" or not isinstance(payload, dict):
            return
        event = payload.get("type")
        if event not in {"task_started", "task_complete", "token_count", "context_compacted", "thread_compacted", "turn_aborted"}:
            return
        if not self.verified:
            raise UsageLogError("用量事件之前缺少任务身份记录。")
        timestamp = _timestamp(record.get("timestamp"))
        if event in {"context_compacted", "thread_compacted"}:
            self.gap("上下文压缩改变了统计基线，用量可能不完整。")
        elif event == "task_started":
            self.start(payload, timestamp)
        elif event in {"task_complete", "turn_aborted"}:
            self.finish(payload, timestamp, interrupted=event == "turn_aborted")
        elif event == "token_count":
            self.usage(payload)

    def context(self, payload: dict[str, Any]) -> None:
        # Deliberately select only these three context fields. In particular,
        # never use prompts, instructions, cwd, or inferred account defaults.
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            return
        model = _identifier(payload.get("model"))
        service_tier = _identifier(payload.get("service_tier"))
        turn = self.turns.get(turn_id)
        if turn is not None:
            turn.pricing.observe(model, service_tier)
            return
        metadata = self.pending_contexts.get(turn_id)
        if metadata is None:
            if len(self.pending_contexts) >= MAX_PENDING_CONTEXTS:
                self.pending_contexts.pop(next(iter(self.pending_contexts)))
                self.pending_context_overflow = True
                # An evicted ID could appear again. Preserve bounded memory and
                # avoid later claiming complete metadata after losing evidence.
                for pending in self.pending_contexts.values():
                    pending.incomplete = True
                self.warn("待归属的模型记录过多，后续计价信息可能不完整。")
            metadata = _PricingMetadata(incomplete=self.pending_context_overflow)
            self.pending_contexts[turn_id] = metadata
        metadata.observe(model, service_tier)

    def attach_context(self, turn: _Turn) -> None:
        metadata = self.pending_contexts.pop(turn.id, None)
        turn.pricing = metadata if metadata is not None else _PricingMetadata(incomplete=self.pending_context_overflow)

    def start(self, payload: dict[str, Any], timestamp: float | int | None) -> None:
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            self.gap("已跳过缺少有效轮次编号的开始事件。")
            if self.active is not None:
                self.active.status = "interrupted"
                self.active.ended_at = timestamp
            self.active = None
            return
        if turn_id in self.turns:
            # Replayed starts never clear the baseline or reopen completed turns.
            return
        if self.active is not None:
            self.active.mark("记录到下一轮开始，但未记录到本轮结束。")
            self.active.status = "interrupted"
            self.active.ended_at = timestamp
        self.active = _Turn(turn_id, self.thread_id, timestamp)
        self.attach_context(self.active)
        self.turns[turn_id] = self.active

    def finish(self, payload: dict[str, Any], timestamp: float | int | None, *, interrupted: bool) -> None:
        turn_id = _identifier(payload.get("turn_id"))
        if turn_id is None:
            self.gap("结束事件缺少有效轮次编号，已暂停归属统计。")
            # Its identity is unknown, so further unlabelled snapshots cannot
            # safely be assigned to the previously active turn.
            if self.active is not None:
                self.active.status = "interrupted"
                self.active.ended_at = timestamp
            self.active = None
            return
        turn = self.turns.get(turn_id)
        if turn is None:
            turn = _Turn(turn_id, self.thread_id, None, started=False)
            self.attach_context(turn)
            self.turns[turn_id] = turn
        elif turn.status != "running":
            return
        turn.status = "interrupted" if interrupted else "completed"
        turn.ended_at = timestamp
        if self.active is turn:
            self.active = None

    def usage(self, payload: dict[str, Any]) -> None:
        info = payload.get("info")
        if info is None:
            # Quota-only notifications can carry null info. It is not zero usage.
            return
        if not isinstance(info, dict):
            self.gap("已跳过无效 token 记录，用量可能不完整。")
            return
        total = _coherent(info.get("total_token_usage"))
        last = _coherent(info.get("last_token_usage"))
        if total is None:
            self.gap("已跳过无效 token 记录，用量可能不完整。")
            return

        turn = self.active
        explicit_turn = payload.get("turn_id")
        explicit_thread = payload.get("thread_id")
        if explicit_thread is not None and explicit_thread != self.thread_id:
            self.warn("已排除其他任务的 token 记录。")
            if turn is not None:
                turn.mark("已排除其他任务的 token 记录。")
            # Another thread has its own cumulative baseline. It must never
            # replace this thread's baseline, even if its counters are valid.
            return
        if explicit_turn is not None and (turn is None or explicit_turn != turn.id):
            if turn is not None:
                turn.mark("已排除其他轮次的 token 记录。")
            turn = None

        repeated = total == self.previous_total
        if turn is None:
            self.warn("已排除无法归属到进行中轮次的用量。")
        elif repeated:
            if self.baseline_valid:
                turn.add({key: 0 for key in total})
            else:
                turn.mark("缺少完整的起始 token 基线。")
        elif self.baseline_valid and self.previous_total is not None:
            amount = _delta(total, self.previous_total)
            if amount is not None:
                turn.add(amount)
            else:
                turn.mark("token 计数重置或变化异常，用量可能不完整。")
                self.warn("检测到 token 计数重置或差值异常。")
                fallback = _bounded_last(last, total)
                if fallback is not None:
                    turn.add(fallback)
        else:
            fallback = _bounded_last(last, total)
            if fallback is not None and total == fallback and turn.started:
                turn.add(fallback)
            else:
                turn.mark("缺少完整起始基线，仅统计已观察到的增量。")
                if fallback is not None:
                    turn.add(fallback)
        self.previous_total = total
        self.baseline_valid = True

    def result(self) -> dict[str, Any]:
        if not self.verified:
            raise UsageLogError("用量记录中缺少匹配的任务身份。")
        return {
            "threadId": self.thread_id,
            "turns": [turn.export() for turn in self.turns.values()],
            "warnings": self.warnings,
            "sourceNote": _SOURCE_NOTE,
        }


def read_usage_log(path: str | os.PathLike[str], expected_thread_id: str) -> dict[str, Any]:
    """Read exactly one caller-supplied file and return sanitized usage summaries.

    Invalid paths, foreign session IDs, and oversized files raise UsageLogError.
    A missing/invalid counter yields a partial result, never invented zero usage.
    ISO timestamps are converted to Unix seconds. Symlink leaf paths are refused.
    """
    if _identifier(expected_thread_id) is None:
        raise UsageLogError("需要有效的任务编号。")
    try:
        file_path = Path(path)
    except (TypeError, ValueError):
        raise UsageLogError("需要用量记录的文件路径。") from None
    if file_path.suffix.lower() != ".jsonl":
        raise UsageLogError("用量记录必须是 JSONL 文件。")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(file_path, flags)
    except (OSError, ValueError):
        raise UsageLogError("无法打开指定的用量记录文件。") from None
    reader = _Reader(expected_thread_id)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode):
                raise UsageLogError("用量记录路径必须指向普通文件。")
            if details.st_size > MAX_LOG_BYTES:
                raise UsageLogError("用量记录超过 128 MiB 的读取上限。")
            bytes_read = 0
            while True:
                raw = stream.readline(MAX_LINE_BYTES + 1)
                if not raw:
                    break
                bytes_read += len(raw)
                if bytes_read > MAX_LOG_BYTES:
                    raise UsageLogError("用量记录超过 128 MiB 的读取上限。")
                if len(raw) > MAX_LINE_BYTES:
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(MAX_LINE_BYTES + 1)
                        bytes_read += len(raw)
                        if bytes_read > MAX_LOG_BYTES:
                            raise UsageLogError("用量记录超过 128 MiB 的读取上限。")
                    reader.gap("已跳过过大的单条记录，用量可能不完整。")
                    continue
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                    if not raw.endswith(b"\n"):
                        reader.gap("末条记录尚未写完，请稍后刷新。")
                        break
                    reader.gap("已跳过格式异常的记录，用量可能不完整。")
                    continue
                reader.handle(record)
    except OSError:
        raise UsageLogError("无法读取指定的用量记录文件。") from None
    return reader.result()


__all__ = ["UsageLogError", "read_usage_log"]
