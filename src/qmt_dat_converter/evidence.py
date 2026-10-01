"""Offline, explicitly scoped evidence comparisons; never infer coverage."""

import hashlib
import json
import math
import re

from .decoder import SCHEMA, PROFILE
from .model import ConverterError


def initial_verification():
    return {"conversion_consistency": {"status": "unverified", "rule_version": "qmt-business-v2"},
            "coverage": {"status": "unverified", "reason": "No validated membership, calendar and pause evidence.", "scope": None},
            "external_evidence": {"status": "unverified", "scope": []},
            "field_profile": {"status": "unverified", "profile": PROFILE,
                              "untested": ["independent-price-scale", "independent-volume-unit", "unknown-record-words"]}}


class Evidence:
    def __init__(self, path):
        self.payload = None
        self.expected = {}
        self.matched = set()
        self.mismatches = []
        self.hash = None
        if path is None:
            return
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
            source = payload["source"]
            if (payload["version"] != 1 or not isinstance(source["description"], str)
                    or not source["description"].strip()
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", source["sha256"])
                    or source["provenance"] not in {"same-source", "independent"}
                    or not payload["comparisons"]):
                raise ValueError("invalid envelope")
            for index, item in enumerate(payload["comparisons"]):
                if (not re.fullmatch(r"\d{6}\.(SH|SZ)", item["code"])
                        or item["period"] not in {"1d", "1m"}
                        or type(item["timestamp"]) is not int or not 0 < item["timestamp"] <= 2**63 - 1
                        or not isinstance(item["fields"], dict) or not item["fields"]
                        or set(item["fields"]) - set(SCHEMA.names)):
                    raise ValueError("invalid comparison")
                for key, value in item["fields"].items():
                    if key in {"timestamp", "volume"} and type(value) is not int:
                        raise ValueError("integer expected")
                    if key in {"open", "high", "low", "close", "amount"} and (type(value) not in {int, float} or not math.isfinite(value)):
                        raise ValueError("finite number expected")
                    if key in {"code", "exchange", "trade_date", "datetime", "minute"} and not isinstance(value, str):
                        raise ValueError("string expected")
                self.expected.setdefault((item["code"], item["period"]), []).append((index, item))
            self.hash = hashlib.sha256(raw).hexdigest()
            self.payload = payload
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ConverterError(f"invalid-evidence: {exc}") from exc

    def needs(self, source):
        return (source.code, source.period) in self.expected

    def timestamps(self, source) -> tuple[int, ...]:
        return tuple(item["timestamp"] for _, item in self.expected.get((source.code, source.period), ()))

    def compare(self, source, table):
        requests = self.expected.get((source.code, source.period), [])
        if not requests:
            return
        # Only referenced timestamps enter Python; full source data stays Arrow.
        import pyarrow as pa
        import pyarrow.compute as pc
        timestamps = pa.array([item["timestamp"] for _, item in requests], type=pa.int64())
        actual = {row["timestamp"]: row for row in table.filter(pc.is_in(table["timestamp"], value_set=timestamps)).to_pylist()}
        for index, item in requests:
            row = actual.get(item["timestamp"])
            if row is not None and all(row[name] == value for name, value in item["fields"].items()):
                self.matched.add(index)
            else:
                self.mismatches.append(item)

    def result(self):
        if self.payload is None:
            return {"status": "unverified", "scope": []}
        count = len(self.payload["comparisons"])
        return {"status": "matched" if len(self.matched) == count else "mismatch",
                "provenance": self.payload["source"]["provenance"],
                "source": self.payload["source"], "evidence_sha256": self.hash,
                "scope": self.payload["comparisons"], "matched": len(self.matched), "expected": count,
                "coverage_claim_accepted": False}
