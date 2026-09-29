import json
import os
import subprocess
import sys
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow as pa

from test_engine import setup, reasons
from qmt_dat_converter.engine import run


def test_failed_readback_keeps_old_output(setup, dat_file, row, monkeypatch):
    config, source, target = setup
    run(config)
    before = target.read_bytes()
    new, later = list(row), list(row)
    new[4] = 7200
    later[0] += 86400
    dat_file(source, new, later)
    original = pq.write_table

    def corrupt(table, where, *args, **kwargs):
        original(table.slice(0, 1), where, *args, **kwargs)

    monkeypatch.setattr(pq, "write_table", corrupt)
    result = run(config)
    assert result["status"] == "partial"
    assert "readback" in reasons(result)
    assert target.read_bytes() == before


def test_interrupted_replacement_recovers_with_content_revalidation(setup, dat_file, row, monkeypatch):
    config, source, target = setup
    run(config)
    new, later = list(row), list(row)
    new[4] = 7200
    later[0] += 86400
    dat_file(source, new, later)
    original = os.replace
    interrupted = False

    def replace_then_interrupt(src, dst):
        nonlocal interrupted
        original(src, dst)
        if str(dst) == str(target) and not interrupted:
            interrupted = True
            raise OSError("simulated crash after replacement")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_then_interrupt)
        failed = run(config)
    assert failed["status"] == "partial"
    result = run(config)
    assert result["status"] == "complete"
    assert result["counts"]["recovered_partitions"] == 1
    assert result["counts"]["recovery_revalidated_files"] == 1
    assert pq.read_table(target)["close"].to_pylist() == [7.2, 7.25]
    assert not list((config.output / ".staging").rglob("*.parquet"))


def test_changed_source_during_crash_rolls_back_then_requires_new_validation(setup, dat_file, row, monkeypatch):
    config, source, target = setup
    run(config)
    before = target.read_bytes()
    corrected, later = list(row), list(row)
    corrected[4] = 7200
    later[0] += 86400
    dat_file(source, corrected, later)
    original = os.replace
    def interrupt(src, dst):
        original(src, dst)
        if str(dst) == str(target):
            raise OSError("crash after replacement")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt)
        run(config)
    source.write_bytes(b"bad")
    result = run(config)
    assert result["status"] == "partial"
    assert "recovery-rejected" in reasons(result)
    assert target.read_bytes() == before


def test_staging_corruption_is_detected_against_decoded_fingerprint(setup, monkeypatch):
    config, _, target = setup
    original = pq.ParquetWriter.write_table
    def corrupt(self, table, *args, **kwargs):
        altered = table.set_column(table.schema.get_field_index("close"), table.schema.field("close"), pa.array([7.0] * table.num_rows))
        return original(self, altered, *args, **kwargs)
    monkeypatch.setattr(pq.ParquetWriter, "write_table", corrupt)
    result = run(config)
    assert result["status"] == "partial"
    assert "staging-readback" in reasons(result)
    assert not target.exists()


def test_database_commit_failure_after_replacement_is_recoverable(setup):
    config, _, target = setup
    run(config, action="scan")
    cache = config.output / ".state/cache.sqlite3"
    with sqlite3.connect(cache) as db:
        db.execute("CREATE TRIGGER reject_days BEFORE INSERT ON days BEGIN SELECT RAISE(ABORT, 'simulated state commit failure'); END")
    failed = run(config)
    assert failed["status"] == "partial"
    assert target.exists()
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT COUNT(*) FROM days").fetchone()[0] == 0
        db.execute("DROP TRIGGER reject_days")
    result = run(config)
    assert result["status"] == "complete"
    assert result["counts"]["recovered_partitions"] == 1
    assert result["counts"]["recovery_revalidated_files"] == 1


def test_crash_after_database_commit_cannot_roll_back_established_revision(setup, dat_file, row, monkeypatch):
    config, source, target = setup
    run(config)
    corrected, later = list(row), list(row)
    corrected[4] = 7200
    later[0] += 86400
    dat_file(source, corrected, later)
    original = Path.unlink
    def interrupt(path, *args, **kwargs):
        if path.parent.name == ".journal" and path.suffix == ".json":
            raise OSError("crash during committed-journal cleanup")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        assert run(config)["status"] == "partial"
    established = target.read_bytes()
    source.write_bytes(b"bad")
    result = run(config)
    assert result["status"] == "partial"
    assert target.read_bytes() == established
    assert pq.read_table(target)["close"].to_pylist() == [7.2, 7.25]


def test_concurrent_process_lock_prevents_second_writer(setup):
    config, _, target = setup
    run(config, action="scan")
    from qmt_dat_converter.state import output_lock
    with output_lock(config.output):
        script = "from pathlib import Path; from qmt_dat_converter.engine import run; from qmt_dat_converter.model import Config; import json,sys; print(json.dumps(run(Config(Path(sys.argv[1]),Path(sys.argv[2]),periods=('1d',)))))"
        environment = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
        child = subprocess.run([sys.executable, "-c", script, str(config.source), str(config.output)], capture_output=True, text=True, env=environment, check=True)
    result = json.loads(child.stdout)
    assert result["status"] == "failed"
    assert "output-locked" in reasons(result)
    assert not target.exists()
    assert run(config)["status"] == "complete"


def test_process_exit_before_publication_discards_only_owned_orphan_staging(setup):
    config, _, target = setup
    script = "from pathlib import Path; from qmt_dat_converter.engine import run; from qmt_dat_converter.model import Config; import os,sys; run(Config(Path(sys.argv[1]),Path(sys.argv[2]),periods=('1d',)),progress=lambda e: os._exit(77) if e['event']=='source_complete' else None)"
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    child = subprocess.run([sys.executable, "-c", script, str(config.source), str(config.output)], capture_output=True, env=environment)
    assert child.returncode == 77
    assert not target.exists()
    assert list((config.output / ".staging").rglob("*.parquet"))
    assert run(config)["status"] == "complete"
    assert not list((config.output / ".staging").rglob("*.parquet"))
