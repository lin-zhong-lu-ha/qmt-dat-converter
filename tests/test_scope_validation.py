"""End-to-end checks for date-scoped OHLC validation and source history."""

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from qmt_dat_converter.engine import run
from qmt_dat_converter.model import Config


OLD_TIMESTAMP = 765388800  # 1994-04-04 in Beijing
NEXT_OLD_TIMESTAMP = OLD_TIMESTAMP + 86400


def _source(tmp_path):
    root = tmp_path / "install" / "datadir"
    return root / "SH" / "86400" / "600000.DAT"


def _config(source, tmp_path, **changes):
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",))
    return replace(config, **changes)


def _target(config, year="2026", month="09"):
    return config.output / "data" / "qmt-daily-monthly" / year / month / "SH.parquet"


def _old_row(row, *, invalid=False, timestamp=OLD_TIMESTAMP):
    older = list(row)
    older[0] = timestamp
    if invalid:
        older[1:5] = [6060, 6000, 5610, 5700]
    return older


def _issues(report, reason):
    return [issue for issue in report["issues"] if issue["reason"] == reason]


def _cached_days(config):
    with sqlite3.connect(config.output / ".state" / "cache.sqlite3") as db:
        return {day for (day,) in db.execute("SELECT day FROM days")}


def test_bounded_full_publishes_valid_day_and_reports_older_bad_ohlc(tmp_path, dat_file, row):
    source = _source(tmp_path)
    dat_file(source, _old_row(row, invalid=True), row)
    config = _config(source, tmp_path, mode="full", start="20260922", end="20260922")

    report = run(config)

    assert report["status"] == "complete"
    assert report["counts"]["written_partitions"] == 1
    assert report["verification"]["conversion_consistency"]["status"] == "verified"
    assert report["verification"]["conversion_consistency"]["validation_scope"] == {
        "start": "2026-09-22", "end": "2026-09-22"
    }
    assert report["scope"]["start"] == "2026-09-22"
    warning, = _issues(report, "invalid-ohlc-outside-scope")
    assert warning["category"] == "warning"
    assert warning["code"] == "600000.SH"
    assert warning["count"] == 1
    assert warning["rows"][0]["date"] == "1994-04-04"
    assert warning["rows"][0]["open"] == 6.06
    assert warning["rows"][0]["high"] == 6.0
    assert pq.read_table(_target(config))["trade_date"].to_pylist() == ["20260922"]
    assert not _target(config, "1994", "04").exists()
    assert _cached_days(config) == {"20260922"}
    with sqlite3.connect(config.output / ".state" / "cache.sqlite3") as db:
        assert db.execute("SELECT complete FROM sources").fetchone()[0] == 0
    assert json.loads(Path(report["paths"]["report"]).read_text(encoding="utf-8")) == report


def test_bounded_verify_passes_but_unbounded_incremental_revalidates_bad_history(
    tmp_path, dat_file, row
):
    source = _source(tmp_path)
    dat_file(source, _old_row(row, invalid=True), row)
    bounded = _config(source, tmp_path, mode="full", start="20260922", end="20260922")
    assert run(bounded)["status"] == "complete"
    target = _target(bounded)
    published = target.read_bytes()

    verified = run(bounded, action="verify")
    assert verified["status"] == "complete"
    assert verified["counts"]["content_verified_partitions"] == 1
    assert _issues(verified, "invalid-ohlc-outside-scope")
    assert target.read_bytes() == published

    unbounded = run(replace(bounded, mode="incremental", start=None, end=None))
    assert unbounded["status"] == "partial"
    assert unbounded["counts"]["stat_reused_files"] == 0
    assert unbounded["counts"]["content_revalidated_files"] == 1
    error, = _issues(unbounded, "invalid-ohlc")
    assert error["category"] == "error"
    assert error["rows"][0]["date"] == "1994-04-04"
    assert target.read_bytes() == published
    assert _cached_days(bounded) == {"20260922"}


def test_expanding_bounds_to_bad_day_blocks_and_preserves_valid_output(tmp_path, dat_file, row):
    source = _source(tmp_path)
    dat_file(source, _old_row(row, invalid=True), row)
    bounded = _config(source, tmp_path, mode="full", start="20260922", end="20260922")
    assert run(bounded)["status"] == "complete"
    target = _target(bounded)
    published = target.read_bytes()

    expanded = run(replace(bounded, mode="range", start="19940404"))

    assert expanded["status"] == "partial"
    assert expanded["counts"]["written_partitions"] == 0
    error, = _issues(expanded, "invalid-ohlc")
    assert error["category"] == "error"
    assert error["code"] == "600000.SH"
    assert error["rows"][0]["date"] == "1994-04-04"
    assert error["rows"][0]["open"] == 6.06
    assert target.read_bytes() == published
    assert not _target(bounded, "1994", "04").exists()


