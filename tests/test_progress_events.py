"""Live issue delivery and phase durations on real synthetic sources."""

from qmt_dat_converter.engine import run
from qmt_dat_converter.model import Config


def test_error_is_live_before_later_file_and_saved_details_match(tmp_path, dat_file, row):
    root = tmp_path / "install/datadir"
    broken = list(row)
    broken[1] = 7400
    dat_file(root / "SH/86400/600000.DAT", broken)
    dat_file(root / "SH/86400/600001.DAT", row)
    events = []
    report = run(Config(root, tmp_path / "output", periods=("1d",)), progress=events.append)
    error_index = next(i for i, e in enumerate(events) if e["event"] == "issue")
    later_index = next(i for i, e in enumerate(events) if e["event"] == "source" and e["index"] == 2)
    assert error_index < later_index
    assert events[error_index]["issue"] in report["issues"]
    assert events[error_index]["issue_counts"]["error"] == 1
    assert events[error_index]["issue_counts"]["excluded"] > 0
    assert [e["phase"] for e in events if e["event"] == "phase"] == ["scan", "check", "source", "publish", "report"]
    assert set(report["timings"]) == {"scan", "check", "source", "publish", "report"}
    assert all(value >= 0 for value in report["timings"].values())
    blocked = [e for e in events if e["event"] == "partition_complete"]
    assert blocked and blocked[0]["counts"]["written_partitions"] == 0


def test_reused_source_still_completes_and_cancelled_phase_is_timed(tmp_path, dat_file, row):
    root = tmp_path / "install/datadir"
    dat_file(root / "SH/86400/600000.DAT", row)
    config = Config(root, tmp_path / "output", periods=("1d",))
    run(config)
    events = []
    reused = run(config, progress=events.append)
    completed, = [e for e in events if e["event"] == "source_complete"]
    assert completed["counts"]["stat_reused_files"] == 1
    assert completed["elapsed_seconds"] >= completed["phase_elapsed_seconds"] >= 0
    assert reused["timings"]["source"] >= 0
    stop = False
    def cancel_after_source(event):
        nonlocal stop
        if event["event"] == "source_complete":
            stop = True
    # A fresh output ensures there is a publication boundary at which to cancel.
    from dataclasses import replace
    cancelled = run(replace(config, output=tmp_path / "cancel"), progress=cancel_after_source, cancel=lambda: stop)
    assert cancelled["status"] == "cancelled"
    assert cancelled["timings"]["source"] >= 0
    assert cancelled["timings"]["publish"] >= 0
    assert cancelled["timings"]["report"] >= 0


def test_failed_scan_keeps_elapsed_phase_and_counts_errors(tmp_path):
    report = run(Config(tmp_path / "missing", tmp_path / "output", periods=("1d",)))
    assert report["status"] == "failed"
    assert report["issue_counts"]["error"] == 1
    assert report["timings"]["scan"] >= 0
    assert report["elapsed_seconds"] >= report["timings"]["scan"]


def test_failed_final_report_write_retains_its_duration(tmp_path, dat_file, row, monkeypatch):
    from qmt_dat_converter import engine
    root = tmp_path / "install/datadir"
    dat_file(root / "SH/86400/600000.DAT", row)
    original = engine.write_json
    frozen = []
    def fail_final(path, value):
        if path.parent.name == "reports":
            frozen.append(value["timings"]["report"])
            raise OSError("failed final report save")
        return original(path, value)
    monkeypatch.setattr(engine, "write_json", fail_final)
    report = run(Config(root, tmp_path / "output", periods=("1d",)))
    assert report["status"] == "failed"
    assert report["timings"]["report"] >= frozen[0]
    assert report["elapsed_seconds"] >= sum(report["timings"].values())
