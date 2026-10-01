"""Single-worker persistent DAT conversion. No service or network dependency."""

from collections import defaultdict
from dataclasses import replace
import json
import shutil
import sqlite3
import time
import uuid

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .decoder import SCHEMA, decode, table_digest, single_row_digests
from .evidence import Evidence, initial_verification
from .model import Cancelled, ConverterError
from .report_text import write_text_report
from .source import _signature, scan_sources, read_snapshot
from .state import RULE_VERSION, connect, output_lock, own_output, safe_path, write_json
from .storage import (PARQUET_OPTIONS, check_sources, file_hash, merge, partition_path,
                      publish, read_table, recover, sorted_table)


def _issue(report, reason, path=None, category="error"):
    item = {"category": category, "reason": str(reason)}
    item.update(getattr(reason, "details", {}))
    if path is not None:
        item["path"] = str(path)
    report["issues"].append(item)


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise Cancelled("cancelled")


def _emit(progress, **event):
    if progress is not None:
        progress(event)


def _selected(day, config):
    return ((config.start is None or day >= config.start.replace("-", ""))
            and (config.end is None or day <= config.end.replace("-", "")))


def _in_scope(source, config):
    return source["period"] in config.periods and (not config.codes or source["code"] in config.codes)


def _block_source(db, source_id, blocked):
    blocked.update(row[0] for row in db.execute("SELECT DISTINCT partition FROM days WHERE source_id=?", (source_id,)))