def test_narrow_conversion_preserves_cached_older_day_and_detects_its_real_removal(
    tmp_path, dat_file, row
):
    source = _source(tmp_path)
    old = _old_row(row)
    dat_file(source, old, row)
    config = _config(source, tmp_path, mode="full")
    assert run(config)["status"] == "complete"
    old_target = _target(config, "1994", "04")
    old_publication = old_target.read_bytes()
    current_target = _target(config)
    narrow = replace(config, mode="range", start="20260922", end="20260922")
    corrected = list(row)
    corrected[4] = 7200
    dat_file(source, old, corrected)

    updated = run(narrow)

    assert updated["status"] == "complete"
    assert not _issues(updated, "removed-days")
    assert _cached_days(config) == {"19940404", "20260922"}
    assert old_target.read_bytes() == old_publication
    assert pq.read_table(current_target)["close"].to_pylist() == [7.2]
    current_publication = current_target.read_bytes()

    dat_file(source, _old_row(row, timestamp=NEXT_OLD_TIMESTAMP), corrected)
    removed = run(narrow)
    assert removed["status"] == "partial"
    assert _issues(removed, "removed-days")
    assert removed["counts"]["written_partitions"] == 0
    assert _cached_days(config) == {"19940404", "20260922"}
    assert old_target.read_bytes() == old_publication
    assert current_target.read_bytes() == current_publication


def test_in_range_ohlc_blocks_other_code_in_shared_partition(tmp_path, dat_file, row):
    source = _source(tmp_path)
    other = source.with_name("600001.DAT")
    dat_file(source, row)
    dat_file(other, row)
    config = _config(source, tmp_path, mode="full", start="20260922", end="20260922")
    assert run(config)["status"] == "complete"
    target = _target(config)
    published = target.read_bytes()
    invalid = list(row)
    invalid[1] = 7400
    corrected = list(row)
    corrected[4] = 7200
    dat_file(source, invalid)
    dat_file(other, corrected)

    report = run(config)

    assert report["status"] == "partial"
    error, = _issues(report, "invalid-ohlc")
    assert error["category"] == "error"
    assert error["code"] == "600000.SH"
    assert error["rows"][0]["date"] == "2026-09-22"
    assert error["rows"][0]["open"] == 7.4
    assert error["rows"][0]["high"] == 7.35
    assert report["counts"]["written_partitions"] == 0
    assert target.read_bytes() == published
    assert pq.read_table(target)["close"].to_pylist() == [7.25, 7.25]


def test_bad_new_month_blocks_partition_even_when_source_cache_has_only_old_month(
    tmp_path, dat_file, row
):
    source = _source(tmp_path)
    other = source.with_name("600001.DAT")
    old = _old_row(row)
    dat_file(source, old)
    dat_file(other, old)
    config = _config(source, tmp_path, mode="full")
    assert run(config)["status"] == "complete"
    assert _cached_days(config) == {"19940404"}
    current_target = _target(config)
    invalid = list(row)
    invalid[1] = 7400
    dat_file(source, old, invalid)
    dat_file(other, old, row)
    narrow = replace(config, mode="range", start="20260922", end="20260922")

    report = run(narrow)

    assert report["status"] == "partial"
    error, = _issues(report, "invalid-ohlc")
    assert error["code"] == "600000.SH"
    assert error["rows"][0]["date"] == "2026-09-22"
    blocked, = _issues(report, "partition-unpublishable")
    assert blocked["blocking_codes"] == ["600000.SH"]
    assert report["counts"]["written_partitions"] == 0
    assert not current_target.exists()
    assert _cached_days(config) == {"19940404"}


def test_interrupted_bounded_replacement_recovers_with_same_validation_scope(
    tmp_path, dat_file, row, monkeypatch
):
    source = _source(tmp_path)
    old = _old_row(row, invalid=True)
    dat_file(source, old, row)
    config = _config(source, tmp_path, mode="full", start="20260922", end="20260922")
    assert run(config)["status"] == "complete"
    target = _target(config)
    corrected = list(row)
    corrected[4] = 7200
    dat_file(source, old, corrected)
    original_replace = os.replace
    interrupted = False

    def replace_then_interrupt(src, dst):
        nonlocal interrupted
        original_replace(src, dst)
        if str(dst) == str(target) and not interrupted:
            interrupted = True
            raise OSError("simulated crash after replacement")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_interrupt)
        failed = run(config)
    assert interrupted
    assert failed["status"] == "partial"

    recovered = run(config)
    assert recovered["status"] == "complete"
    assert recovered["counts"]["recovered_partitions"] == 1
    assert recovered["counts"]["recovery_revalidated_files"] == 1
    assert _issues(recovered, "invalid-ohlc-outside-scope")
    assert pq.read_table(target)["close"].to_pylist() == [7.2]
    assert not list((config.output / ".staging").rglob("*.parquet"))
