import hashlib
import struct

import pyarrow as pa
import pytest

from qmt_dat_converter.decoder import SCHEMA, decode, table_digest, single_row_digests
from qmt_dat_converter.model import ConverterError, Snapshot, SourceFile


HEADER = bytes.fromhex("feffffffffffff7f")


def snapshot(*rows, header=HEADER):
    raw = header + b"".join(struct.pack("<16I", *row) for row in rows)
    return Snapshot(raw=raw, signature={}, sha256=hashlib.sha256(raw).hexdigest())


def source(period="1m"):
    return SourceFile(path=None, code="600000.SH", market="SH", period=period, kind="stock")


def test_literal_record_preserves_amount_prices_volume_and_beijing_clock(row):
    item = decode(snapshot(row), source()).table.to_pylist()[0]
    assert item == {
        "trade_date": "20260922", "code": "600000.SH", "exchange": "SH",
        "datetime": "2026-09-22 11:00:00", "minute": "11:00",
        "timestamp": 1790046000000, "open": 7.1, "high": 7.35,
        "low": 7.0, "close": 7.25, "volume": 1234,
        "amount": 4294967301.0,
    }
    assert decode(snapshot(row), source()).table.schema == SCHEMA


def test_batched_single_record_digests_match_canonical_digest_for_slices_chunks_and_values(row):
    later = list(row)
    later[0] += 86400
    later[4] = 7200
    later[6] = 4000000000
    table = decode(snapshot(row, later), source("1d")).table
    table = pa.concat_tables([table.slice(1), table.slice(0, 1), table.slice(1)])
    assert single_row_digests(table) == [table_digest(table.slice(i, 1)) for i in range(3)]
    assert single_row_digests(table)[0] != single_row_digests(table)[1]


def test_daily_preclose_is_audit_evidence_not_business_column(row):
    decoded = decode(snapshot(row), source("1d"))
    assert "preclose" not in decoded.table.column_names
    assert decoded.days["20260922"]["preclose_raw"] == [7000]
    assert decoded.profile.endswith("unverified")


def test_padding_does_not_extend_volume_or_change_business_digest(row):
    changed = list(row)
    changed[10] = 4000000000
    changed[11] = 3000000000
    first = decode(snapshot(row), source())
    second = decode(snapshot(changed), source())
    assert second.table["volume"].to_pylist() == [1234]
    assert table_digest(first.table) == table_digest(second.table)
    assert first.days["20260922"]["raw_hash"] != second.days["20260922"]["raw_hash"]


@pytest.mark.parametrize("bad", [b"", b"bad", bytes(8)])
def test_unknown_or_short_header_fails(bad):
    with pytest.raises(ConverterError, match="header"):
        decode(Snapshot(bad, {}, hashlib.sha256(bad).hexdigest()), source())


def test_empty_record_area_fails():
    with pytest.raises(ConverterError, match="empty"):
        decode(snapshot(), source())


def test_partial_record_fails(row):
    valid = snapshot(row)
    with pytest.raises(ConverterError, match="length"):
        decode(Snapshot(valid.raw[:-1], {}, ""), source())


def test_out_of_order_time_fails(row):
    earlier = list(row)
    earlier[0] -= 60
    with pytest.raises(ConverterError, match="time-order"):
        decode(snapshot(row, earlier), source())


def test_unknown_status_fails(row):
    changed = list(row)
    changed[14] = 1
    with pytest.raises(ConverterError, match="status"):
        decode(snapshot(changed), source())


@pytest.mark.parametrize("index,value", [(1, 0), (2, 0), (3, 0), (4, 0), (1, 7400), (2, 7100), (3, 7300)])
def test_invalid_ohlc_fails(row, index, value):
    changed = list(row)
    changed[index] = value
    with pytest.raises(ConverterError, match="ohlc"):
        decode(snapshot(changed), source())


def test_amount_above_exact_float_bound_fails(row):
    changed = list(row)
    changed[8] = 1
    changed[9] = 1 << 21
    with pytest.raises(ConverterError, match="amount-not-exactly-representable"):
        decode(snapshot(changed), source())


def test_date_bounds_are_inclusive_and_use_beijing_date(row):
    next_day = list(row)
    next_day[0] += 86400
    decoded = decode(snapshot(row, next_day), source(), "2026-09-22", "2026-09-22")
    assert decoded.table.num_rows == 1
    assert list(decoded.days) == ["20260922"]


def test_digest_is_sorted_and_sensitive_to_business_value(row):
    first = decode(snapshot(row), source()).table
    next_row = list(row)
    next_row[0] += 60
    second = decode(snapshot(row, next_row), source()).table
    reversed_table = second.take(pa.array([1, 0]))
    assert table_digest(second) == table_digest(reversed_table)
    assert table_digest(first) != table_digest(second)
    changed = second.set_column(second.schema.get_field_index("close"), "close", pa.array([7.25, 7.26]))
    assert table_digest(second) != table_digest(changed)


def test_digest_tie_order_and_chunk_boundaries_do_not_change_values(row):
    base = decode(snapshot(row), source()).table
    index = base.schema.get_field_index("close")
    changed = base.set_column(index, base.schema.field(index), pa.array([7.26]))
    combined = pa.concat_tables([base, changed])
    reversed_table = combined.take(pa.array([1, 0]))
    assert table_digest(combined) == table_digest(reversed_table)
    assert table_digest(combined) == table_digest(combined.select(reversed(combined.column_names)))
