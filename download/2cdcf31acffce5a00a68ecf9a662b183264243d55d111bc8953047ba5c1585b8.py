"""Small, dependency-free progress reporting for local batch jobs."""

from __future__ import annotations

import math
import sys
import time
from typing import TextIO


_monotonic = time.monotonic


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def _short(value: object, limit: int = 100) -> str:
    line = " ".join(str(value).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


class ProgressReporter:
    """Create task progress on stderr without changing application output."""

    def __init__(self, config: dict | None = None, stream: TextIO | None = None):
        config = config or {}
        self.enabled = config.get("enabled", True)
        if not isinstance(self.enabled, bool):
            raise ValueError("progress.enabled must be a boolean")
        interval = config.get("min_interval_seconds", 1.0)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError("progress.min_interval_seconds must be a positive number")
        self.min_interval_seconds = float(interval)
        self.stream = stream if stream is not None else sys.stderr
        self.tty = self.enabled and callable(getattr(self.stream, "isatty", None)) and self.stream.isatty()

    def task(self, label: str, total: int | None = None) -> ProgressTask:
        return ProgressTask(self, label, total)


class ProgressTask:
    def __init__(self, reporter: ProgressReporter, label: str, total: int | None):
        if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
            raise ValueError("progress total must be a non-negative integer or None")
        self.reporter = reporter
        self.label = _short(label, 80)
        self.total = total
        self.count = 0
        self.detail = ""
        self.started_at = _monotonic()
        self.last_emit_at = self.started_at
        self.last_bucket = 0
        self.last_line_width = 0
        self.closed = False
        if reporter.enabled:
            self._emit("started")

    def __enter__(self) -> ProgressTask:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close(error=exc)
        return False

    def advance(self, n: int = 1, detail: str | None = None) -> None:
        if self.closed:
            raise RuntimeError("progress task is closed")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("progress advance must be a non-negative integer")
        self.count += n
        if detail is not None:
            self.detail = _short(detail)
        if not self.reporter.enabled:
            return
        now = _monotonic()
        elapsed_since_emit = now - self.last_emit_at
        if self.reporter.tty:
            should_emit = elapsed_since_emit >= self.reporter.min_interval_seconds
        elif self.total is None:
            should_emit = elapsed_since_emit >= 30
        elif self.total > 30:
            bucket = min(10, self.count * 10 // self.total)
            should_emit = bucket > self.last_bucket or elapsed_since_emit >= 30
        else:
            should_emit = elapsed_since_emit >= self.reporter.min_interval_seconds
        if should_emit:
            self._emit("running", now=now)

    def note(self, detail: str) -> None:
        if self.closed:
            raise RuntimeError("progress task is closed")
        self.detail = _short(detail)
        if self.reporter.enabled:
            self._emit("running")

    def close(self, error: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        if self.reporter.enabled:
            status = f"failed ({type(error).__name__}: {_short(error, 80)})" if error is not None else "done"
            self._emit(status)

    def _emit(self, status: str, *, now: float | None = None) -> None:
        now = _monotonic() if now is None else now
        elapsed = max(0.0, now - self.started_at)
        rate = self.count / elapsed if elapsed > 0 else 0.0
        count = f"{self.count:,}" if self.total is None else f"{self.count:,}/{self.total:,}"
        if self.reporter.tty and self.total is not None:
            filled = min(20, self.count * 20 // self.total) if self.total else 20
            bar = "█" * filled + "░" * (20 - filled)
            progress = f"[{bar}] {count}"
        else:
            progress = count
        stats = f"{_duration(elapsed)} {rate:.1f}/s"
        if self.total is not None and rate > 0 and self.count < self.total:
            stats += f" ETA {_duration((self.total - self.count) / rate)}"
        detail = f" · {self.detail}" if self.detail else ""
        line = f"{self.label}: {progress} · {status} · {stats}{detail}"
        if self.reporter.tty:
            padding = " " * max(0, self.last_line_width - len(line))
            self.reporter.stream.write("\r" + line + padding + ("\n" if self.closed else ""))
            self.last_line_width = len(line)
        else:
            self.reporter.stream.write(line + "\n")
        self.reporter.stream.flush()
        self.last_emit_at = now
        if self.total:
            self.last_bucket = min(10, self.count * 10 // self.total)
