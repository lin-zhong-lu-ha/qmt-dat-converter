import hashlib
import struct

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .model import ConverterError, Decoded, RecordValidationError, Snapshot, SourceFile, normalize_date


HEADER = bytes.fromhex("feffffffffffff7f")
PROFILE = "qmt-64-v1-unverified"
SCHEMA = pa.schema([
    pa.field("trade_date", pa.string(), nullable=False),
    pa.field("code", pa.string(), nullable=False),
    pa.field("exchange", pa.string(), nullable=False),
    pa.field("datetime", pa.string(), nullable=False),
    pa.field("minute", pa.string(), nullable=False),
    pa.field("timestamp", pa.int64(), nullable=False),
    pa.field("open", pa.float64(), nullable=False),
    pa.field("high", pa.float64(), nullable=False),
    pa.field("low", pa.float64(), nullable=False),
    pa.field("close", pa.float64(), nullable=False),
    pa.field("volume", pa.int64(), nullable=False),
    pa.field("amount", pa.float64(), nullable=False),
])


def single_row_digests(table: pa.Table) -> list[str]:
    """Canonical v2 digests for each one-record day in a table."""
    if set(table.column_names) != set(SCHEMA.names):
        raise ConverterError("digest-schema-mismatch")
    columns = []
    for field in SCHEMA:
        column = table[field.name].combine_chunks()
        if column.null_count:
            raise ConverterError("digest-null-field")
        if pa.types.is_string(field.type):
            encoded = [value.encode("utf-8") for value in column.to_pylist()]
            columns.append([struct.pack("<II", 0, len(value)) + value for value in encoded])
        else:
            dtype = "<i8" if pa.types.is_integer(field.type) else "<f8"
            buffer = column.to_numpy(zero_copy_only=False).astype(dtype, copy=False).tobytes()
            columns.append([buffer[offset:offset + 8] for offset in range(0, len(buffer), 8)])
    prefix = b"qmt-business-v2\x00" + struct.pack("<Q", 1)
    return [hashlib.sha256(prefix + b"".join(values)).hexdigest() for values in zip(*columns)]


