"""Bounded, deterministic recall of definition-record concept candidates.

Shared name fragments are retrieval evidence, never semantic identity.  This
module does not call an LLM, merge records, or alter the ontology build plan.
"""

from collections import defaultdict
from itertools import combinations
import re
import unicodedata

from .column_roles import is_sensitive_column
from .storage import digest, qi


ROLE_CUES = {
    "alias": (r"(?:^|_)(?:alias|aka|synonym)(?:_|$)", r"别名|同义名|曾用名"),
    "name": (r"(?:^|_)(?:name|title|label)(?:_|$)", r"名称|名字|标题"),
    "description": (r"(?:^|_)(?:definition|description|desc|meaning|comment|explanation)(?:_|$)", r"定义|说明|描述|含义|解释"),
    "formula": (r"(?:^|_)(?:formula|calculation|expression|expr|caliber|rule)(?:_|$)", r"公式|计算|口径"),
    "unit": (r"(?:^|_)(?:unit|uom)(?:_|$)", r"单位|量纲"),
    "scope": (r"(?:^|_)(?:scope|domain|region|area|range|applicable|period)(?:_|$)", r"范围|适用|业务域|地区|区域|周期"),
}
_IDENTIFIER_SUFFIX = re.compile(r"(?:^|_)(?:id|code|key|uuid|guid|no|number)$", re.I)
_IDENTIFIER_DECLARATION = re.compile(
    r"(?:编号|编码|标识符|引用键|主键|外键)(?:[，,;；。.\s]|$)|"
    r"\b(?:identifier|primary key|foreign key|reference key)\b", re.I)
_RULE_PARAMETER = re.compile(
    r"(?:^|_)(?:rule|formula|expression|calculation)_(?:type|kind|mode|operator)$", re.I)


def _raw_field_roles(table):
    """Collect metadata and source-checked candidates before conflict resolution."""
    profiles = {item["column"]: item for item in table.get("profiles", [])}
    excluded = set(table.get("semantic_excluded_columns") or ())
    selected = defaultdict(list)
    for column in table["columns"]:
        name = column["column_name"]
        if name in excluded or is_sensitive_column(column):
            continue
        profile = profiles.get(name, {})
        if profile.get("scan_scope") == "full_input" and profile.get("usable_count") == 0:
            continue
        comment = str(column.get("column_comment") or "")
        # An explicit column name is stronger than a comment mentioning several roles.
        role = next((key for key, (pattern, _) in ROLE_CUES.items()
                     if re.search(pattern, name.casefold())), None)
        if role is None and not re.search(r"(?:^|_)(?:id|code|key|uuid|time|date|no|number)(?:_|$)", name.casefold()):
            role = next((key for key, (_, pattern) in ROLE_CUES.items()
                         if re.search(pattern, comment)), None)
        if role:
            selected[role].append(name)
    # Competing source-backed role candidates coexist for recall. Neither a
    # metadata cue nor a model proposal establishes the final business role.
    for item in table.get("inferred_semantic_roles", ()):
        name, role = item.get("column"), item.get("role")
        if (item.get("status") != "source_verified_role_candidate"
                or role not in ROLE_CUES or name not in profiles
                or name in excluded or name in selected.get(role, ())):
            continue
        column = next((c for c in table["columns"] if c["column_name"] == name), None)
        if (column is None or is_sensitive_column(column)
                or profiles[name].get("scan_scope") != "full_input"
                or profiles[name].get("usable_count", 0) == 0):
            continue
        selected[role].append(name)
    return dict(selected)


