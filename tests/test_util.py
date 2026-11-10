"""Tests for eta.util module."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from eta.util import (
    EtaError,
    CommandResult,
    run,
    utc_now_iso,
    utc_iso,
    parse_bool,
    atomic_write,
    atomic_write_json,
    read_json,
    read_jsonl,
    file_mode,
    ensure_dir,
    check_safe_unit,
    check_safe_name,
    check_hostname,
    format_duration,
    human_bytes,
    uptime_seconds,
    iter_sorted,
)


class TestCommandResult:
    def test_ok_property(self):
        result = CommandResult(["echo", "test"], 0, "stdout", "stderr")
        assert result.ok is True

    def test_not_ok_property(self):
        result = CommandResult(["false"], 1, "", "error")
        assert result.ok is False

    def test_output_property(self):
        result = CommandResult(["echo", "test"], 0, "stdout", "stderr")
        assert result.output == "stdout\nstderr"


class TestRun:
    def test_run_success(self):
        result = run(["echo", "hello"], timeout=5.0)
        assert result.ok is True
        assert "hello" in result.stdout

    def test_run_failure(self):
        result = run(["false"], timeout=5.0)
        assert result.ok is False
        assert result.returncode == 1

    def test_run_not_found(self):
        result = run(["/nonexistent/command/that/does/not/exist"], timeout=5.0)
        assert result.ok is False
        assert result.returncode == 127

    def test_run_with_env(self):
        result = run(["printenv", "TEST_VAR"], env={"TEST_VAR": "test_value"}, timeout=5.0)
        assert result.ok is True
        assert "test_value" in result.stdout

    def test_run_with_input(self):
        result = run(["cat"], input_text="stdin content", timeout=5.0)
        assert result.ok is True
        assert "stdin content" in result.stdout


class TestTimeFunctions:
    def test_utc_now_iso_format(self):
        iso = utc_now_iso()
        assert len(iso) == 20  # YYYY-MM-DDTHH:MM:SSZ
        assert iso.endswith("Z")
        assert iso[10] == "T"

    def test_utc_iso_format(self):
        ts = 1700000000.0  # 2023-11-14 22:13:20 UTC
        iso = utc_iso(ts)
        assert iso == "2023-11-14T22:13:20Z"

    def test_uptime_seconds(self):
        uptime = uptime_seconds()
        assert uptime >= 0.0


class TestParseBool:
    def test_bool_true(self):
        assert parse_bool(True) is True
        assert parse_bool(1) is True
        assert parse_bool("true") is True
        assert parse_bool("True") is True
        assert parse_bool("YES") is True
        assert parse_bool("on") is True
        assert parse_bool("1") is True

    def test_bool_false(self):
        assert parse_bool(False) is False
        assert parse_bool(0) is False
        assert parse_bool("false") is False
        assert parse_bool("False") is False
        assert parse_bool("NO") is False
        assert parse_bool("off") is False
        assert parse_bool("0") is False

    def test_bool_invalid(self):
        with pytest.raises(ValueError):
            parse_bool("invalid")
        with pytest.raises(ValueError):
            parse_bool("maybe")


class TestAtomicWrite:
    def test_atomic_write_str(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            atomic_write(path, "hello world")
            assert path.read_text() == "hello world"

    def test_atomic_write_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.bin"
            atomic_write(path, b"binary data")
            assert path.read_bytes() == b"binary data"

    def test_atomic_write_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "test.txt"
            atomic_write(path, "nested")
            assert path.read_text() == "nested"

    def test_atomic_write_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            atomic_write(path, "test", mode=0o644)
            mode = file_mode(path)
            assert mode & 0o777 == 0o644


class TestAtomicWriteJson:
    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            atomic_write_json(path, {"key": "value", "number": 42})
            data = json.loads(path.read_text())
            assert data == {"key": "value", "number": 42}

    def test_atomic_write_json_sorted_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            atomic_write_json(path, {"z": 1, "a": 2})
            content = path.read_text()
            # Keys should be sorted
            assert content.index("a") < content.index("z")


class TestReadJson:
    def test_read_json_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            path.write_text('{"key": "value"}')
            result = read_json(path)
            assert result == {"key": "value"}

    def test_read_json_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.json"
            result = read_json(path, default="default")
            assert result == "default"

    def test_read_json_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid.json"
            path.write_text("not json")
            result = read_json(path, default="default")
            assert result == "default"


class TestReadJsonl:
    def test_read_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
            result = read_jsonl(path)
            assert result == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_read_jsonl_with_tail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            path.write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
            result = read_jsonl(path, tail=2)
            assert result == [{"b": 2}, {"c": 3}]

    def test_read_jsonl_skips_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            path.write_text('{"a": 1}\ninvalid\n{"b": 2}\n')
            result = read_jsonl(path)
            assert result == [{"a": 1}, {"b": 2}]


class TestFileMode:
    def test_file_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.txt"
            path.write_text("test")
            os.chmod(path, 0o644)
            mode = file_mode(path)
            assert mode == 0o644


class TestEnsureDir:
    def test_ensure_dir_creates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "newdir"
            result = ensure_dir(path)
            assert result.exists()
            assert result.is_dir()
            assert result == path

    def test_ensure_dir_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            result = ensure_dir(path)
            assert result == path


class TestValidation:
    def test_check_safe_unit_valid(self):
        assert check_safe_unit("eta-admin.service") == "eta-admin.service"
        assert check_safe_unit("my-unit@.service") == "my-unit@.service"

    def test_check_safe_unit_invalid(self):
        with pytest.raises(EtaError):
            check_safe_unit("")
        with pytest.raises(EtaError):
            check_safe_unit("../evil")
        with pytest.raises(EtaError):
            check_safe_unit("evil/unit")
        with pytest.raises(EtaError):
            check_safe_unit("a" * 130)

    def test_check_safe_name_valid(self):
        assert check_safe_name("service") == "service"
        assert check_safe_name("my-service") == "my-service"
        assert check_safe_name("s1") == "s1"

    def test_check_safe_name_invalid(self):
        with pytest.raises(EtaError):
            check_safe_name("")
        with pytest.raises(EtaError):
            check_safe_name("Service")  # capital
        with pytest.raises(EtaError):
            check_safe_name("my_service")  # underscore
        with pytest.raises(EtaError):
            check_safe_name("-service")  # starts with hyphen

    def test_check_hostname_valid(self):
        assert check_hostname("eta") == "eta"
        assert check_hostname("eta-server") == "eta-server"
        assert check_hostname("ETA") == "eta"  # lowercased

    def test_check_hostname_invalid(self):
        with pytest.raises(EtaError):
            check_hostname("")
        with pytest.raises(EtaError):
            check_hostname("-eta")
        with pytest.raises(EtaError):
            check_hostname("eta-")
        with pytest.raises(EtaError):
            check_hostname("a" * 64)  # too long
        with pytest.raises(EtaError):
            check_hostname("eta_server")  # underscore


class TestFormatDuration:
    def test_format_duration_ms(self):
        assert format_duration(0.001) == "1ms"
        assert format_duration(0.5) == "500ms"

    def test_format_duration_seconds(self):
        assert format_duration(1.0) == "1.00s"
        assert format_duration(30.5) == "30.50s"
        assert format_duration(59.99) == "59.99s"

    def test_format_duration_minutes(self):
        assert format_duration(60) == "1m00s"
        assert format_duration(90) == "1m30s"
        assert format_duration(3599) == "59m59s"

    def test_format_duration_hours(self):
        assert format_duration(3600) == "1h00m"
        assert format_duration(7200) == "2h00m"
        assert format_duration(3661) == "1h01m"


class TestHumanBytes:
    def test_human_bytes(self):
        assert human_bytes(0) == "0B"
        assert human_bytes(512) == "512B"
        assert human_bytes(1024) == "1.0KiB"
        assert human_bytes(1536) == "1.5KiB"
        assert human_bytes(1024 * 1024) == "1.0MiB"
        assert human_bytes(1024 * 1024 * 1024) == "1.0GiB"
        assert human_bytes(1024 * 1024 * 1024 * 1024) == "1.0TiB"


class TestIterSorted:
    def test_iter_sorted(self):
        assert iter_sorted(["z", "a", "m"]) == ["a", "m", "z"]
        assert iter_sorted([]) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])