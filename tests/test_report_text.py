from copy import deepcopy

from qmt_dat_converter.evidence import initial_verification
from qmt_dat_converter.report_text import format_report


def base_report():
    return {"status": "complete", "run_id": "example", "action": "convert", "elapsed_seconds": 1.0,
            "scope": {"source": "source", "output": "output", "periods": ["1d"], "codes": [],
                      "start": None, "end": None, "mode": "incremental"},
            "counts": {"source_files": 1, "new_days": 1, "revised_days": 0, "reused_days": 0,
                       "written_partitions": 1, "content_verified_partitions": 0},
            "issues": [], "paths": {"report": "example.json"},
            "verification": initial_verification()}


def test_external_evidence_wording_preserves_provenance_and_match_counts():
    report = base_report()
    assert "无外部对照" in format_report(report)
    report["verification"]["external_evidence"] = {
        "status": "matched", "provenance": "same-source", "matched": 725, "expected": 725,
        "scope": [{"code": "600000.SH", "period": "1m"}]}
    same = format_report(report)
    assert "同源对照" in same and "725/725" in same and "非独立真值" in same
    assert "1 项" in same
    independent = deepcopy(report)
    independent["verification"]["external_evidence"]["provenance"] = "independent"
    separate = format_report(independent)
    assert "独立来源" in separate and "未经认证" in separate and "725/725" in separate
