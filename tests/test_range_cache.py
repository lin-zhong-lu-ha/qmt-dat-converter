"""Real DAT range receipts must be corroborated by committed public output."""

import json
import sqlite3
from dataclasses import replace

import pytest

from qmt_dat_converter import engine
from qmt_dat_converter.model import Config


@pytest.fixture
def bounded(tmp_path, dat_file, row):
    source = tmp_path / "install/datadir/SH/86400/600000.DAT"
    old = list(row)
    old[0] = 765388800
    old[1:5] = [6060, 6000, 5610, 5700]
    dat_file(source, old, row)
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",),
                    mode="range", start="20260922", end="20260922")
    return config, source


def test_repeated_range_skips_snapshot_and_preserves_warning_and_inventory(bounded, monkeypatch):
    config, _ = bounded
    first = engine.run(config)
    assert first["status"] == "complete"
    def unexpected(*args, **kwargs):
        pytest.fail("a committed unchanged range should not read the DAT again")
    monkeypatch.setattr(engine, "read_snapshot", unexpected)
    second = engine.run(config)
    assert second["status"] == "complete"
    assert second["counts"]["scope_reused_files"] == 1
    assert second["counts"]["content_revalidated_files"] == 0
    assert second["issues"] == first["issues"]
    with sqlite3.connect(config.output / ".state/cache.sqlite3") as db:
        assert db.execute("SELECT complete,day_count FROM sources").fetchone() == (0, 2)


@pytest.mark.parametrize("action,mode", [("convert", "full"), ("verify", "range")])
def test_full_and_verify_always_reread(bounded, monkeypatch, action, mode):
    config, _ = bounded
    engine.run(config)
    original = engine.read_snapshot
    calls = []
    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, "read_snapshot", observed)
    report = engine.run(replace(config, mode=mode), action=action)
    assert calls and report["counts"].get("scope_reused_files", 0) == 0


@pytest.mark.parametrize("change", ["source", "expand", "evidence", "tamper", "missing"])
def test_changed_or_uncorroborated_scope_rereads(bounded, dat_file, row, monkeypatch, change):
    config, source = bounded
    engine.run(config)
    target = config.output / "data/qmt-daily-monthly/2026/09/SH.parquet"
    if change == "source":
        corrected = list(row)
        corrected[4] = 7200
        old = list(row)
        old[0] = 765388800
        dat_file(source, old, corrected)
    elif change == "expand":
        config = replace(config, start="19940404")
    elif change == "evidence":
        evidence = config.output.parent / "evidence.json"
        evidence.write_text(json.dumps({"version": 1, "source": {"description": "synthetic fixture", "sha256": "0" * 64,
                                                               "provenance": "same-source"},
                                        "comparisons": [{"code": "600000.SH", "period": "1d", "timestamp": row[0] * 1000,
                                                         "fields": {"close": 7.25}}]}))
        config = replace(config, evidence=evidence)
    elif change == "tamper":
        target.write_bytes(b"tampered")
    else:
        target.unlink()
    original = engine.read_snapshot
    calls = []
    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, "read_snapshot", observed)
    report = engine.run(config)
    assert calls
    assert report["counts"].get("scope_reused_files", 0) == 0
    if change in {"expand", "tamper", "missing"}:
        assert report["status"] == "partial"


def test_cancelled_uncommitted_range_does_not_get_receipt(bounded):
    config, _ = bounded
    stop = False
    def progress(event):
        nonlocal stop
        if event["event"] == "source_complete":
            stop = True
    assert engine.run(config, progress=progress, cancel=lambda: stop)["status"] == "cancelled"
    with sqlite3.connect(config.output / ".state/cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM scope_receipts").fetchone()[0] == 0
    assert engine.run(config)["counts"]["content_revalidated_files"] == 1


def test_bounded_checks_only_affected_month_but_verify_checks_history(tmp_path, dat_file, row):
    source = tmp_path / "install/datadir/SH/86400/600000.DAT"
    old = list(row)
    old[0] = 765388800
    dat_file(source, old, row)
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",))
    assert engine.run(config)["status"] == "complete"
    narrow = replace(config, mode="range", start="20260922", end="20260922")
    assert engine.run(narrow)["counts"]["checked_partitions"] == 1
    assert engine.run(narrow, action="verify")["counts"]["checked_partitions"] == 2