def field_role_conflicts(table, roles=None):
    """Resolve strong declaration conflicts without discarding source fields.

    A key's semantic-looking prefix does not make its value a name or formula.
    Only identifier declarations/suffixes trigger this boundary; a primary key
    alone and a numeric-looking value do not. Rule contents remain candidates.
    """
    roles = _raw_field_roles(table) if roles is None else roles
    columns = {item["column_name"]: item for item in table["columns"]}
    table_name = table.get("name") or ".".join(filter(None, (
        table.get("schema"), table.get("table_name"))))
    conflicts = []
    for role in ("name", "alias", "formula"):
        for field in roles.get(role, ()):
            column = columns[field]
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", field).casefold()
            identifier_name = bool(_IDENTIFIER_SUFFIX.search(normalized))
            identifier_declared = bool(_IDENTIFIER_DECLARATION.search(str(column.get("column_comment") or "")))
            base = {"column": field, "proposed_role": role,
                    "schema_evidence_id": f"schema:{table_name}:{field}",
                    "resolution_origin": "source_declaration_conflict",
                    "semantic_identity_claim": False}
            if identifier_name or identifier_declared:
                conflicts.append({**base, "effective_role": "reference", "binding_only": True,
                    "reason": "identifier_declaration_does_not_support_name_alias_or_formula",
                    "identifier_name": identifier_name, "identifier_declared": identifier_declared,
                    "declared_primary_key": field in table.get("pk", ())})
            elif role == "formula" and _RULE_PARAMETER.search(normalized):
                conflicts.append({**base, "effective_role": "scope", "binding_only": False,
                    "formula_status": "fragment", "parameter_role": "rule_parameter",
                    "reason": "rule_type_or_mode_is_a_parameter_not_complete_rule_content"})
    return conflicts


def _field_roles(table):
    """Retain all supported roles, resolving strong key/content conflicts first.

    No per-role slots discard business fields. Resolved bindings remain in
    card references; rule parameters remain in scope and pattern identity.
    """
    selected = _raw_field_roles(table)
    for conflict in field_role_conflicts(table, selected):
        field, role = conflict["column"], conflict["proposed_role"]
        selected[role].remove(field)
        if conflict["effective_role"] == "scope" and field not in selected.setdefault("scope", []):
            selected["scope"].append(field)
    return {role: fields for role, fields in selected.items() if fields}


def _name_cues(value):
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    pieces = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized)
    cues = set()
    for piece in pieces:
        if re.fullmatch(r"[\u4e00-\u9fff]+", piece):
            cues.update("gram:" + piece[i:i + 2] for i in range(len(piece) - 1))
        elif len(piece) >= 3:
            cues.add("term:" + piece)
    if normalized:
        cues.add("exact:" + normalized)
    return cues


def _record_card(data, table_name, row, roles, max_field_chars):
    values = {}
    for role, columns in roles.items():
        entries = []
        for column in columns:
            raw = row.get(column)
            if raw is None or not str(raw).strip():
                continue
            value = str(raw)
            entries.append({"column": column, "value": value[:max_field_chars],
                            "truncated": len(value) > max_field_chars,
                            "schema_evidence_id": f"schema:{table_name}:{column}"})
        if entries:
            values[role] = entries
    if not values.get("name"):
        return None
    return {"table": table_name, "record_id": data.record_id(table_name, row),
            "row_number": row["__r2_row"], "fields": values}


def _sample_definition_rows(data, table_name, roles, limit, max_field_chars):
    table = data.tables[table_name]
    columns = list(dict.fromkeys([field for values in roles.values() for field in values] + table["pk"]))
    names = [f"nullif(trim({qi(field)}), '')" for field in roles["name"]]
    name_key = "coalesce(" + ", ".join([*names, "''"]) + ")"
    sql = (f"SELECT __r2_row, {', '.join(map(qi, columns))} FROM {qi(table['sql_name'])} "
           f"WHERE {name_key} <> '' ORDER BY md5({name_key}), __r2_row LIMIT ?")
    rows = data.db.execute(sql, [limit + 1]).fetchall()
    cards = []
    for values in rows[:limit]:
        row = dict(zip(["__r2_row", *columns], values))
        card = _record_card(data, table_name, row, roles, max_field_chars)
        if card is not None:
            cards.append(card)
    return cards, len(rows) > limit


def _unit_values(card):
    return {entry["value"].strip().casefold() for entry in card["fields"].get("unit", [])}


