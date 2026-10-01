from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa


class ConverterError(Exception):
    pass


class RecordValidationError(ConverterError):
    def __init__(self, reason: str, details: dict):
        super().__init__(reason)
        self.details = details


class Cancelled(ConverterError):
    pass


def normalize_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d" if len(value) == 8 else "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ConverterError(f"invalid-date: {value}") from exc
    if parsed < date(1970, 1, 1):
        raise ConverterError(f"invalid-date: {value}")
    return parsed.isoformat()


@dataclass(frozen=True)
class Config:
    source: Path
    output: Path
    periods: tuple[str, ...] = ("1d", "1m")
    start: str | None = None
    end: str | None = None
    mode: str = "incremental"
    codes: tuple[str, ...] = ()
    evidence: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "output", Path(self.output))
        object.__setattr__(self, "start", normalize_date(self.start))
        object.__setattr__(self, "end", normalize_date(self.end))
        object.__setattr__(self, "periods", tuple(self.periods))
        object.__setattr__(self, "codes", tuple(code.upper() for code in self.codes))
        if self.evidence is not None:
            object.__setattr__(self, "evidence", Path(self.evidence))
        if not self.periods or set(self.periods) - {"1d", "1m"}:
            raise ConverterError("invalid-period")
        if self.mode not in {"full", "incremental", "range"}:
            raise ConverterError("invalid-mode")
        if self.start and self.end and self.start > self.end:
            raise ConverterError("invalid-date-range")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    code: str
    market: str
    period: str
    kind: str
    signature: dict | None = None


@dataclass(frozen=True)
class Snapshot:
    raw: bytes
    signature: dict
    sha256: str


@dataclass(frozen=True)
class Decoded:
    table: pa.Table
    days: dict[str, dict]
    profile: str
    warnings: tuple[dict, ...] = ()
