import hashlib
from pathlib import Path

from .model import Config, ConverterError, Snapshot, SourceFile


PERIOD_DIRS = {"86400": "1d", "60": "1m"}
STOCK_PREFIXES = {
    "SH": ("600", "601", "603", "605", "688", "689"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
}
INDEX_CODES = {
    "000001.SH", "000005.SH", "000300.SH", "000905.SH",
    "399001.SZ", "399006.SZ",
}


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _signature(path: Path) -> dict:
    stat = path.stat()
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


def _validate_read_path(path: Path, source_root: Path | None) -> None:
    if path.is_symlink():
        raise ConverterError(f"source-link: {path}")
    if source_root is None:
        return
    try:
        root = source_root.resolve(strict=True)
        candidate = path.absolute()
        if not _within(candidate, root):
            raise ConverterError(f"source-outside-root: {path}")
        current = candidate
        while current != root:
            if current.is_symlink():
                raise ConverterError(f"source-link: {current}")
            current = current.parent
        if not _within(candidate.resolve(strict=True), root) or not candidate.is_file():
            raise ConverterError(f"source-outside-root: {path}")
    except (OSError, RuntimeError) as exc:
        raise ConverterError(f"source-unreadable: {path}") from exc


def scan_sources(config: Config) -> tuple[list[SourceFile], list[dict]]:
    try:
        source_root = config.source.resolve(strict=True)
        if not source_root.is_dir():
            raise ConverterError("source-not-directory")
        output_root = config.output.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConverterError(f"source-inaccessible: {config.source}") from exc
    installation = source_root.parent
    if _within(output_root, installation) or _within(source_root, output_root):
        raise ConverterError("output-path-overlaps-source-installation")

    sources: list[SourceFile] = []
    issues: list[dict] = []
    for market in ("SH", "SZ"):
        for directory, period in PERIOD_DIRS.items():
            if period not in config.periods:
                continue
            folder = source_root / market / directory
            if not folder.exists():
                issues.append({"path": str(folder), "category": "excluded", "reason": "missing-period-directory"})
                continue
            try:
                if folder.is_symlink() or not _within(folder.resolve(strict=True), source_root):
                    issues.append({"path": str(folder), "category": "excluded", "reason": "outside-source-directory"})
                    continue
                entries = sorted(folder.iterdir(), key=lambda path: path.name.casefold())
            except (OSError, RuntimeError):
                issues.append({"path": str(folder), "category": "malformed", "reason": "inaccessible-directory"})
                continue
            for path in entries:
                if path.suffix.upper() != ".DAT" or len(path.stem) != 6 or not path.stem.isascii() or not path.stem.isdigit():
                    issues.append({"path": str(path), "category": "malformed", "reason": "nonstandard-name"})
                    continue
                code = f"{path.stem}.{market}"
                if code in INDEX_CODES:
                    kind = "index"
                elif path.stem.startswith(STOCK_PREFIXES[market]):
                    kind = "stock"
                else:
                    issues.append({"path": str(path), "code": code, "category": "excluded", "reason": "unclassified"})
                    continue
                if config.codes and code not in config.codes:
                    issues.append({"path": str(path), "code": code, "category": "excluded", "reason": "code-filter"})
                    continue
                try:
                    if path.is_symlink() or not path.is_file() or not _within(path.resolve(strict=True), source_root):
                        raise OSError("not a regular in-root file")
                    signature = _signature(path)
                except (OSError, RuntimeError):
                    issues.append({"path": str(path), "code": code, "category": "excluded", "reason": "inaccessible-file"})
                    continue
                sources.append(SourceFile(path, code, market, period, kind, signature))
    return sources, issues


def read_snapshot(
    path: Path, retries: int = 2, *, source_root: Path | None = None,
    expected_signature: dict | None = None,
) -> Snapshot:
    if retries < 0:
        raise ConverterError("invalid-retries")
    if expected_signature is not None and source_root is None:
        raise ConverterError("source-root-required")
    path = Path(path)
    source_root = Path(source_root) if source_root is not None else None
    for attempt in range(retries + 1):
        try:
            _validate_read_path(path, source_root)
            before = _signature(path)
            if expected_signature is not None and before != expected_signature:
                raise ConverterError(f"source-changed: {path}")
            raw = path.read_bytes()
            _validate_read_path(path, source_root)
            after = _signature(path)
        except OSError as exc:
            raise ConverterError(f"source-unreadable: {path}") from exc
        if expected_signature is not None and after != expected_signature:
            raise ConverterError(f"source-changed: {path}")
        if before == after and len(raw) == before["size"]:
            return Snapshot(raw, after, hashlib.sha256(raw).hexdigest())
        if attempt == retries:
            raise ConverterError(f"source-changed: {path}")
    raise AssertionError("unreachable")