def recall_concept_candidates(data, *, max_definition_tables=16,
                              max_records_per_table=64, max_total_records=1024,
                              max_name_cue_fanout=32, max_pair_comparisons=10000,
                              max_candidates=200, max_field_chars=256):
    """Recall bounded record pairs from names/aliases; return evidence and coverage.

    SQL scans the imported CSV snapshot and returns at most the configured row
    budget.  The result is only a list of records worth semantic comparison.
    """
    limits = (max_definition_tables, max_records_per_table, max_total_records,
              max_name_cue_fanout, max_pair_comparisons, max_candidates,
              max_field_chars)
    if any(type(value) is not int or value <= 0 for value in limits):
        raise ValueError("Concept candidate limits must be positive integers")

    choices = []
    skipped = []
    for table_name, table in sorted(data.tables.items()):
        roles = _field_roles(table)
        if not roles.get("name"):
            skipped.append({"table": table_name, "reason": "no_usable_name_field"})
            continue
        choices.append((table_name, roles))
    choices.sort(key=lambda pair: (-sum(bool(pair[1].get(role)) for role in
                                   ("description", "formula", "unit", "scope")), pair[0]))
    skipped.extend({"table": name, "reason": "table_budget"}
                   for name, _ in choices[max_definition_tables:])
    cards = []
    coverage = []
    for table_name, roles in choices[:max_definition_tables]:
        remaining = max_total_records - len(cards)
        if remaining <= 0:
            skipped.append({"table": table_name, "reason": "record_budget"})
            continue
        limit = min(max_records_per_table, remaining)
        sampled, truncated = _sample_definition_rows(data, table_name, roles, limit, max_field_chars)
        cards.extend(sampled)
        coverage.append({"table": table_name, "input_rows": data.tables[table_name]["rows"],
                         "selected_records": len(sampled), "sample_truncated": truncated,
                         "selected_columns": roles})

    index = defaultdict(set)
    for position, card in enumerate(cards):
        names = [entry["value"] for role in ("name", "alias")
                 for entry in card["fields"].get(role, [])]
        for name in names:
            for cue in _name_cues(name):
                index[cue].add(position)
    pairs = defaultdict(set)
    skipped_fanout = 0
    comparisons = 0
    comparison_budget_exhausted = False
    for cue, holders in sorted(index.items(), key=lambda item: (len(item[1]), item[0])):
        if len(holders) > max_name_cue_fanout:
            skipped_fanout += 1
            continue
        for left, right in combinations(sorted(holders), 2):
            if comparisons >= max_pair_comparisons:
                comparison_budget_exhausted = True
                break
            comparisons += 1
            pairs[(left, right)].add(cue)
        if comparison_budget_exhausted:
            break

    ranked = sorted(pairs, key=lambda pair: (
        not any(cue.startswith("exact:") for cue in pairs[pair]),
        -len(pairs[pair]), cards[pair[0]]["record_id"], cards[pair[1]]["record_id"]))
    candidates = []
    for left, right in ranked[:max_candidates]:
        a, b = cards[left], cards[right]
        units_a, units_b = _unit_values(a), _unit_values(b)
        cues = pairs[(left, right)]
        candidates.append({
            "candidate_id": digest([data.snapshot_id, a["record_id"], b["record_id"]])[:24],
            "source_snapshot": data.snapshot_id,
            "records": [a, b],
            "shared_name_cues": sorted(cues),
            "retrieval_channels": (["normalized_exact_name"] if any(c.startswith("exact:") for c in cues) else [])
                                  + (["name_or_alias_fragment"] if any(not c.startswith("exact:") for c in cues) else []),
            "unit_observation": "different" if units_a and units_b and units_a.isdisjoint(units_b)
                                else "same" if units_a and units_b else "unknown",
            "decision": {"status": "proposed", "same_concept": "unresolved"},
        })
    return {"candidates": candidates, "coverage": {
        "input_scope": "imported_csv_snapshot_only", "definition_tables_considered": len(choices),
        "definition_tables_sampled": len(coverage), "tables": coverage,
        "tables_skipped": skipped, "definition_records_sampled": len(cards),
        "name_cues_skipped_high_fanout": skipped_fanout,
        "pair_comparisons": comparisons,
        "pair_comparison_budget_exhausted": comparison_budget_exhausted,
        "candidate_pairs_found": len(ranked), "candidates_returned": len(candidates),
        "candidate_pairs_not_returned": max(0, len(ranked) - len(candidates)),
        "partial": (bool(skipped) or any(t["sample_truncated"] for t in coverage)
                    or skipped_fanout > 0 or comparison_budget_exhausted
                    or len(ranked) > len(candidates)),
        "semantic_status": "unjudged; name overlap and units do not prove identity",
    }}
