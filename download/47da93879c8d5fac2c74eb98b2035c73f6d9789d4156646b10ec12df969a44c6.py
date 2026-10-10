"""Check numerical design examples only; does not run the ontology pipeline."""
import argparse
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import yaml

EXPECTED = {
    "unrelated_numeric_overlap": 1.0,
    "whole_column_containment": 0.02,
    "conditional_containment": 1.0,
    "unscoped_target_matches": 2,
    "scoped_target_matches": 1,
    "unknown_scope_records": 1,
    "duplicate_target_multiplicity": 2,
    "skewed_row_hit_ratio": 0.999,
    "skewed_distinct_containment": 0.5,
    "raw_code_matches": 1,
    "numeric_normalized_matches": 2,
    "unsafe_concatenated_keys": 1,
    "safe_tuple_keys": 2,
    "json_path_present": 20,
    "json_path_matched": 20,
    "empty_denominator_ratio": None,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="New YAML result file")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    sql = Path(__file__).with_name("cases.sql").read_bytes()
    with duckdb.connect() as db:
        actual = dict(db.execute(sql.decode()).fetchall())
    checks = [{"name": name, "actual": actual.get(name), "expected": expected,
               "passed": name in actual and (actual[name] is None if expected is None
                                               else actual[name] is not None and math.isclose(actual[name], expected))}
              for name, expected in EXPECTED.items()]
    passed = set(actual) == set(EXPECTED) and all(c["passed"] for c in checks)
    report = {"status": "passed" if passed else "failed", "recorded_at": datetime.now(timezone.utc).isoformat(),
              "synthetic": True, "llm_calls": 0, "duckdb_version": duckdb.__version__,
              "sql_sha256": hashlib.sha256(sql).hexdigest(), "checks": checks,
              "validation_scope": "SQL measurement definitions only; no field candidate discovery or business semantic accuracy claim"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    print({"status": report["status"], "checks": len(checks), "llm_calls": 0, "output": str(output)})
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
