import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_engine import setup, reasons
from qmt_dat_converter.engine import run


def evidence_file(config, value):
    path = config.source.parent / "evidence.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return replace(config, evidence=path)


def test_unknown_evidence_cannot_promote_coverage(setup):
    config, _, _ = setup
    report = run(config)
    assert report["verification"]["coverage"]["status"] == "unverified"
    assert report["verification"]["external_evidence"]["status"] == "unverified"
    assert report["verification"]["field_profile"]["status"] == "unverified"
    bare = evidence_file(config, {"version": 1, "verified": True})
    report = run(bare)
    assert "invalid-evidence" in reasons(report)
    assert report["verification"]["coverage"]["status"] == "unverified"


def test_evidence_compares_expected_values_and_preserves_provenance(setup):
    config, _, _ = setup
    payload = {"version": 1, "source": {"description": "same DAT reference export", "sha256": "a" * 64, "provenance": "same-source"}, "comparisons": [{"code": "600000.SH", "period": "1d", "timestamp": 1790046000000, "fields": {"close": 7.25, "volume": 1234, "amount": 4294967301}}]}
    report = run(evidence_file(config, payload))
    assert report["verification"]["external_evidence"]["status"] == "matched"
    assert report["verification"]["external_evidence"]["provenance"] == "same-source"
    assert report["verification"]["coverage"]["status"] == "unverified"
    payload["comparisons"][0]["fields"]["close"] = 8.0
    report = run(evidence_file(config, payload))
    assert report["status"] == "partial"
    assert "evidence-mismatch" in reasons(report)
    assert report["verification"]["external_evidence"]["status"] == "mismatch"


@pytest.mark.parametrize("timestamp,fields", [
    (9223372036854775808, {"close": 7.25}),
    (10**400, {"close": 7.25}),
    (1790046000000, {"close": 10**400}),
    (1790046000000, {"amount": 10**400}),
], ids=["timestamp-overflow", "huge-timestamp", "huge-close", "huge-amount"])
def test_oversized_evidence_numbers_return_saved_invalid_report(setup, timestamp, fields):
    config, _, target = setup
    payload = {"version": 1, "source": {"description": "numeric validation sample", "sha256": "a" * 64, "provenance": "same-source"},
               "comparisons": [{"code": "600000.SH", "period": "1d", "timestamp": timestamp, "fields": fields}]}
    report = run(evidence_file(config, payload))
    assert report["status"] == "partial"
    assert "invalid-evidence" in reasons(report)
    assert json.loads(Path(report["paths"]["report"]).read_text(encoding="utf-8")) == report
    assert not target.exists()


def test_maximum_int64_evidence_timestamp_returns_saved_missing_scope_report(setup):
    config, _, target = setup
    payload = {"version": 1, "source": {"description": "timestamp boundary sample", "sha256": "a" * 64, "provenance": "same-source"},
               "comparisons": [{"code": "600000.SH", "period": "1d", "timestamp": 9223372036854775807, "fields": {"close": 7.25}}]}
    report = run(evidence_file(config, payload))
    assert report["status"] == "partial"
    assert "invalid-evidence" not in reasons(report)
    assert "evidence-mismatch-or-missing-scope" in reasons(report)
    assert json.loads(Path(report["paths"]["report"]).read_text(encoding="utf-8")) == report
    assert not target.exists()
