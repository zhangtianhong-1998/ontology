"""Exact retrieval keeps its semantics while entering through value indexes."""

import pytest

from ontology_r2.semantic_cards import SemanticCardIndex, build_semantic_cards
from test_semantic_cards import _dataset


class _QueryCapture:
    def __init__(self, db, *, legacy=False):
        self.db, self.legacy, self.exact = db, legacy, []

    def execute(self, sql, params=()):
        if sql.startswith("WITH exact_ids AS MATERIALIZED"):
            if self.legacy:
                sql = ("SELECT c.*, 0.0 AS bm25 FROM cards c WHERE "
                       "(c.name_norm=? OR EXISTS (SELECT 1 FROM aliases a "
                       "WHERE a.card_id=c.card_id AND a.name_norm=?))"
                       + sql.split("WHERE 1=1", 1)[1])
            self.exact.append((sql, params))
        return self.db.execute(sql, params)


@pytest.fixture
def exact_index(tmp_path):
    data = _dataset(tmp_path)
    built = build_semantic_cards(data, tmp_path / "cards.sqlite")
    index = SemanticCardIndex(built["index_path"])
    db = index.db
    # The same card can match both name and alias. The uncertain card also
    # shares aliases, so kind/table/scope/exclusion filters must apply before
    # the exact channel's LIMIT rather than after an early ID limit.
    for row in db.execute("SELECT card_id FROM cards").fetchall():
        for alias in ("水果收入", "销售额", "revenue"):
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?)", (row[0], alias))
    db.commit()
    try:
        yield index
    finally:
        index.db = db
        index.close()
        data.close()


def test_exact_index_union_matches_legacy_results_with_all_filters(exact_index):
    index, db = exact_index, exact_index.db
    first = db.execute("SELECT * FROM cards WHERE kind='definition' ORDER BY card_id").fetchone()
    filters = [
        {}, {"kind": "definition"}, {"kind": "uncertain"},
        {"table": "fruit.fruit_definition"}, {"scope": "华东"},
        {"scope": {"scope": "华南"}}, {"root_hint": first["root_hint"]},
        {"exclude_card_id": first["card_id"]}, {"exclude_pattern_id": first["pattern_id"]},
        {"kind": "definition", "scope": "华东", "exclude_card_id": first["card_id"]},
        {"kind": "definition", "table": "fruit.fruit_definition", "scope": "华南", "limit": 1},
        {"limit": 1}, {"limit": 2},
    ]
    for query in ("水果收入", "销售额", "  ＲＥＶＥＮＵＥ  ", "收入", "不存在", ""):
        for options in filters:
            index.db = _QueryCapture(db, legacy=True)
            expected = index.search(query, **options)
            index.db = _QueryCapture(db)
            actual = index.search(query, **options)
            assert actual == expected, (query, options)
            assert len({card["card_id"] for card in actual}) == len(actual)
    index.db = db


def test_exact_lookup_plan_uses_name_alias_indexes_before_card_filters(exact_index):
    index, db = exact_index, exact_index.db
    original = dict(db.execute("SELECT * FROM cards LIMIT 1").fetchone())
    columns = list(original)
    # A bounded distractor corpus exposes the former O(kind cardinality)
    # correlated lookup without scanning the experiment's million-row index.
    records = []
    for i in range(2000):
        item = {**original, "card_id": f"distractor:{i:04}", "signature": f"signature:{i}",
                "kind": "definition", "name_norm": f"unrelated:{i}"}
        records.append(tuple(item[key] for key in columns))
    db.executemany("INSERT INTO cards VALUES (" + ",".join("?" for _ in columns) + ")", records)
    db.commit()

    steps, captures = {}, {}
    for mode in ("legacy", "indexed"):
        capture = _QueryCapture(db, legacy=mode == "legacy")
        index.db = capture
        counter = [0]

        def progress():
            counter[0] += 1
            return 0

        db.set_progress_handler(progress, 1)
        try:
            assert index.search("absent-exact-name", kind="definition") == []
        finally:
            db.set_progress_handler(None, 0)
        steps[mode], captures[mode] = counter[0], capture.exact[0]
    index.db = db
    sql, params = captures["indexed"]
    plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
    assert any("SEARCH cards USING INDEX cards_name (name_norm=?)" in row for row in plan), plan
    assert any("SEARCH aliases USING INDEX aliases_name (name_norm=?)" in row for row in plan), plan
    assert any("SEARCH c USING INDEX sqlite_autoindex_cards_1 (card_id=?)" in row for row in plan), plan
    assert not any("cards_pattern" in row for row in plan), plan
    assert steps["indexed"] < steps["legacy"] // 5, steps

