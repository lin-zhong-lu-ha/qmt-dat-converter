import json
import os
from dataclasses import replace

import pyarrow.parquet as pq
import pytest

from qmt_dat_converter.engine import run
from qmt_dat_converter.model import Config


@pytest.fixture
def setup(tmp_path, dat_file, row):
    root = tmp_path / "install" / "datadir"
    for market in ("SH", "SZ"):
        (root / market / "86400").mkdir(parents=True)
        (root / market / "60").mkdir(parents=True)
    source = root / "SH" / "86400" / "600000.DAT"
    later = list(row)
    later[0] += 86400
    dat_file(source, row, later)
    config = Config(root, tmp_path / "output", periods=("1d",))
    target = config.output / "data/qmt-daily-monthly/2026/09/SH.parquet"
    return config, source, target


def reasons(report):
    return " ".join(issue["reason"] for issue in report["issues"])


def test_full_then_incremental_reuses_without_rewriting(setup):
    config, _, target = setup
    first = run(replace(config, mode="full"))
    assert first["status"] == "complete"
    assert first["counts"]["new_days"] == 2
    before = target.stat().st_mtime_ns
    second = run(config)
    assert second["counts"]["written_partitions"] == 0
    assert second["counts"]["reused_days"] == 2
    assert second["counts"]["stat_reused_files"] == 1
    assert target.stat().st_mtime_ns == before
    assert pq.read_table(target).num_rows == 2
    assert json.loads(open(second["paths"]["report"], encoding="utf-8").read()) == second


def test_append_and_older_correction_preserve_unchanged_days(setup, dat_file, row):
    config, source, target = setup
    run(config)
    second, third = list(row), list(row)
    second[0] += 86400
    third[0] += 2 * 86400
    dat_file(source, row, second, third)
    appended = run(config)
    assert appended["counts"]["new_days"] == 1
    assert appended["counts"]["reused_days"] == 2
    corrected = list(row)
    corrected[4] = 7200
    dat_file(source, corrected, second, third)
    result = run(config)
    assert result["counts"]["revised_days"] == 1
    assert result["counts"]["reused_days"] == 2
    assert pq.read_table(target)["close"].to_pylist() == [7.2, 7.25, 7.25]


def test_range_union_matches_full_and_never_deletes_other_codes(setup, dat_file, row):
    config, source, target = setup
    dat_file(source.with_name("600001.DAT"), row)
    whole = run(replace(config, output=config.output.with_name("whole"), mode="full"))
    assert whole["status"] == "complete"
    day = replace(config, mode="range", start="20260922", end="20260922")
    run(day)
    run(replace(config, mode="range", start="20260923", end="20260927"))
    expected = pq.read_table(config.output.with_name("whole") / target.relative_to(config.output))
    assert pq.read_table(target).equals(expected)
    result = run(day)
    assert result["counts"]["content_revalidated_files"] == 2
    assert result["counts"]["written_partitions"] == 0


def test_code_filtered_range_preserves_previously_valid_other_code(setup, dat_file, row):
    config, source, target = setup
    dat_file(source.with_name("600001.DAT"), row)
    config = replace(config, mode="range", start="20260922", end="20260922")
    assert run(config)["status"] == "complete"
    corrected, later = list(row), list(row)
    corrected[4] = 7200
    later[0] += 86400
    dat_file(source, corrected, later)
    result = run(replace(config, codes=("600000.SH",)))
    assert result["status"] == "complete"
    assert pq.read_table(target)["close"].to_pylist() == [7.2, 7.25]


