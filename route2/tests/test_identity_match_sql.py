"""Native identity execution must preserve the former Python-token semantics."""

from contextlib import contextmanager
import re
from types import SimpleNamespace

import duckdb
import pytest

from ontology_r2 import discovery


@contextmanager
def rows(source, target):
    db = duckdb.connect(":memory:")
    data = SimpleNamespace(db=db, snapshot_id="identity-test", tables={})
    for name, fields, values in (
        ("source", ["ref", "kind", "scope"], source),
        ("target", ["code", "scope"], target),
    ):
        db.execute(f"CREATE TABLE {name} (__r2_row BIGINT, "
                   + ", ".join(f'"{field}" VARCHAR' for field in fields) + ")")
        db.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in range(len(fields)+1))})",
                       [(i, *value) for i, value in enumerate(values, 1)])
        data.tables[name] = {"sql_name": name, "column_names": fields}
    try:
        yield data
    finally:
        db.close()


def candidate():
    return {"candidate_id": "identity", "source": {"table": "source", "field": "ref"},
            "target": {"table": "target", "field": "code"}}


def test_native_identity_empty_filter_equals_python_strip_and_preserves_raw_values():
    whitespace = discovery._PYTHON_STRIP_CHARACTERS
    # Fail on a future Python Unicode update instead of silently changing identity.
    assert set(whitespace) == {chr(i) for i in range(0x110000) if chr(i).isspace()}
    values = [None, "", whitespace, "1", "01", "１", "A", "a", "\u200b", "\ufeff"]
    for char in whitespace:
        values.extend([char, char * 2, char + "A" + char])
    db = duckdb.connect(":memory:")
    try:
        actual = db.execute("SELECT ref FROM unnest(?) t(ref) WHERE trim(ref, ?) <> ''",
                            [values, whitespace]).fetchall()
        expected = [(key,) for value in values for key in discovery._match_keys(value, "identity")]
        assert actual == expected
    finally:
        db.close()


@pytest.mark.parametrize("conditional", [False, True])
def test_native_identity_matches_legacy_links_statistics_and_ambiguity(monkeypatch, conditional):
    values = [None, "", " ", "\t", "\n", "\u3000", "\x1c", " \t\u00a0",
              " A ", "A", "a", "1", "01", "１", "\u200b", "duplicate", "missing"]
    source = [(value, "linked", "north") for value in values]
    source += [("A", "outside", "north"), ("A", None, "north"),
               ("A", "linked", "south"), ("A", "linked", None)]
    target = [(value, "north") for value in values if value != "missing"]
    target += [("duplicate", "north"), ("A", "south")]
    options = {"selector": {"kind": "linked"} if conditional else {},
               "scope_bindings": {"scope": "scope"} if conditional else {}, "transform": "identity"}
    with rows(source, target) as data:
        sql, params = discovery.association_match_sql(data, candidate(), **options)
        assert "__r2_match_keys" not in sql
        assert data.db.execute("SELECT count(*) FROM duckdb_functions() "
                               "WHERE function_name='__r2_match_keys'").fetchone()[0] == 0
        native_links = sorted(data.db.execute(sql, params).fetchall())
        native_check = discovery._validate_transformed_candidate(
            data, candidate(), sample_limit=100, **options)
        assert native_check["checks"]["ambiguous_matches"] > 0
        assert native_check["checks"]["missing_in_input"] > 0

        original = discovery._matching_ctes
        data.db.create_function("__r2_match_keys", discovery._match_keys,
                                ["VARCHAR", "VARCHAR"], "VARCHAR[]")

        def legacy(*args, **kwargs):
            query, parameters, group = original(*args, **kwargs)
            # Restore only the former token CTEs; all conditions, multiplicity
            # checks and link assembly remain identical to the native path.
            query, replacements = re.subn(
                r"source_tokens AS \(.*?\), target_tokens AS \(.*?\), target_keys AS",
                "source_tokens AS (SELECT *, unnest(__r2_match_keys(ref, 'identity')) AS key "
                "FROM source_values), target_tokens AS (SELECT *, "
                "unnest(__r2_match_keys(ref, 'identity')) AS key FROM target_values), target_keys AS",
                query, flags=re.S)
            assert replacements == 1
            return query, parameters, group

        monkeypatch.setattr(discovery, "_matching_ctes", legacy)
        legacy_sql, legacy_params = discovery.association_match_sql(data, candidate(), **options)
        assert sorted(data.db.execute(legacy_sql, legacy_params).fetchall()) == native_links
        assert discovery._validate_transformed_candidate(
            data, candidate(), sample_limit=100, **options) == native_check


def test_nonidentity_still_uses_transform_udf():
    with rows([("A", "linked", "north")], [("a", "north")]) as data:
        sql, params = discovery.association_match_sql(
            data, candidate(), transform="nfkc_whitespace_casefold")
        assert "__r2_match_keys" in sql
        assert data.db.execute(sql, params).fetchall() == [(1, 1)]
