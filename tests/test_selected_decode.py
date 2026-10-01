import json
import hashlib
import struct

import pytest

from qmt_dat_converter.decoder import SCHEMA, decode, table_digest
from qmt_dat_converter.evidence import Evidence
from qmt_dat_converter.model import ConverterError
from test_decoder import snapshot, source


def dated_rows(row):
    rows = []
    for day in range(1, 31):
        item = list(row)
        item[0] += (day - 22) * 86400
        rows.append(item)
    return rows


def test_bounded_decode_materializes_three_days_but_inventories_all_source_days(row):
    rows = dated_rows(row)

    decoded = decode(snapshot(*rows), source("1d"), "20260922", "20260924")

    assert decoded.source_days == tuple(f"202609{day:02}" for day in range(1, 31))
    assert decoded.table["trade_date"].to_pylist() == ["20260922", "20260923", "20260924"]
    assert list(decoded.days) == ["20260922", "20260923", "20260924"]
    assert decoded.days["20260923"]["digest"] == table_digest(decoded.table.slice(1, 1))


def test_evidence_timestamp_outside_range_is_materialized_without_other_history(row, tmp_path):
    rows = dated_rows(row)
    old_timestamp = rows[2][0] * 1000
    payload = {
        "version": 1,
        "source": {"description": "independent sample", "sha256": "a" * 64,
                   "provenance": "independent"},
        "comparisons": [
            {"code": "600000.SH", "period": "1d", "timestamp": old_timestamp,
             "fields": {"close": 7.25}},
            {"code": "000001.SZ", "period": "1d", "timestamp": old_timestamp,
             "fields": {"close": 7.25}},
        ],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    evidence = Evidence(path)

    assert evidence.timestamps(source("1d")) == (old_timestamp,)
    assert evidence.timestamps(source("1m")) == ()
    decoded = decode(snapshot(*rows), source("1d"), "20260922", "20260924",
                     extra_timestamps=evidence.timestamps(source("1d")))
    evidence.compare(source("1d"), decoded.table)

    assert decoded.table["trade_date"].to_pylist() == [
        "20260903", "20260922", "20260923", "20260924"]
    assert list(decoded.days) == ["20260903", "20260922", "20260923", "20260924"]
    assert decoded.source_days == tuple(f"202609{day:02}" for day in range(1, 31))
    assert evidence.matched == {0}
    assert evidence.mismatches == []


def test_extra_timestamp_requires_exact_millisecond_match(row):
    decoded = decode(snapshot(*dated_rows(row)), source("1d"), "20260922", "20260922",
                     extra_timestamps=(row[0] * 1000 - 19 * 86400000 + 1,))

    assert decoded.table["trade_date"].to_pylist() == ["20260922"]
    assert list(decoded.days) == ["20260922"]


def test_sparse_evidence_rows_hash_only_materialized_records(row):
    first = list(row)
    middle = list(row)
    last = list(row)
    middle[0] += 60
    last[0] += 120
    decoded = decode(snapshot(first, middle, last), source("1m"), "20260923", "20260923",
                     extra_timestamps=(first[0] * 1000, last[0] * 1000))

    selected_raw = struct.pack("<16I", *first) + struct.pack("<16I", *last)
    assert decoded.table["timestamp"].to_pylist() == [first[0] * 1000, last[0] * 1000]
    assert decoded.days["20260922"]["raw_hash"] == hashlib.sha256(selected_raw).hexdigest()
    assert decoded.days["20260922"]["rows"] == 2
    assert decoded.source_days == ("20260922",)


def test_empty_selected_range_preserves_schema_and_full_inventory(row):
    decoded = decode(snapshot(*dated_rows(row)), source("1d"), "20261001", "20261002")

    assert decoded.table.schema == SCHEMA
    assert decoded.table.num_rows == 0
    assert decoded.days == {}
    assert decoded.source_days == tuple(f"202609{day:02}" for day in range(1, 31))


def test_outside_rows_still_receive_global_validation_and_ohlc_warning(row):
    rows = dated_rows(row)
    rows[0][1:5] = [7400, 7350, 7000, 7250]
    decoded = decode(snapshot(*rows), source("1d"), "20260922", "20260924")
    assert decoded.warnings[0]["reason"] == "invalid-ohlc-outside-scope"
    assert decoded.warnings[0]["days"] == ["20260901"]

    rows[0][14] = 1
    with pytest.raises(ConverterError, match="unknown-status"):
        decode(snapshot(*rows), source("1d"), "20260922", "20260924")


def test_unbounded_decode_keeps_all_business_days(row):
    decoded = decode(snapshot(*dated_rows(row)), source("1d"))

    assert tuple(decoded.days) == decoded.source_days
    assert decoded.table.num_rows == 30