def _stage(root, db, run_id, source_id, source, decoded, wanted):
    """One file per source, one row group per output partition (not per daily row)."""
    stage = f".staging/{run_id}/{source_id}.parquet"
    path = safe_path(root, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    groups = defaultdict(list)
    offset = 0
    for day, detail in decoded.days.items():
        if day in wanted:
            groups[partition_path(source, day)].append((day, detail, offset))
        offset += detail["rows"]
    pending = []
    with pq.ParquetWriter(path, SCHEMA, **PARQUET_OPTIONS) as writer:
        for group_index, (partition, days) in enumerate(groups.items()):
            tables = []
            group_offset = 0
            for day, detail, offset in days:
                tables.append(decoded.table.slice(offset, detail["rows"]))
                pending.append((run_id, source_id, day, detail["raw_hash"], detail["digest"], detail["rows"],
                                RULE_VERSION, partition, stage, group_index, group_offset))
                group_offset += detail["rows"]
            table = pa.concat_tables(tables)
            writer.write_table(table, row_group_size=table.num_rows)
    with db:
        db.executemany("INSERT INTO pending VALUES (?,?,?,?,?,?,?,?,?,?,?)", pending)


def _incoming(root, pending):
    groups = defaultdict(list)
    for item in pending:
        groups[(item["stage"], item["row_group"])].append(item)
    tables = []
    for (stage, row_group), days in groups.items():
        table = pq.ParquetFile(safe_path(root, stage)).read_row_group(row_group)
        single_digests = single_row_digests(table) if all(item["rows"] == 1 for item in days) else None
        for item in days:
            day_table = table.slice(item["row_offset"], item["rows"])
            digest = single_digests[item["row_offset"]] if single_digests is not None else table_digest(day_table)
            if digest != item["digest"]:
                raise ConverterError("staging-readback-mismatch")
            tables.append(day_table)
    return pa.concat_tables(tables)


def _verify_rows(existing, incoming):
    keys = set(zip(incoming["code"].to_pylist(), incoming["trade_date"].to_pylist()))
    selected = pa.array([key in keys for key in zip(existing["code"].to_pylist(), existing["trade_date"].to_pylist())], type=pa.bool_())
    if not sorted_table(existing.filter(selected)).equals(sorted_table(incoming)):
        raise ConverterError("source-output-mismatch")


def _process(config, root, sources, report, action, progress, cancel):
    db = connect(root)
    try:
        if action == "convert":
            recover(root, config.source, db, report)
        elif action == "verify" and list(safe_path(root, ".journal").glob("*.json")):
            raise ConverterError("pending-recovery-run-convert")
        if action == "scan":
            return
        try:
            evidence = Evidence(config.evidence)
        except ConverterError as exc:
            _issue(report, exc)
            evidence = Evidence(None)
        previous = {row["path"]: row for row in db.execute("SELECT * FROM sources") if _in_scope(row, config)}
        inventory = {str(source.path.relative_to(config.source)): source for source in sources}
        blocked = set()
        block_all = set()
        blocking_codes = defaultdict(set)
        group_blocking_codes = defaultdict(set)
        for path, old in previous.items():
            if path not in inventory:
                _issue(report, "source-missing", config.source / path)
                _block_source(db, old["id"], blocked)
                for row in db.execute("SELECT DISTINCT partition FROM days WHERE source_id=?", (old["id"],)):
                    blocking_codes[row[0]].add(old["code"])
        scope_sql = "s.period IN (" + ",".join("?" for _ in config.periods) + ")"
        scope_args = list(config.periods)
        if config.codes:
            scope_sql += " AND s.code IN (" + ",".join("?" for _ in config.codes) + ")"
            scope_args.extend(config.codes)
        partition_query = "SELECT p.* FROM partitions p WHERE EXISTS (SELECT 1 FROM days d JOIN sources s ON s.id=d.source_id WHERE d.partition=p.path AND " + scope_sql + ")"
        for row in db.execute(partition_query, scope_args):
            path = safe_path(root, row["path"])
            try:
                current = _signature(path)
                if action == "verify" or config.mode == "full" or current != json.loads(row["signature"]):
                    if file_hash(path) != row["sha256"]:
                        raise ConverterError("output-tampered")
                report["counts"]["checked_partitions"] += 1
            except (ConverterError, OSError) as exc:
                _issue(report, f"output-tampered: {exc}", path)
                blocked.add(row["path"])
        source_info = {}
        completed_candidates = {}
        for index, source in enumerate(sources):
            _check_cancel(cancel)
            relative = str(source.path.relative_to(config.source))
            _emit(progress, event="source", index=index + 1, total=len(sources), code=source.code, period=source.period)
            with db:
                db.execute("INSERT OR IGNORE INTO sources(path,code,market,period,kind) VALUES (?,?,?,?,?)",
                           (relative, source.code, source.market, source.period, source.kind))
            old = db.execute("SELECT * FROM sources WHERE path=?", (relative,)).fetchone()
            source_id = old["id"]
            can_reuse = (action == "convert" and config.mode == "incremental" and config.start is None and config.end is None
                         and old["complete"] and old["signature"] and json.loads(old["signature"]) == source.signature
                         and not evidence.needs(source))
            if can_reuse:
                source_info[source_id] = {"path": relative, "signature": source.signature, "sha256": old["sha256"]}
                report["counts"]["stat_reused_files"] += 1
                report["counts"]["reused_days"] += old["day_count"]
                continue
            old_days = {item["day"]: item for item in db.execute("SELECT * FROM days WHERE source_id=?", (source_id,))}
            try:
                snapshot = read_snapshot(source.path, source_root=config.source, expected_signature=source.signature)
                source_info[source_id] = {"path": relative, "signature": snapshot.signature, "sha256": snapshot.sha256}
                report["counts"]["content_revalidated_files"] += 1
                if old["signature"] and snapshot.signature["size"] < json.loads(old["signature"])["size"]:
                    raise ConverterError("source-shrunk")
                decoded = decode(snapshot, source, validation_start=config.start, validation_end=config.end)
                for warning in decoded.warnings:
                    report["issues"].append({**warning, "path": str(source.path)})
                if set(old_days) - set(decoded.days):
                    raise ConverterError("removed-days")
                evidence.compare(source, decoded.table)
                wanted = set()
                for day, detail in decoded.days.items():
                    if not _selected(day, config):
                        continue
                    cached = old_days.get(day)
                    if cached and detail["rows"] < cached["rows"]:
                        raise ConverterError("removed-records")
                    unchanged = cached and cached["raw_hash"] == detail["raw_hash"] and cached["rule_version"] == RULE_VERSION
                    report["counts"]["reused_days" if unchanged else "revised_days" if cached else "new_days"] += 1
                    if not unchanged or action == "verify":
                        wanted.add(day)
                if wanted:
                    _stage(root, db, report["run_id"], source_id, source, decoded, wanted)
                completed_candidates[source_id] = (source_info[source_id], len(decoded.days), config.start is None and config.end is None)
                del decoded, snapshot
            except (ConverterError, OSError, ValueError, pa.ArrowException, sqlite3.Error) as exc:
                _issue(report, exc, source.path)
                _block_source(db, source_id, blocked)
                for row in db.execute("SELECT DISTINCT partition FROM days WHERE source_id=?", (source_id,)):
                    blocking_codes[row[0]].add(source.code)
                for day in getattr(exc, "details", {}).get("days", []):
                    partition = partition_path(source, day)
                    blocked.add(partition)
                    blocking_codes[partition].add(source.code)
                if not old_days:
                    group = (source.period, source.market, source.kind)
                    block_all.add(group)
                    group_blocking_codes[group].add(source.code)
            _emit(progress, event="source_complete", index=index + 1, total=len(sources), code=source.code, period=source.period)
        external = evidence.result()
        report["verification"]["external_evidence"] = external
        evidence_failed = external["status"] == "mismatch" or any(item["reason"].startswith("invalid-evidence") for item in report["issues"])
        if external["status"] == "mismatch":
            _issue(report, "evidence-mismatch-or-missing-scope")
        partitions = [row[0] for row in db.execute("SELECT DISTINCT partition FROM pending WHERE run=? ORDER BY partition", (report["run_id"],))]
        for index, partition in enumerate(partitions):
            _check_cancel(cancel)
            _emit(progress, event="partition", index=index + 1, total=len(partitions), path=partition)
            pending = db.execute("SELECT * FROM pending WHERE run=? AND partition=?", (report["run_id"], partition)).fetchall()
            affected = {row["source_id"] for row in pending}
            affected.update(row[0] for row in db.execute("SELECT DISTINCT source_id FROM days WHERE partition=?", (partition,)))
            identities = []
            unpublishable = partition in blocked or evidence_failed
            causes = set(blocking_codes[partition])
            for source_id in affected:
                source_row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if (source_row["period"], source_row["market"], source_row["kind"]) in block_all:
                    unpublishable = True
                    causes.update(group_blocking_codes[(source_row["period"], source_row["market"], source_row["kind"])])
                info = source_info.get(source_id)
                if info is None:
                    if source_row["signature"] and source_row["sha256"]:
                        info = {"path": source_row["path"], "signature": json.loads(source_row["signature"]), "sha256": source_row["sha256"]}
                    else:
                        unpublishable = True
                if info is not None:
                    identities.append(info)
            if unpublishable:
                _issue(report, "partition-unpublishable", partition)
                report["issues"][-1]["blocking_codes"] = sorted(causes)
                continue
            try:
                target = safe_path(root, partition)
                known = db.execute("SELECT * FROM partitions WHERE path=?", (partition,)).fetchone()
                if target.exists():
                    if known is None or file_hash(target) != known["sha256"]:
                        raise ConverterError("output-tampered-or-untracked")
                    existing = read_table(target)
                elif known:
                    raise ConverterError("output-missing")
                else:
                    existing = pa.Table.from_batches([], schema=SCHEMA)
                incoming = _incoming(root, pending)
                check_sources(config.source, identities)
                if action == "verify":
                    _verify_rows(existing, incoming)
                    report["counts"]["content_verified_partitions"] += 1
                else:
                    combined = merge(existing, incoming)
                    publish(root, config.source, db, report["run_id"], partition, combined, identities)
                    report["counts"]["written_partitions"] += 1
            except (ConverterError, OSError, ValueError, pa.ArrowException, sqlite3.Error) as exc:
                _issue(report, exc, partition)
                blocked.add(partition)
            finally:
                existing = incoming = combined = None
            _emit(progress, event="partition_complete", index=index + 1, total=len(partitions), path=partition)
        if action == "convert":
            with db:
                for source_id, (info, day_count, full_file) in completed_candidates.items():
                    pending = db.execute("SELECT 1 FROM pending WHERE run=? AND source_id=? LIMIT 1", (report["run_id"], source_id)).fetchone()
                    count = db.execute("SELECT COUNT(*) FROM days WHERE source_id=?", (source_id,)).fetchone()[0]
                    if pending is None:
                        db.execute("UPDATE sources SET signature=?,sha256=?,complete=?,day_count=? WHERE id=?",
                                   (json.dumps(info["signature"], sort_keys=True), info["sha256"], int(full_file and count == day_count), count, source_id))
        errors = any(item["category"] == "error" for item in report["issues"])
        report["verification"]["conversion_consistency"].update(
            status="failed" if errors else "verified" if sources else "unverified",
            method="content-comparison" if action == "verify" else "readback-and-cached-fingerprints",
            stat_reused_files=report["counts"]["stat_reused_files"],
            content_revalidated_files=report["counts"]["content_revalidated_files"])
    finally:
        journals = list(safe_path(root, ".journal").glob("*.json"))
        protected = {json.loads(path.read_text(encoding="utf-8"))["run"] for path in journals}
        if report["run_id"] not in protected:
            with db:
                db.execute("DELETE FROM pending WHERE run=?", (report["run_id"],))
            stage = safe_path(root, f".staging/{report['run_id']}")
            if stage.exists():
                shutil.rmtree(stage)
        db.close()


def run(config, action="convert", progress=None, cancel=None) -> dict:
    """Return/save a JSON-safe report; verify never changes public data.

    Day counts describe observed source days; written_partitions counts actual
    publications. Rejected/unowned/locked outputs have paths.report=None.
    """
    started = time.monotonic()
    run_id = uuid.uuid4().hex
    report = {"status": "complete", "run_id": run_id, "action": action,
              "scope": {"source": str(config.source), "output": str(config.output),
                        "periods": list(config.periods), "codes": list(config.codes),
                        "start": config.start, "end": config.end, "mode": config.mode},
              "elapsed_seconds": 0.0,
              "counts": {key: 0 for key in ("source_files", "new_days", "revised_days", "reused_days", "written_partitions",
                         "stat_reused_files", "content_revalidated_files", "checked_partitions", "content_verified_partitions",
                         "recovered_partitions", "recovery_revalidated_files")},
              "issues": [], "paths": {"source": str(config.source), "output": str(config.output),
                                      "report": None, "text_report": None},
              "verification": initial_verification()}
    report["verification"]["conversion_consistency"]["validation_scope"] = {"start": config.start, "end": config.end}
    try:
        if action not in {"convert", "scan", "verify"}:
            raise ConverterError("invalid-action")
        sources, issues = scan_sources(config)
        config = replace(config, source=config.source.resolve(strict=True), output=config.output.absolute())
        report["issues"].extend(issues)
        report["counts"]["source_files"] = len(sources)
        with output_lock(config.output):
            root = own_output(config)
            try:
                _check_cancel(cancel)
                _emit(progress, event="scan_complete", source_files=len(sources), issue_count=len(issues))
                _process(config, root, sources, report, action, progress, cancel)
                if any(item["category"] == "error" for item in report["issues"]):
                    report["status"] = "partial"
            except Cancelled:
                report["status"] = "cancelled"
            except (ConverterError, OSError, ValueError, pa.ArrowException, sqlite3.Error) as exc:
                report["status"] = "failed"
                _issue(report, exc)
            report_path = safe_path(root, f"reports/{run_id}.json")
            text_path = safe_path(root, f"reports/{run_id}.txt")
            report["paths"]["report"] = str(report_path)
            report["paths"]["text_report"] = str(text_path)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_text_report(text_path, report)
            write_json(report_path, report)
    except (ConverterError, OSError, ValueError, pa.ArrowException, sqlite3.Error) as exc:
        report["status"] = "failed"
        report["paths"]["report"] = None
        _issue(report, exc)
    return report
