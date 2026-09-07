"""The CLI must run offline by default and refuse implicit live configuration."""

import json
import subprocess
import sys
from pathlib import Path

from nanobot.evaluation.__main__ import load_suite


def test_basic_suite_cli(tmp_path):
    suite = tmp_path / "smoke.json"
    suite.write_text(json.dumps({"cases": [{
        "schema_version": 1, "id": "cli-smoke", "description": "CLI smoke",
        "user_inputs": ["PRIVATE-CLI-SYNTHETIC Reply pong"], "scripted_responses": [{"content": "pong"}],
        "allowed_tools": [], "verifiers": [{"name": "answer_contains", "expected": "pong"}],
    }]}), encoding="utf-8")
    result = subprocess.run([
        sys.executable, "-m", "nanobot.evaluation", "run", "--suite", str(suite),
        "--mode", "baseline", "--output-root", str(tmp_path / "out"),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "passed"
    assert "PRIVATE-CLI-SYNTHETIC" not in result.stdout + result.stderr


def test_live_refuses_without_explicit_test_limits(tmp_path):
    result = subprocess.run([
        sys.executable, "-m", "nanobot.evaluation", "run", "--suite", "missing.json",
        "--output-root", str(tmp_path), "--live",
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert "live" in result.stderr.lower()


def test_cli_loads_versioned_matrix_fixtures():
    fixture_dir = Path(__file__).parent / "fixtures"
    assert len(load_suite(fixture_dir / "context_matrix.json")) == 12
    assert len(load_suite(fixture_dir / "memory_dependency.json")) == 36
    assert len(load_suite(fixture_dir)) == 59


def test_cli_compares_single_result_and_emits_markdown(tmp_path):
    result = {
        "case_id": "sample", "mode": "baseline", "status": "passed", "verdicts": [],
        "metrics": {"actual_input_tokens": None}, "trace_ref": "unused",
        "provenance": {"case_hash": "same", "config_hash": "same", "model": "scripted",
                       "temperature": 0, "render_version": "v1", "dependencies": {}, "mechanism_only": True},
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    for format_name in ("json", "markdown"):
        process = subprocess.run([
            sys.executable, "-m", "nanobot.evaluation", "compare", "--baseline", str(path),
            "--candidate", str(path), "--format", format_name,
        ], capture_output=True, text=True, timeout=30)
        assert process.returncode == 0, process.stderr
        if format_name == "json":
            report = json.loads(process.stdout)
            assert report["baseline"]["task_success_rate"] == 1
        else:
            assert "unknown" in process.stdout and "sample" in process.stdout