def decode(snapshot: Snapshot, source: SourceFile, start=None, end=None, *,
           validation_start=None, validation_end=None) -> Decoded:
    raw = snapshot.raw
    if len(raw) < 8 or raw[:8] != HEADER:
        raise ConverterError("unknown-header")
    if len(raw) == 8:
        raise ConverterError("empty-dat")
    if (len(raw) - 8) % 64:
        raise ConverterError("invalid-record-length")
    if source.period not in {"1d", "1m"} or source.market not in {"SH", "SZ"}:
        raise ConverterError("unsupported-source")
    first = normalize_date(start)
    last = normalize_date(end)
    if first and last and first > last:
        raise ConverterError("invalid-date-range")
    validation_first = normalize_date(validation_start) if validation_start is not None else first
    validation_last = normalize_date(validation_end) if validation_end is not None else last
    if validation_first and validation_last and validation_first > validation_last:
        raise ConverterError("invalid-date-range")

    words = np.frombuffer(raw, dtype="<u4", offset=8).reshape(-1, 16)
    seconds = words[:, 0].astype(np.int64)
    if np.any(seconds == 0) or np.any(np.diff(seconds) <= 0):
        raise ConverterError("invalid-time-order")
    local_clocks = (seconds + 8 * 3600).astype("datetime64[s]")
    local_days = local_clocks.astype("datetime64[D]")
    validation_scope = np.ones(len(words), dtype=np.bool_)
    if validation_first:
        validation_scope &= local_days >= np.datetime64(validation_first)
    if validation_last:
        validation_scope &= local_days <= np.datetime64(validation_last)
    prices = words[:, 1:5]
    bad_ohlc = (np.any(prices == 0, axis=1)
                | np.any(prices[:, 1, None] < prices[:, [0, 2, 3]], axis=1)
                | np.any(prices[:, 2, None] > prices[:, [0, 1, 3]], axis=1))
    warnings = []
    if np.any(bad_ohlc):
        for blocking in (True, False):
            indices = np.flatnonzero(bad_ohlc & (validation_scope if blocking else ~validation_scope))
            if not len(indices):
                continue
            detail = {"code": source.code, "period": source.period, "count": len(indices),
                      "blocking": blocking, "days": [str(day).replace("-", "") for day in np.unique(local_days[indices])],
                      "rows": [{"record": int(i), "date": str(local_days[i]),
                                "datetime": str(local_clocks[i]).replace("T", " "),
                                **{name: int(words[i, column]) / 1000
                                   for column, name in enumerate(("open", "high", "low", "close"), 1)}}
                               for i in indices[:10]]}
            if blocking:
                raise RecordValidationError("invalid-ohlc", detail)
            warnings.append({"category": "warning", "reason": "invalid-ohlc-outside-scope", **detail})
    if np.any(words[:, 14] != 0):
        raise ConverterError("unknown-status")
    amount = words[:, 8].astype(np.uint64) | (words[:, 9].astype(np.uint64) << 32)
    if np.any(amount > 2**53):
        raise ConverterError("amount-not-exactly-representable")

    selected = np.ones(len(words), dtype=np.bool_)
    if first:
        selected &= local_days >= np.datetime64(first)
    if last:
        selected &= local_days <= np.datetime64(last)
    positions = np.flatnonzero(selected)
    selected_words = words[selected]
    selected_seconds = seconds[selected]
    selected_prices = prices[selected]
    selected_amount = amount[selected]
    selected_days = local_days[selected]
    clock = pa.array(np.datetime_as_string(local_clocks[selected], unit="s"), type=pa.string())
    date_text = pc.utf8_slice_codeunits(clock, 0, 10)
    size = len(positions)
    table = pa.Table.from_arrays([
        pc.replace_substring(date_text, "-", ""),
        pa.array([source.code] * size, type=pa.string()),
        pa.array([source.market] * size, type=pa.string()),
        pc.replace_substring(clock, "T", " "),
        pc.utf8_slice_codeunits(clock, 11, 16),
        pa.array(selected_seconds * 1000, type=pa.int64()),
        pa.array(selected_prices[:, 0].astype(np.float64) / 1000, type=pa.float64()),
        pa.array(selected_prices[:, 1].astype(np.float64) / 1000, type=pa.float64()),
        pa.array(selected_prices[:, 2].astype(np.float64) / 1000, type=pa.float64()),
        pa.array(selected_prices[:, 3].astype(np.float64) / 1000, type=pa.float64()),
        pa.array(selected_words[:, 6].astype(np.int64), type=pa.int64()),
        pa.array(selected_amount.astype(np.float64), type=pa.float64()),
    ], schema=SCHEMA)
    days = {}
    unique_days, starts, counts = np.unique(selected_days, return_index=True, return_counts=True)
    single_digests = single_row_digests(table) if source.period == "1d" and np.all(counts == 1) else None
    for local_day, offset, count in zip(unique_days, starts, counts):
        day = str(local_day).replace("-", "")
        first_record = int(positions[offset])
        last_record = int(positions[offset + count - 1]) + 1
        day_table = table.slice(int(offset), int(count))
        days[day] = {
            "raw_hash": hashlib.sha256(raw[8 + first_record * 64:8 + last_record * 64]).hexdigest(),
            "digest": single_digests[int(offset)] if single_digests is not None else table_digest(day_table),
            "rows": int(count),
        }
        if source.period == "1d":
            days[day]["preclose_raw"] = words[first_record:last_record, 13].tolist()
    return Decoded(table, days, PROFILE, tuple(warnings))


def table_digest(table: pa.Table) -> str:
    if set(table.column_names) != set(SCHEMA.names):
        raise ConverterError("digest-schema-mismatch")
    table = table.select(SCHEMA.names)
    if any(table[field.name].null_count for field in SCHEMA):
        raise ConverterError("digest-null-field")
    sort_names = ["code", "timestamp"] + [name for name in SCHEMA.names if name not in {"code", "timestamp"}]
    order = pc.sort_indices(table, sort_keys=[(name, "ascending") for name in sort_names])
    sorted_table = table.take(order)
    digest = hashlib.sha256()
    digest.update(b"qmt-business-v2\x00")
    digest.update(struct.pack("<Q", sorted_table.num_rows))
    for field in SCHEMA:
        column = sorted_table[field.name].combine_chunks()
        if pa.types.is_string(field.type):
            offsets_buffer, data_buffer = column.buffers()[1:]
            offsets = np.frombuffer(offsets_buffer, dtype="<i4", count=len(column) + 1, offset=column.offset * 4)
            start_byte = int(offsets[0])
            end_byte = int(offsets[-1])
            digest.update((offsets - start_byte).astype("<i4", copy=False).tobytes())
            if data_buffer is not None:
                digest.update(memoryview(data_buffer)[start_byte:end_byte])
        elif pa.types.is_integer(field.type):
            digest.update(column.to_numpy(zero_copy_only=False).astype("<i8", copy=False).tobytes())
        else:
            digest.update(column.to_numpy(zero_copy_only=False).astype("<f8", copy=False).tobytes())
    return digest.hexdigest()
