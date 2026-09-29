"""One-partition publication with typed readback and a recoverable journal."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .decoder import SCHEMA, table_digest
from .model import ConverterError
from .source import _signature, _validate_read_path, read_snapshot
from .state import commit_partition, safe_path, sync_directory, sync_file, write_json


PARQUET_OPTIONS = {"compression": "snappy", "version": "1.0", "data_page_version": "1.0", "use_dictionary": False}


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def partition_path(source, day):
    if source.kind == "index":
        folder = "indexes/daily" if source.period == "1d" else "indexes/minute"
    else:
        folder = "qmt-daily-monthly" if source.period == "1d" else "qmt-minute"
    date_path = f"{day[:4]}/{day[4:6]}" if source.period == "1d" else day
    return f"data/{folder}/{date_path}/{source.market}.parquet"


def sorted_table(table):
    return table.take(pc.sort_indices(table, sort_keys=[("code", "ascending"), ("timestamp", "ascending")]))


def read_table(path):
    table = pq.ParquetFile(path).read()
    if not table.schema.equals(SCHEMA, check_metadata=False) or any(table[name].null_count for name in SCHEMA.names):
        raise ConverterError("output-schema-mismatch")
    return table


def check_sources(source_root, sources, *, content=False):
    for item in sources:
        path = source_root / item["path"]
        _validate_read_path(path, source_root)
        if _signature(path) != item["signature"]:
            raise ConverterError(f"source-changed-before-publish: {item['path']}")
        if content:
            snapshot = read_snapshot(path, source_root=source_root, expected_signature=item["signature"])
            if snapshot.sha256 != item["sha256"]:
                raise ConverterError(f"source-content-changed: {item['path']}")


def merge(existing, incoming):
    replacement_keys = set(zip(incoming["code"].to_pylist(), incoming["trade_date"].to_pylist()))
    old_keys = zip(existing["code"].to_pylist(), existing["trade_date"].to_pylist())
    replace_mask = pa.array([key in replacement_keys for key in old_keys], type=pa.bool_())
    replaced = existing.filter(replace_mask)
    old_record_keys = set(zip(replaced["code"].to_pylist(), replaced["timestamp"].to_pylist()))
    new_record_keys = set(zip(incoming["code"].to_pylist(), incoming["timestamp"].to_pylist()))
    if not old_record_keys.issubset(new_record_keys):
        raise ConverterError("removed-records")
    return sorted_table(pa.concat_tables([existing.filter(pc.invert(replace_mask)), incoming]))


def publish(root, source_root, db, run_id, partition, table, sources):
    target = safe_path(root, partition)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(root, partition + f".{run_id}.tmp")
    backup = safe_path(root, partition + f".{run_id}.bak")
    journal_path = safe_path(root, f".journal/{hashlib.sha256(partition.encode()).hexdigest()}.json")
    pq.write_table(table, temporary, **PARQUET_OPTIONS)
    sync_file(temporary)
    readback = read_table(temporary)
    if not sorted_table(readback).equals(sorted_table(table)):
        temporary.unlink()
        raise ConverterError("readback-mismatch")
    del readback
    check_sources(source_root, sources)
    old_hash = None
    if target.exists():
        old_hash = file_hash(target)
        shutil.copyfile(target, backup)
        sync_file(backup)
    journal = {"version": 1, "run": run_id, "source_root": str(source_root.resolve()),
               "partition": partition, "temporary": str(temporary.relative_to(root)),
               "backup": str(backup.relative_to(root)), "old_sha256": old_hash,
               "sha256": file_hash(temporary), "digest": table_digest(table), "sources": sources}
    write_json(journal_path, journal)
    check_sources(source_root, sources)
    os.replace(temporary, target)
    sync_directory(target.parent)
    commit_partition(db, journal, _signature(target))
    journal_path.unlink()
    backup.unlink(missing_ok=True)


def recover(root, source_root, db, report):
    """A replaced file earns a cache commit only after fresh source/readback checks."""
    recovered_runs = set()
    for path in sorted(safe_path(root, ".journal").glob("*.json")):
        safe_path(root, str(path.relative_to(root)))
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal.get("version") != 1 or journal.get("source_root") != str(source_root.resolve()):
            raise ConverterError("invalid-recovery-journal")
        target = safe_path(root, journal["partition"])
        temporary = safe_path(root, journal["temporary"])
        backup = safe_path(root, journal["backup"])
        actual = file_hash(target) if target.exists() else None
        if actual == journal["sha256"]:
            cached = db.execute("SELECT * FROM partitions WHERE path=?", (journal["partition"],)).fetchone()
            committed = cached is not None and cached["sha256"] == journal["sha256"] and cached["digest"] == journal["digest"]
            if table_digest(read_table(target)) != journal["digest"]:
                raise ConverterError("recovery-readback-mismatch")
            try:
                check_sources(source_root, journal["sources"], content=True)
                report["counts"]["recovery_revalidated_files"] += len(journal["sources"])
            except (ConverterError, OSError) as exc:
                if committed:
                    # SQLite's atomic transaction already established this
                    # revision. A later source change cannot undo that commit.
                    # The ordinary source pass will compare the new identity.
                    report["issues"].append({"category": "warning", "reason": f"source-changed-since-committed-publication: {exc}", "path": journal["partition"]})
                    report["counts"]["recovered_partitions"] += 1
                elif journal["old_sha256"] is not None:
                    if not backup.exists() or file_hash(backup) != journal["old_sha256"]:
                        raise ConverterError("recovery-backup-missing-or-tampered") from exc
                    os.replace(backup, target)
                else:
                    # Keep the uncertain bytes in the owned staging area for audit.
                    quarantine = safe_path(root, f".staging/rejected-{journal['run']}-{target.name}")
                    os.replace(target, quarantine)
                if not committed:
                    report["issues"].append({"category": "error", "reason": f"recovery-rejected: {exc}", "path": journal["partition"]})
            else:
                commit_partition(db, journal, _signature(target))
                report["counts"]["recovered_partitions"] += 1
        elif actual != journal["old_sha256"]:
            raise ConverterError("recovery-output-tampered")
        with db:
            db.execute("DELETE FROM pending WHERE run=? AND partition=?", (journal["run"], journal["partition"]))
        path.unlink()
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        recovered_runs.add(journal["run"])
    # The process lock is held and every journal has been resolved. UUID stage
    # directories without a journal are abandoned, including a hard exit before
    # the first partition's journal. Never reuse their uncommitted fingerprints.
    for stage in safe_path(root, ".staging").iterdir():
        if re.fullmatch(r"[0-9a-f]{32}", stage.name) and stage.is_dir():
            safe_path(root, str(stage.relative_to(root)))
            for item in stage.rglob("*"):
                safe_path(root, str(item.relative_to(root)))
            recovered_runs.add(stage.name)
    for run_id in recovered_runs:
        with db:
            db.execute("DELETE FROM pending WHERE run=?", (run_id,))
        stage = safe_path(root, f".staging/{run_id}")
        if stage.is_dir():
            shutil.rmtree(stage)