def test_force_verify_finds_source_change_with_retained_mtime(setup, dat_file, row):
    config, source, target = setup
    run(config)
    before = target.read_bytes()
    saved = source.stat()
    corrected, later = list(row), list(row)
    corrected[4] = 7200
    later[0] += 86400
    dat_file(source, corrected, later)
    os.utime(source, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    result = run(config, action="verify")
    assert result["status"] == "partial"
    assert "source-output-mismatch" in reasons(result)
    assert result["counts"]["content_revalidated_files"] == 1
    assert target.read_bytes() == before


@pytest.mark.parametrize("change", ["delete", "shrink", "invalid"])
def test_missing_shrunk_invalid_source_preserves_existing_partition(setup, dat_file, row, change):
    config, source, target = setup
    run(config)
    before = target.read_bytes()
    if change == "delete":
        source.unlink()
    elif change == "shrink":
        dat_file(source, row)
    else:
        source.write_bytes(b"wrong header" + source.read_bytes()[12:])
    result = run(config)
    assert result["status"] == "partial"
    assert result["counts"]["written_partitions"] == 0
    assert target.read_bytes() == before


def test_failed_source_blocks_shared_partition_changes(setup, dat_file, row):
    config, source, target = setup
    other = dat_file(source.with_name("600001.DAT"), row)
    run(config)
    before = target.read_bytes()
    source.write_bytes(b"bad")
    corrected = list(row)
    corrected[4] = 7200
    dat_file(other, corrected)
    result = run(config)
    assert result["status"] == "partial"
    assert target.read_bytes() == before


def test_output_tampering_is_reported_and_not_overwritten(setup):
    config, _, target = setup
    run(config)
    original = pq.read_table(target)
    pq.write_table(original.slice(0, 1), target)
    altered = target.read_bytes()
    result = run(config, action="verify")
    assert result["status"] == "partial"
    assert "output-tampered" in reasons(result)
    assert target.read_bytes() == altered


def test_foreign_output_and_incompatible_source_root_refused(setup, tmp_path):
    config, _, _ = setup
    config.output.mkdir()
    sentinel = config.output / "existing.txt"
    sentinel.write_text("mine")
    rejected = run(config)
    assert rejected["status"] == "failed"
    assert "unowned-output" in reasons(rejected)
    assert sentinel.read_text() == "mine"
    sentinel.unlink()
    run(config)
    root = tmp_path / "other-install/datadir"
    root.mkdir(parents=True)
    rejected = run(replace(config, source=root))
    assert rejected["status"] == "failed"
    assert "source-root-mismatch" in reasons(rejected)


def test_scan_and_cancellation_produce_json_reports_without_parquet(setup):
    config, _, target = setup
    events = []
    result = run(config, action="scan", progress=events.append)
    assert result["counts"]["source_files"] == 1
    assert not target.exists()
    assert json.dumps(events)
    cancelled = run(config, cancel=lambda: True)
    assert cancelled["status"] == "cancelled"
    assert not target.exists()


def test_indexes_and_minute_files_use_separate_partitions(setup, dat_file, row):
    config, _, _ = setup
    dat_file(config.source / "SH/86400/000001.DAT", row)
    dat_file(config.source / "SH/60/600000.DAT", row)
    result = run(replace(config, periods=("1d", "1m")))
    assert result["status"] == "complete"
    assert pq.read_table(config.output / "data/indexes/daily/2026/09/SH.parquet")["code"].to_pylist() == ["000001.SH"]
    assert pq.read_table(config.output / "data/qmt-minute/20260922/SH.parquet")["minute"].to_pylist() == ["11:00"]


def test_minute_record_removal_does_not_destroy_existing_bars(setup, dat_file, row):
    config, _, _ = setup
    source = config.source / "SH/60/600000.DAT"
    two, three = list(row), list(row)
    two[0] += 60
    three[0] += 120
    dat_file(source, row, two)
    config = replace(config, periods=("1m",))
    run(config)
    target = config.output / "data/qmt-minute/20260922/SH.parquet"
    before = target.read_bytes()
    dat_file(source, row, three)
    result = run(config)
    assert result["status"] == "partial"
    assert "removed-records" in reasons(result)
    assert target.read_bytes() == before


def test_source_replacement_after_staging_cannot_publish(setup, dat_file, row):
    config, source, target = setup
    run(config)
    before = target.read_bytes()
    corrected, later = list(row), list(row)
    corrected[4] = 7200
    later[0] += 86400
    dat_file(source, corrected, later)
    def change(event):
        if event["event"] == "partition":
            dat_file(source, row, later)
    result = run(config, progress=change)
    assert result["status"] == "partial"
    assert "source-changed" in reasons(result)
    assert target.read_bytes() == before


def test_cancel_after_staging_cleans_pending_without_publishing(setup):
    config, _, target = setup
    stop = False
    def progress(event):
        nonlocal stop
        if event["event"] == "source_complete":
            stop = True
    result = run(config, progress=progress, cancel=lambda: stop)
    assert result["status"] == "cancelled"
    assert not target.exists()
    assert not list((config.output / ".staging").rglob("*.parquet"))
    assert run(config)["status"] == "complete"


def test_output_link_refused_without_touching_external_directory(setup, tmp_path):
    config, _, _ = setup
    run(config, action="scan")
    outside = tmp_path / "outside"
    outside.mkdir()
    data = config.output / "data"
    data.rmdir()
    try:
        data.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit symbolic links")
    result = run(config)
    assert result["status"] == "failed"
    assert "output-link" in reasons(result)
    assert list(outside.iterdir()) == []
