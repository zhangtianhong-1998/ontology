"""Disk-backed, lexical recall of source-grounded definition and reference cards.

Every input row is inspected. A cap limits indexed *distinct cards*, not the
scan, and the coverage report names rows that could not enter the index.
Similarity is candidate evidence only; it never asserts concept identity.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from .column_roles import classify_columns
from .concept_candidates import _field_roles
from .row_semantics import classify_row_purpose, profile_joint_distinct
from .storage import digest, qi


_HAN = re.compile(r"[\u3400-\u9fff]+")
_WORDS = re.compile(r"[a-z0-9]+")
_REFERENCE = re.compile(r"(?:^|_)(?:code|id|key|source|target|ref|reference|type|field|value)(?:_|$)", re.I)
_CONTENT_REFERENCE = re.compile(r"(?:^|_)(?:code|field|value)(?:_|$)", re.I)
_PURE_TECHNICAL = re.compile(r"(?:\d+(?:\.\d+)?|[0-9a-f]{16,}|\d{4}-\d\d-\d\d)", re.I)
_CARD_ROLES = ("name", "alias", "description", "formula", "unit", "scope")


def _context_table(table_name):
    tokens = set(table_name.casefold().replace(".", "_").split("_"))
    return bool(tokens & {"rule", "config", "anchor", "log"})


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _terms(value: str) -> list[str]:
    """Use opaque ASCII FTS terms so a two-Han-character query stays one token."""
    value = _norm(value)
    found = set()
    for match in _HAN.finditer(value):
        chunk = match.group()
        for size in (2, 3):
            for index in range(len(chunk) - size + 1):
                found.add("g" + "".join(f"{ord(c):06x}" for c in chunk[index:index + size]))
        if len(chunk) == 1:
            found.add("g" + f"{ord(chunk):06x}")
    for word in _WORDS.findall(value):
        found.add("w" + word.encode("utf-8").hex())
    return sorted(found)


def _columns(table, max_unknown_fields_per_table):
    roles = _field_roles(table)
    classified = classify_columns(table)
    selected = {column for columns in roles.values() for column in columns}
    reference = [item["column"] for item in classified
                 if item["role"] in ("semantic", "unknown") and item["column"] not in selected
                 and item["column"] not in table["pk"]
                 and _REFERENCE.search(item["column"])]
    unknown = [item["column"] for item in classified if item["role"] == "unknown"]
    reference = reference[:8]
    selected.update(reference)
    unknown_available = [name for name in unknown if name not in selected]
    fallback = unknown_available[:max_unknown_fields_per_table]
    return roles, reference, fallback, {
        "unknown_columns_considered": fallback,
        "unknown_columns_not_examined": unknown_available[max_unknown_fields_per_table:],
        "reference_columns_considered": reference,
    }


def _row_card(table_name, row, roles, reference, fallback, max_field_chars, max_index_chars,
              row_purpose="unresolved"):
    if row_purpose == "business_fact":
        return None
    fields = {}
    signature_fields = {}
    for role, columns in [*((role, roles.get(role, ())) for role in _CARD_ROLES),
                          ("reference", reference), ("unknown", fallback)]:
        values = []
        originals = []
        for column in columns:
            raw = row.get(column)
            if raw is None or not str(raw).strip():
                continue
            raw = str(raw)
            if role == "unknown" and (len(raw.strip()) < 3 or _PURE_TECHNICAL.fullmatch(raw.strip())):
                continue
            originals.append([column, raw])
            values.append({"column": column, "value": raw[:max_field_chars],
                           "truncated": len(raw) > max_field_chars,
                           "schema_evidence_id": f"schema:{table_name}:{column}"})
        if values:
            fields[role] = values
            signature_fields[role] = originals
    informative_reference = any(_CONTENT_REFERENCE.search(entry["column"])
                                for entry in fields.get("reference", ()))
    if row_purpose == "configuration_data":
        kind = "reference"
    elif row_purpose == "definition_data" and fields.get("name"):
        kind = "definition"
    elif informative_reference:
        kind = "reference"
    elif fields.get("description") or fields.get("formula") or fields.get("alias") or fields.get("unknown"):
        kind = "uncertain"
    else:
        return None
    names = [value["value"] for value in fields.get("name", ())]
    aliases = [value["value"] for value in fields.get("alias", ())]
    scope = {value["column"]: value["value"] for value in fields.get("scope", ())}
    units = [value["value"] for value in fields.get("unit", ())]
    search_text = " ".join(value["value"] for role in ("name", "alias", "description", "formula", "unknown")
                           for value in fields.get(role, ()))
    reference_text = " ".join(value["value"] for value in fields.get("reference", ())
                              if _CONTENT_REFERENCE.search(value["column"]))
    search_text = " ".join(part for part in (search_text, reference_text) if part)
    index_text_truncated = len(search_text) > max_index_chars
    search_text = search_text[:max_index_chars]
    signature = digest([table_name, kind, signature_fields])
    semantic_fields = {role: [[column, _norm(raw)] for column, raw in signature_fields[role]]
                       for role in _CARD_ROLES if role in signature_fields}
    semantic_preview_truncated = any(entry["truncated"] for role in _CARD_ROLES
                                     for entry in fields.get(role, ()))
    # A pattern is a retrieval/scheduling group, never a business identity.
    # A truncated semantic preview cannot safely stand for another card, even
    # though the unabridged source value was available when fingerprinting.
    pattern_signature = digest([table_name, kind, semantic_fields,
                                signature if semantic_preview_truncated else None])
    reference_signature = (digest(signature_fields["reference"])
                           if signature_fields.get("reference") else None)
    return {"kind": kind, "table": table_name, "name": names[0] if names else "",
            "aliases": aliases, "scope": scope, "unit": units[0] if units else "",
            "fields": fields, "search_text": search_text,
            "index_text_truncated": index_text_truncated, "signature": signature,
            "pattern_signature": pattern_signature,
            "reference_signature": reference_signature,
            "semantic_preview_truncated": semantic_preview_truncated}


def _root_hint(table):
    if _context_table(table["table_name"]):
        return "GeneralObject"
    text = " ".join((table["table_name"], table.get("table_comment") or "")).casefold()
    for pattern, root in ((r"metric|指标", "Metric"), (r"measure|度量", "Measure"),
                          (r"dim|维度", "Dimension"), (r"synonym|词典|术语", "Term")):
        if re.search(pattern, text):
            return root
    return "GeneralObject"


def _connect(path):
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    return db


def _table_quotas(specs, max_cards):
    """Reserve across tables before scanning; no first-table monopoly."""
    quotas = {name: 0 for name, table, *_ in specs}
    active = [(name, table, weight) for name, table, *_, weight in specs
              if table["rows"] > 0 and weight > 0]
    active.sort(key=lambda item: (-item[2], item[0]))
    if max_cards < len(active):
        for name, _, _ in active[:max_cards]:
            quotas[name] = 1
        return quotas
    for name, _, _ in active:
        quotas[name] = 1
    remaining = max_cards - len(active)
    while remaining:
        candidates = [(name, table, weight) for name, table, weight in active
                      if quotas[name] < table["rows"]]
        if not candidates:
            break
        total_weight = sum(weight for _, _, weight in candidates)
        grants = {name: min(table["rows"] - quotas[name], remaining * weight // total_weight)
                  for name, table, weight in candidates}
        if not any(grants.values()):
            name = candidates[0][0]
            grants[name] = 1
        for name, grant in grants.items():
            quotas[name] += grant
            remaining -= grant
    return quotas


def build_semantic_cards(data, index_path, *, max_cards=200000, max_field_chars=512,
                         max_index_chars=768,
                         max_unknown_fields_per_table=4, max_omitted_examples=8,
                         progress=None):
    """Scan the whole imported snapshot and build a bounded local FTS5 index.

    The database retains every indexed row-to-card mapping. A distinct-card cap
    does not end the scan; over-cap rows are counted and example IDs reported.
    """
    limits = (max_cards, max_field_chars, max_index_chars, max_omitted_examples)
    if any(type(value) is not int or value <= 0 for value in limits):
        raise ValueError("Card limits must be positive integers")
    if type(max_unknown_fields_per_table) is not int or max_unknown_fields_per_table < 0:
        raise ValueError("max_unknown_fields_per_table must be nonnegative")
    path = Path(index_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("Semantic card index already exists: " + str(path))
    db = _connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
      CREATE TABLE cards (
        card_id TEXT PRIMARY KEY, signature TEXT NOT NULL UNIQUE,
        pattern_id TEXT NOT NULL, reference_signature TEXT,
        snapshot_id TEXT NOT NULL, table_name TEXT NOT NULL, kind TEXT NOT NULL,
        root_hint TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
        scope_json TEXT NOT NULL, unit TEXT NOT NULL,
        fields_json TEXT NOT NULL, index_text_truncated INTEGER NOT NULL,
        representative_record_id TEXT NOT NULL,
        representative_row_number INTEGER NOT NULL, occurrence_count INTEGER NOT NULL
      );
      CREATE TABLE aliases (card_id TEXT NOT NULL, name_norm TEXT NOT NULL,
                            PRIMARY KEY (card_id, name_norm));
      CREATE INDEX aliases_name ON aliases(name_norm);
      CREATE INDEX cards_table_kind ON cards(table_name, kind, card_id);
      CREATE INDEX cards_pattern ON cards(kind, pattern_id, card_id);
      CREATE INDEX cards_name ON cards(name_norm);
      CREATE TABLE card_sources (
        card_id TEXT NOT NULL, record_id TEXT NOT NULL, row_number INTEGER NOT NULL,
        PRIMARY KEY (card_id, record_id)
      );
      CREATE VIRTUAL TABLE card_fts USING fts5(card_id UNINDEXED, grams, tokenize='unicode61');
    """)
    total = Counter()
    by_table = {}
    omitted_examples = []
    indexed_cards = 0
    specs = []
    for table_name, table in data.tables.items():
        roles, reference, fallback, report = _columns(table, max_unknown_fields_per_table)
        row_purpose = classify_row_purpose(table)
        quality = (4 if roles.get("name") and any(roles.get(role) for role in
                   ("description", "formula", "unit", "scope")) else
                   3 if roles.get("name") else 2 if roles.get("description") or roles.get("formula") else 1)
        purpose_weight = {"definition_data": 8, "configuration_data": 1,
                          "unresolved": 1, "business_fact": 0}[row_purpose["purpose"]]
        weight = purpose_weight * quality
        specs.append((table_name, table, roles, reference, fallback, report, row_purpose, weight))
    quotas = _table_quotas(specs, max_cards)
    specs.sort(key=lambda item: (-item[-1], item[0]))
    task = progress.task("语义卡索引", sum(t["rows"] for t in data.tables.values())) if progress else nullcontext(None)
    try:
        with task as stage:
            unused_prior_quota = 0
            for table_name, table, roles, reference, fallback, columns_report, row_purpose, _ in specs:
                effective_quota = quotas[table_name] + unused_prior_quota
                joint_distinct = profile_joint_distinct(data, table_name, row_purpose)
                selected = list(dict.fromkeys([*table["pk"], *[field for fields in roles.values() for field in fields],
                                               *reference, *fallback]))
                selected_sql = ", ".join(qi(field) for field in selected)
                sql = f"SELECT __r2_row{', ' + selected_sql if selected else ''} FROM {qi(table['sql_name'])} ORDER BY __r2_row"
                cursor = data.db.cursor().execute(sql)
                names = [item[0] for item in cursor.description]
                counts = Counter()
                if stage:
                    stage.note(table_name)
                try:
                    while batch := cursor.fetchmany(1000):
                        for values in batch:
                            row = dict(zip(names, values))
                            counts["rows_scanned"] += 1
                            card = _row_card(table_name, row, roles, reference, fallback,
                                             max_field_chars, max_index_chars,
                                             row_purpose["purpose"])
                            if card is None:
                                counts["rows_without_card"] += 1
                                if row_purpose["purpose"] == "business_fact":
                                    counts["rows_skipped_business_fact"] += 1
                                continue
                            signature = card["signature"]
                            found = db.execute("SELECT card_id FROM cards WHERE signature=?", (signature,)).fetchone()
                            if found is None and counts["cards_indexed"] >= effective_quota:
                                counts["rows_omitted_cap"] += 1
                                if len(omitted_examples) < max_omitted_examples:
                                    omitted_examples.append({"table": table_name, "row_number": row["__r2_row"],
                                                             "reason": "distinct_card_cap"})
                                continue
                            record_id = data.record_id(table_name, row)
                            if found is None:
                                card_id = "card:" + digest([data.snapshot_id, signature])[:24]
                                root = _root_hint(table)
                                pattern_id = "pattern:" + digest([data.snapshot_id, card["pattern_signature"]])[:24]
                                db.execute("""INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                           (card_id, signature, pattern_id, card["reference_signature"],
                                            data.snapshot_id, table_name, card["kind"], root,
                                            card["name"], _norm(card["name"]),
                                            json.dumps(card["scope"], ensure_ascii=False, sort_keys=True),
                                            card["unit"], json.dumps(card["fields"], ensure_ascii=False),
                                            int(card["index_text_truncated"]),
                                            record_id, row["__r2_row"], 1))
                                for alias in card["aliases"]:
                                    db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (card_id, _norm(alias)))
                                grams = _terms(card["search_text"])
                                if grams:
                                    db.execute("INSERT INTO card_fts(card_id, grams) VALUES (?,?)",
                                               (card_id, " ".join(grams)))
                                indexed_cards += 1
                                counts["cards_indexed"] += 1
                                counts["cards_with_index_text_truncated"] += int(card["index_text_truncated"])
                            else:
                                card_id = found["card_id"]
                                db.execute("UPDATE cards SET occurrence_count=occurrence_count+1 WHERE card_id=?",
                                           (card_id,))
                            db.execute("INSERT OR IGNORE INTO card_sources VALUES (?,?,?)",
                                       (card_id, record_id, row["__r2_row"]))
                            counts["rows_indexed"] += 1
                            counts["kind_" + card["kind"]] += 1
                        if stage:
                            stage.advance(len(batch), detail=table_name)
                        db.commit()
                finally:
                    cursor.close()
                unused_prior_quota = effective_quota - counts["cards_indexed"]
                by_table[table_name] = {**counts, **columns_report,
                                        "row_purpose": row_purpose,
                                        "joint_distinct": joint_distinct,
                                        "input_rows": table["rows"],
                                        "reserved_card_quota": quotas[table_name],
                                        "effective_card_quota": effective_quota}
                total.update(counts)
        patterns_indexed = db.execute("SELECT count(DISTINCT pattern_id) FROM cards").fetchone()[0]
        definition_patterns = db.execute(
            "SELECT count(DISTINCT pattern_id) FROM cards WHERE kind='definition'").fetchone()[0]
        coverage = {
            "snapshot_id": data.snapshot_id, "scan_scope": "full_imported_csv_snapshot",
            "rows_scanned": total["rows_scanned"], "rows_indexed": total["rows_indexed"],
            "cards_indexed": indexed_cards, "patterns_indexed": patterns_indexed,
            "definition_patterns_indexed": definition_patterns,
            "pattern_semantics": "candidate_only; same pattern never merges reference variants or record identities",
            "rows_omitted_cap": total["rows_omitted_cap"],
            "rows_without_card": total["rows_without_card"],
            "rows_skipped_business_fact": total["rows_skipped_business_fact"],
            "business_fact_rows_instantiated": 0,
            "business_fact_instances_created": 0,
            "row_purpose_classification_granularity": "table_level_heuristic",
            "business_fact_processing": "excluded_from_definition_cards_only; no_fact_instance_path_in_this_stage",
            "cards_with_index_text_truncated": total["cards_with_index_text_truncated"],
            "kind_rows": {kind: total["kind_" + kind] for kind in ("definition", "reference", "uncertain")},
            "by_table": by_table, "omitted_examples": omitted_examples,
            "partial": bool(total["rows_omitted_cap"] or any(
                item["unknown_columns_not_examined"] for item in by_table.values())),
            "index_limit": max_cards, "max_index_chars": max_index_chars,
            "quota_method": "definition_priority_weighted_by_table_with_one_card_floor_and_forward_unused_capacity",
            "card_capacity_unspent": max_cards - indexed_cards,
            "indexed_record_mapping": "all rows accepted into cards",
            "lexical_method": "NFKC casefold + Chinese 2/3 grams + Latin whole words; SQLite FTS5 BM25",
            "semantic_status": "unjudged; retrieval is not concept identity",
        }
        assert coverage["rows_scanned"] == sum(t["rows"] for t in data.tables.values())
        assert coverage["rows_scanned"] == (coverage["rows_indexed"] + coverage["rows_omitted_cap"]
                                            + coverage["rows_without_card"])
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES (?,?)", ("coverage", json.dumps(coverage, ensure_ascii=False)))
        db.commit()
        return {"index_path": str(path), "coverage": coverage}
    finally:
        db.close()


class SemanticCardIndex:
    """Bounded, source-grounded retrieval from one completed index."""

    def __init__(self, index_path):
        self.path = Path(index_path)
        self.db = _connect(self.path)
        row = self.db.execute("SELECT value FROM metadata WHERE key='coverage'").fetchone()
        if row is None:
            raise ValueError("Incomplete semantic card index")
        self.coverage = json.loads(row["value"])

    def close(self):
        self.db.close()

    def _card(self, row):
        item = dict(row)
        item["table"] = item.pop("table_name")
        item["scope"] = json.loads(item.pop("scope_json"))
        item["fields"] = json.loads(item.pop("fields_json"))
        item["record_id"] = item.pop("representative_record_id")
        item["row_number"] = item.pop("representative_row_number")
        item["index_text_truncated"] = bool(item["index_text_truncated"])
        item["root_hint_basis"] = "table_name_and_comment_weak_hint_not_classification"
        # Card fields are retrieval context. Evidence IDs are registered only
        # when a group decision cites a concrete record value.
        item.pop("signature", None)
        item.pop("reference_signature", None)
        item.pop("name_norm", None)
        return item

    def get(self, card_id):
        row = self.db.execute("SELECT * FROM cards WHERE card_id=?", (card_id,)).fetchone()
        return self._card(row) if row else None

    def sources(self, card_id, *, limit=10):
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        return [dict(row) for row in self.db.execute(
            "SELECT record_id, row_number FROM card_sources WHERE card_id=? ORDER BY row_number LIMIT ?",
            (card_id, limit))]

    def all_cards(self, limit, *, kind="definition"):
        """Return the complete bounded corpus, or no cards with complete=false.

        Callers must not treat a top-k lexical result as full vector recall.
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if kind is not None and kind not in ("definition", "reference", "uncertain"):
            raise ValueError("Unknown card kind")
        total = self.db.execute("SELECT count(*) FROM cards WHERE (? IS NULL OR kind=?)",
                                (kind, kind)).fetchone()[0]
        if total > limit:
            return {"cards": [], "total": total, "complete": False,
                    "reason": "card_count_exceeds_limit"}
        rows = self.db.execute("SELECT * FROM cards WHERE (? IS NULL OR kind=?) ORDER BY card_id",
                               (kind, kind)).fetchall()
        return {"cards": [self._card(row) for row in rows], "total": total,
                "complete": True, "reason": None}

    def pattern_window(self, limit, *, window_index=0, kind="definition"):
        """Page distinct semantic *patterns*, preserving every card separately.

        Representatives are selected by stable card_id. The order interleaves
        source tables before paging, so the first page cannot be monopolized
        by a single large table. A pattern is only a candidate scheduling unit;
        its non-representative records remain unaligned.
        """
        if type(limit) is not int or not 0 < limit <= 1000:
            raise ValueError("pattern window limit must be an integer in 1..1000")
        if type(window_index) is not int or not 0 <= window_index <= 1000000:
            raise ValueError("window_index must be an integer in 0..1000000")
        if kind not in ("definition", "reference", "uncertain"):
            raise ValueError("Unknown card kind")
        total = self.db.execute(
            "SELECT count(DISTINCT pattern_id) FROM cards WHERE kind=?", (kind,)).fetchone()[0]
        total_cards = self.db.execute(
            "SELECT count(*) FROM cards WHERE kind=?", (kind,)).fetchone()[0]
        offset = window_index * limit
        rows = self.db.execute("""
          WITH grouped AS (
            SELECT pattern_id, table_name, root_hint,
                   min(card_id) AS representative_card_id,
                   count(*) AS pattern_card_count,
                   sum(occurrence_count) AS pattern_record_count,
                   count(DISTINCT reference_signature) AS reference_variant_count
            FROM cards WHERE kind=?
            GROUP BY pattern_id, table_name, root_hint
          ), numbered AS (
            SELECT *, row_number() OVER (
              PARTITION BY table_name ORDER BY pattern_id) AS table_rank
            FROM grouped
          )
          SELECT pattern_id, representative_card_id, pattern_card_count,
                 pattern_record_count, reference_variant_count
          FROM numbered
          ORDER BY table_rank, table_name, pattern_id
          LIMIT ? OFFSET ?
        """, (kind, limit, offset)).fetchall()
        before_cards = self.db.execute("""
          WITH grouped AS (
            SELECT pattern_id, table_name, count(*) AS card_count
            FROM cards WHERE kind=? GROUP BY pattern_id, table_name
          ), ranked AS (
            SELECT *, row_number() OVER (
              PARTITION BY table_name ORDER BY pattern_id) AS table_rank
            FROM grouped
          ), ordered AS (
            SELECT card_count, row_number() OVER (
              ORDER BY table_rank, table_name, pattern_id) AS global_rank
            FROM ranked
          )
          SELECT coalesce(sum(card_count), 0) FROM ordered WHERE global_rank <= ?
        """, (kind, offset)).fetchone()[0]
        patterns = []
        for row in rows:
            card = self.get(row["representative_card_id"])
            card["pattern_id"] = row["pattern_id"]
            card["pattern_card_count"] = row["pattern_card_count"]
            card["pattern_record_count"] = row["pattern_record_count"]
            card["reference_variant_count"] = row["reference_variant_count"]
            card["pattern_nonrepresentative_cards"] = row["pattern_card_count"] - 1
            card["pattern_status"] = "candidate_only_not_identity_alignment"
            patterns.append(card)
        return {"patterns": patterns, "total_patterns": total,
                "total_cards": total_cards,
                "window_index": window_index, "window_limit": limit,
                "patterns_before_window": min(total, offset),
                "patterns_after_window": max(0, total - offset - len(patterns)),
                "cards_before_window": before_cards,
                "cards_in_window": sum(item["pattern_card_count"] for item in patterns),
                "cards_after_window": max(0, total_cards - before_cards
                                          - sum(item["pattern_card_count"] for item in patterns)),
                "next_window_index": (window_index + 1 if offset + len(patterns) < total else None),
                "ordering": "interleaved_by_table_rank_then_pattern_id"}

    def seeds(self, limit, *, per_table=16, kind=None, window_index=0):
        """Round-robin one stable, bounded window of cards from each table.

        Window ``n`` reads card positions ``n * per_table`` through
        ``(n + 1) * per_table - 1`` in card-id order. Repeated runs can visit
        later definitions without loading the complete corpus into memory.
        """
        if any(type(value) is not int or not 0 < value <= 1000 for value in (limit, per_table)):
            raise ValueError("limit and per_table must be integers in 1..1000")
        if type(window_index) is not int or not 0 <= window_index <= 1000000:
            raise ValueError("window_index must be an integer in 0..1000000")
        if kind is not None and kind not in ("definition", "reference", "uncertain"):
            raise ValueError("Unknown card kind")
        tables = [row[0] for row in self.db.execute("SELECT DISTINCT table_name FROM cards ORDER BY table_name")]
        queues = {}
        for table in tables:
            rows = self.db.execute(
                "SELECT * FROM cards WHERE table_name=? AND (? IS NULL OR kind=?) "
                "ORDER BY card_id LIMIT ? OFFSET ?",
                (table, kind, kind, per_table, window_index * per_table)).fetchall()
            queues[table] = [self._card(row) for row in rows]
        result = []
        for position in range(per_table):
            for table in tables:
                if position < len(queues[table]):
                    result.append(queues[table][position])
                    if len(result) >= limit:
                        return result
        return result

    def search(self, query, *, limit=10, kind=None, table=None, scope=None,
               root_hint=None, exclude_card_id=None, exclude_pattern_id=None):
        """Return exact-name/alias and BM25 candidates with explicit filters."""
        if type(limit) is not int or not 0 < limit <= 100:
            raise ValueError("limit must be an integer in 1..100")
        if not isinstance(query, str) or len(query) > 512:
            raise ValueError("query must be a string of at most 512 characters")
        if kind is not None and kind not in ("definition", "reference", "uncertain"):
            raise ValueError("Unknown card kind")
        clauses, params = [], []
        for field, value in (("kind", kind), ("table_name", table), ("root_hint", root_hint)):
            if value is not None:
                clauses.append(f"c.{field}=?")
                params.append(value)
        if exclude_card_id is not None:
            clauses.append("c.card_id<>?")
            params.append(exclude_card_id)
        if exclude_pattern_id is not None:
            clauses.append("c.pattern_id<>?")
            params.append(exclude_pattern_id)
        if isinstance(scope, dict):
            clauses.append("c.scope_json=?")
            params.append(json.dumps(scope, ensure_ascii=False, sort_keys=True))
        elif isinstance(scope, str):
            clauses.append("EXISTS (SELECT 1 FROM json_each(c.scope_json) WHERE value=?)")
            params.append(scope)
        elif scope is not None:
            raise ValueError("scope must be a string, mapping, or null")
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        query_norm = _norm(query)
        if not query_norm:
            return []
        results = {}
        # Exact matches are an independent channel and are not limited by FTS tokenization.
        exact = self.db.execute(
            "SELECT c.*, 0.0 AS bm25 FROM cards c WHERE "
            "(c.name_norm=? OR EXISTS (SELECT 1 FROM aliases a WHERE a.card_id=c.card_id AND a.name_norm=?))"
            + where + " ORDER BY c.card_id LIMIT ?", [query_norm, query_norm, *params, limit]).fetchall()
        for row in exact:
            card = self._card(row)
            card.update(retrieval_channels=["exact_name_or_alias"], bm25=None)
            results[card["card_id"]] = card
        terms = _terms(query_norm)
        if terms:
            expression = " OR ".join('"' + term + '"' for term in terms)
            lexical = self.db.execute(
                "SELECT c.*, bm25(card_fts) AS bm25 FROM card_fts JOIN cards c "
                "ON c.card_id=card_fts.card_id WHERE card_fts MATCH ?" + where
                + " ORDER BY bm25(card_fts), c.card_id LIMIT ?",
                [expression, *params, limit]).fetchall()
            for rank, row in enumerate(lexical, 1):
                card = results.get(row["card_id"])
                if card is None:
                    card = self._card(row)
                    card.update(retrieval_channels=[], bm25=row["bm25"])
                    results[card["card_id"]] = card
                else:
                    card["bm25"] = row["bm25"]
                card["retrieval_channels"].append("bm25_zh_2_3gram")
                card["lexical_rank"] = rank
        ordered = sorted(results.values(), key=lambda item: (
            "exact_name_or_alias" not in item["retrieval_channels"],
            item["bm25"] if item["bm25"] is not None else 0,
            item["card_id"]))[:limit]
        for item in ordered:
            item["candidate_status"] = "unjudged"
            item["retrieval_scope"] = "indexed_cards_only"
        return ordered