@pytest.mark.parametrize("change", ["validation-version", "fingerprint", "missing-day"])
def test_receipt_requires_current_versions_and_committed_days(bounded, monkeypatch, change):
    config, _ = bounded
    assert engine.run(config)["status"] == "complete"
    with sqlite3.connect(config.output / ".state/cache.sqlite3") as db:
        if change == "validation-version":
            db.execute("UPDATE scope_receipts SET validation_version='obsolete'")
        elif change == "fingerprint":
            db.execute("UPDATE days SET digest='uncorroborated'")
        else:
            db.execute("DELETE FROM days")
    original = engine.read_snapshot
    calls = []
    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(engine, "read_snapshot", observed)
    report = engine.run(config)
    assert calls and report["counts"]["scope_reused_files"] == 0


def test_receipt_covers_a_narrower_requested_interval(tmp_path, dat_file, row, monkeypatch):
    source = tmp_path / "install/datadir/SH/86400/600000.DAT"
    later = list(row)
    later[0] += 86400
    dat_file(source, row, later)
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",),
                    mode="range", start="20260922", end="20260923")
    assert engine.run(config)["status"] == "complete"
    def unexpected(*args, **kwargs):
        pytest.fail("a subset of a committed validation interval must reuse")
    monkeypatch.setattr(engine, "read_snapshot", unexpected)
    narrow = engine.run(replace(config, start="20260923"))
    assert narrow["counts"]["scope_reused_files"] == 1
    assert narrow["counts"]["reused_days"] == 1


def test_tampering_after_publication_cannot_grant_receipt(bounded):
    config, _ = bounded
    def tamper(event):
        if event["event"] == "partition_complete":
            (config.output / event["path"]).write_bytes(b"changed after publication")
    report = engine.run(config, progress=tamper)
    assert report["status"] == "partial"
    with sqlite3.connect(config.output / ".state/cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM scope_receipts").fetchone()[0] == 0


def test_failed_shared_partition_cannot_grant_other_source_receipt(bounded, dat_file, row):
    config, source = bounded
    invalid = list(row)
    invalid[1] = 7400
    dat_file(source.with_name("600001.DAT"), invalid)
    report = engine.run(config)
    assert report["status"] == "partial"
    assert report["counts"]["written_partitions"] == 0
    with sqlite3.connect(config.output / ".state/cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM scope_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("event_name", ["phase", "source_complete"])
@pytest.mark.parametrize("bounded_run", [True, False])
def test_reused_sources_recheck_output_after_initial_check(tmp_path, dat_file, row, monkeypatch, event_name, bounded_run):
    source = tmp_path / "install/datadir/SH/86400/600000.DAT"
    dat_file(source, row)
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",))
    if bounded_run:
        config = replace(config, mode="range", start="20260922", end="20260922")
    assert engine.run(config)["status"] == "complete"
    target = config.output / "data/qmt-daily-monthly/2026/09/SH.parquet"
    def unexpected(*args, **kwargs):
        pytest.fail("output integrity rechecks must not reread unchanged DAT content")
    monkeypatch.setattr(engine, "read_snapshot", unexpected)
    def tamper(event):
        if ((event_name == "phase" and event["event"] == "phase" and event["phase"] == "source")
                or (event_name == "source_complete" and event["event"] == "source_complete")):
            target.write_bytes(b"changed after initial output check")
    report = engine.run(config, progress=tamper)
    assert report["status"] == "partial"
    assert any(issue["reason"] == "output-tampered" for issue in report["issues"])
    assert report["counts"]["scope_reused_files" if bounded_run else "stat_reused_files"] == 0
    assert target.read_bytes() == b"changed after initial output check"


@pytest.mark.parametrize("bounded_run", [True, False])
def test_reused_sources_recheck_identity_after_completion(tmp_path, dat_file, row, monkeypatch, bounded_run):
    source = tmp_path / "install/datadir/SH/86400/600000.DAT"
    dat_file(source, row)
    config = Config(source.parents[2], tmp_path / "output", periods=("1d",))
    if bounded_run:
        config = replace(config, mode="range", start="20260922", end="20260922")
    assert engine.run(config)["status"] == "complete"
    def unexpected(*args, **kwargs):
        pytest.fail("late identity validation must not decode a replacement in this run")
    monkeypatch.setattr(engine, "read_snapshot", unexpected)
    def replace_source(event):
        if event["event"] == "source_complete":
            changed = list(row)
            changed[4] = 7200
            dat_file(source, changed)
    report = engine.run(config, progress=replace_source)
    assert report["status"] == "partial"
    assert any(issue["reason"].startswith("source-changed-before-publish") for issue in report["issues"])
    assert report["counts"]["scope_reused_files" if bounded_run else "stat_reused_files"] == 0
