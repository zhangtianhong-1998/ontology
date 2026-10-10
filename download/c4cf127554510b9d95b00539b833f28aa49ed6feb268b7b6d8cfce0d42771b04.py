"""Progress output remains bounded and separate from program results."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ontology_r2 import progress
from ontology_r2.storage import read_yaml


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_default_stream_is_stderr_and_stdout_untouched(capsys):
    with progress.ProgressReporter().task("Import", total=2) as task:
        task.advance(2)
    output = capsys.readouterr()
    assert output.out == ""
    assert "Import: 2/2 · done" in output.err


def test_disabled_reporter_emits_nothing(capsys):
    with progress.ProgressReporter({"enabled": False}).task("Quiet", total=1) as task:
        task.advance(detail="secret")
        task.note("still quiet")
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_note_is_visible_before_a_slow_step():
    stream = io.StringIO()
    with progress.ProgressReporter(stream=stream).task("Build", total=1) as task:
        task.note("planning table A")
        assert "planning table A" in stream.getvalue()


def test_cli_progress_uses_stderr_and_can_be_disabled(tmp_path):
    project = Path(__file__).resolve().parents[1]
    base = [sys.executable, "-m", "ontology_r2.cli", "build", "--config",
            str(project / "config/runtime.mock.yaml"), "--profile", "E2L"]
    env = {**os.environ, "PYTHONPATH": str(project / "code")}
    visible = subprocess.run([*base, "--output", str(tmp_path / "visible")],
                             cwd=project, env=env, capture_output=True, text=True, check=True)
    result = json.loads(visible.stdout)
    assert result["status"] in ("complete", "partial")
    assert "导入 CSV:" in visible.stderr
    assert "本体增量构建:" in visible.stderr
    assert "导出 YAML:" in visible.stderr
    construction = read_yaml(tmp_path / "visible/construction.yaml")
    assert construction["iteration_policy"] == {
        "table_passes": 1, "units": len(construction["table_order"]),
        "max_attempts_per_unit": 2, "revisit_rejected_units": False}
    assert result["incremental_iteration_policy"] == construction["iteration_policy"]
    hidden = subprocess.run([*base, "--no-progress", "--output", str(tmp_path / "hidden")],
                            cwd=project, env=env, capture_output=True, text=True, check=True)
    assert json.loads(hidden.stdout)["status"] in ("complete", "partial")
    assert "导入 CSV:" not in hidden.stderr


def test_small_task_throttles_and_always_emits_final_counts(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(progress, "_monotonic", clock)
    stream = io.StringIO()
    with progress.ProgressReporter({"min_interval_seconds": 2}, stream).task("Tables", total=3) as task:
        task.advance(detail="first")
        clock.now = 1
        task.advance()
        assert len(stream.getvalue().splitlines()) == 1
        clock.now = 2
        task.advance()
        assert len(stream.getvalue().splitlines()) == 2
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert "Tables: 3/3 · done" in lines[-1]


def test_large_task_reports_milestones_not_every_row(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(progress, "_monotonic", clock)
    stream = io.StringIO()
    with progress.ProgressReporter(stream=stream).task("Records", total=1000) as task:
        for _ in range(1000):
            task.advance()
    lines = stream.getvalue().splitlines()
    assert len(lines) == 12  # start, ten 10% milestones, final
    assert "100/1,000" in lines[1]
    assert "1,000/1,000 · done" in lines[-1]


def test_unknown_total_reports_sparsely_and_error_exit(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(progress, "_monotonic", clock)
    stream = io.StringIO()
    with pytest.raises(RuntimeError, match="bad row"):
        with progress.ProgressReporter(stream=stream).task("Scan") as task:
            task.advance(50)
            clock.now = 31
            task.advance(1)
            raise RuntimeError("bad row")
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert "Scan: 51 · failed (RuntimeError: bad row)" in lines[-1]


def test_tty_uses_carriage_return_and_shows_rate_eta(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(progress, "_monotonic", clock)

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    stream = Terminal()
    with progress.ProgressReporter({"min_interval_seconds": 1}, stream).task("Links", total=10) as task:
        clock.now = 2
        task.advance(5)
    output = stream.getvalue()
    assert "\r" in output
    assert "2.5/s ETA 2s" in output
    assert output.endswith("\n")


@pytest.mark.parametrize("config", [{"enabled": 1}, {"min_interval_seconds": 0}, {"min_interval_seconds": float("nan")}])
def test_invalid_configuration_fails_visibly(config):
    with pytest.raises(ValueError):
        progress.ProgressReporter(config)
