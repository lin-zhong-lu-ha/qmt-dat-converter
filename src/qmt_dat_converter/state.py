"""Owned output, process locking, and compact persistent day fingerprints."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3

from .decoder import PROFILE
from .model import ConverterError


RULE_VERSION = "qmt-business-v2"
MARKER = ".qmt-converter.json"


def safe_path(root: Path, relative: str = "") -> Path:
    """Reject links/junctions in every existing output path component."""
    root = root.absolute()
    candidate = root / relative
    if candidate != root and root not in candidate.parents:
        raise ConverterError("output-path-escape")
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ConverterError(f"output-link: {part}")
    resolved = candidate.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise ConverterError("output-path-escape")
    return candidate


def sync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def sync_directory(path: Path) -> None:
    # Windows does not expose directory fsync through os.open; replacement is
    # recoverable via the durable journal. This is not a power-loss guarantee.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    safe_path(path.parent, temporary.name)
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@contextmanager
def output_lock(root: Path):
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = safe_path(root, ".qmt-converter.lock")
    with lock.open("a+b") as handle:
        if lock.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ConverterError("output-locked") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def own_output(config):
    root = config.output.absolute()
    marker = safe_path(root, MARKER)
    expected = {"owner": "qmt-dat-converter", "version": 1,
                "source_root": str(config.source.resolve(strict=True)),
                "schema_version": 1, "profile": PROFILE, "rule_version": RULE_VERSION}
    if marker.exists():
        try:
            found = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConverterError("invalid-output-marker") from exc
        if found.get("source_root") != expected["source_root"]:
            raise ConverterError("source-root-mismatch")
        if found != expected:
            raise ConverterError("incompatible-output-version")
    else:
        if any(item.name != ".qmt-converter.lock" for item in root.iterdir()):
            raise ConverterError("unowned-output")
        write_json(marker, expected)
    for name in ("reports", ".state", ".staging", ".journal", "data"):
        safe_path(root, name).mkdir(exist_ok=True)
    return root


def connect(root: Path):
    path = safe_path(root, ".state/cache.sqlite3")
    for suffix in ("-wal", "-shm", "-journal"):
        safe_path(root, str(path.relative_to(root)) + suffix)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
            code TEXT NOT NULL, market TEXT NOT NULL, period TEXT NOT NULL,
            kind TEXT NOT NULL, signature TEXT, sha256 TEXT, complete INTEGER NOT NULL DEFAULT 0,
            day_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS days (
            source_id INTEGER NOT NULL REFERENCES sources(id), day TEXT NOT NULL,
            raw_hash TEXT NOT NULL, digest TEXT NOT NULL, rows INTEGER NOT NULL,
            rule_version TEXT NOT NULL, partition TEXT NOT NULL,
            PRIMARY KEY(source_id, day)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS days_partition ON days(partition);
        CREATE TABLE IF NOT EXISTS partitions (
            path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, digest TEXT NOT NULL,
            signature TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS pending (
            run TEXT NOT NULL, source_id INTEGER NOT NULL, day TEXT NOT NULL,
            raw_hash TEXT NOT NULL, digest TEXT NOT NULL, rows INTEGER NOT NULL,
            rule_version TEXT NOT NULL, partition TEXT NOT NULL,
            stage TEXT NOT NULL, row_group INTEGER NOT NULL, row_offset INTEGER NOT NULL,
            PRIMARY KEY(run, source_id, day)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS pending_partition ON pending(run, partition);
    """)
    return db


def commit_partition(db, journal, signature):
    with db:
        db.execute("""INSERT OR REPLACE INTO days
            SELECT source_id,day,raw_hash,digest,rows,rule_version,partition
            FROM pending WHERE run=? AND partition=?""", (journal["run"], journal["partition"]))
        db.execute("INSERT OR REPLACE INTO partitions VALUES (?,?,?,?)",
                   (journal["partition"], journal["sha256"], journal["digest"], json.dumps(signature, sort_keys=True)))
        db.execute("DELETE FROM pending WHERE run=? AND partition=?", (journal["run"], journal["partition"]))
