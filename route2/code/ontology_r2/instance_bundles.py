"""Bounded cross-table evidence packets from verified rules and definition cards."""

from __future__ import annotations

import json
from math import sqrt
from collections import defaultdict

from .concept_candidates import _field_roles
from .embedding import top_cosine
from .row_bundles import joint_examples
from .semantic_cards import _context_table, _norm, _root_hint
from .storage import digest


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _limits(options):
    defaults = {"max_concept_bundles": 12, "max_relation_bundles": 12,
                "max_seeds_per_table": 8, "lexical_top_k": 8,
                "max_candidates_per_bundle": 3, "max_bundle_bytes": 16000,
                "max_vector_cards": 20000, "vector_top_k": 8,
                "max_joint_pairs_per_rule": 2, "max_pattern_seed_pool": 1000}
    limits = {key: options.get(key, value) for key, value in defaults.items()}
    if any(type(value) is not int or value < 0 for value in limits.values()):
        raise ValueError("Instance bundle limits must be nonnegative integers")
    if not 0 <= limits["max_joint_pairs_per_rule"] <= 2:
        raise ValueError("max_joint_pairs_per_rule must be 0..2")
    if limits["max_bundle_bytes"] < 1000 or limits["max_candidates_per_bundle"] < 1:
        raise ValueError("Bundle bytes and candidate count are too small")
    if not 1 <= limits["max_pattern_seed_pool"] <= 1000:
        raise ValueError("max_pattern_seed_pool must be 1..1000")
    return limits


def _card_text(card):
    parts = []
    for role in ("name", "alias", "description", "formula", "unit", "scope", "unknown"):
        parts.extend(str(item["value"]) for item in card.get("fields", {}).get(role, ()))
    return "\n".join(parts)[:2400]


def _exact_evidence_signature(card):
    """Canonical *literal* evidence identity, never fuzzy concept identity.

    The field names and complete values are retained. Thus equal labels with
    different definitions, units, scopes or reference columns are not skipped.
    This intentionally under-deduplicates across heterogeneous schemas.
    """
    fields = card.get("fields", {})
    if any(entry.get("truncated") for entries in fields.values() for entry in entries):
        # A preview can hide a distinguishing suffix. Never coalesce two
        # truncated cards by their visible prefix alone.
        return "incomplete_evidence:" + str(card.get("card_id") or card.get("record_id")
                                            or digest(card))
    canonical = [(role, sorted((entry["column"], _norm(str(entry["value"])),
                                bool(entry.get("truncated")))
                               for entry in entries))
                 for role, entries in sorted(fields.items())]
    return "evidence:" + digest([card.get("table"), card.get("kind"),
                                  card.get("root_hint"), canonical])[:32]


def _sufficient_single_definition(card):
    fields = card.get("fields", {})
    names = fields.get("name", ())
    details = [entry for role in ("description", "formula")
               for entry in fields.get(role, ())]
    return bool(names and all(not entry.get("truncated") for entry in names)
                and any(not entry.get("truncated") and len(str(entry["value"]).strip()) >= 4
                        for entry in details))


def _exact_relation_signature(rule):
    basis = {key: rule.get(key) for key in
             ("candidate_id", "source", "target", "transform", "selector",
              "scope_bindings", "verification")}
    return "relation_evidence:" + digest(basis)[:32]


def _compatible(seed, candidate):
    if seed["card_id"] == candidate["card_id"]:
        return False
    if seed.get("pattern_id") and seed.get("pattern_id") == candidate.get("pattern_id"):
        return False
    a, b = seed.get("root_hint"), candidate.get("root_hint")
    # A metric may refer to measures, and table-name hints are not a business
    # classification. Cross-root retrieval is candidate context only.
    if {a, b} == {"Metric", "Measure"}:
        return True
    return not (a and b and a != "GeneralObject" and b != "GeneralObject" and a != b)


def _vector_pool(index, limits, embedding, enabled):
    report = {"enabled": bool(enabled), "status": "disabled", "cards_total": 0,
              "cards_encoded": 0, "patterns_total": 0,
              "source_cards_total": 0, "model_sha256": None}
    if not enabled:
        return None, report
    if embedding is None:
        report["status"] = "model_unavailable"
        return None, report
    total = index.db.execute("SELECT count(DISTINCT pattern_id) FROM cards WHERE kind='definition'").fetchone()[0]
    source_cards = index.db.execute("SELECT count(*) FROM cards WHERE kind='definition'").fetchone()[0]
    report.update(cards_total=total, patterns_total=total, source_cards_total=source_cards)
    report["model_sha256"] = embedding.model_sha256
    if total > limits["max_vector_cards"]:
        report["status"] = "over_pattern_cap"
        return None, report
    cards = []
    window_index = 0
    while len(cards) < total:
        window = index.pattern_window(1000, window_index=window_index)
        cards.extend(window["patterns"])
        window_index += 1
        if not window["patterns"]:
            raise ValueError("Pattern window ended before its declared total")
    if not cards:
        report["status"] = "no_definition_cards"
        return None, report
    vectors = embedding.documents([_card_text(card) for card in cards])
    report.update(status="complete", cards_encoded=len(cards))
    return (cards, vectors), report


def _vector_hits(seed, pool, embedding, limit):
    if pool is None:
        return []
    cards, vectors = pool
    query = embedding.query(_card_text(seed))
    # Filter after an enlarged local pool; exact ties keep indexed card order.
    ranked = top_cosine(vectors, query, min(len(cards), max(limit * 8, 32)))
    result = []
    for position, score in ranked:
        card = cards[position]
        if not _compatible(seed, card):
            continue
        result.append((card, score))
        if len(result) >= limit:
            break
    return result


def _select_candidates(seed, lexical, vectors, limit):
    fused = {}
    for rank, card in enumerate(lexical, 1):
        if not _compatible(seed, card):
            continue
        item = fused.setdefault(card["card_id"], {"card": card, "channels": [],
                                                    "ranks": {}, "rrf": 0.0})
        item["channels"].extend(card["retrieval_channels"])
        item["ranks"]["lexical"] = rank
        item["rrf"] += 1 / (60 + rank)
    for rank, (card, similarity) in enumerate(vectors, 1):
        item = fused.setdefault(card["card_id"], {"card": card, "channels": [],
                                                    "ranks": {}, "rrf": 0.0})
        item["channels"].append("vector")
        item["ranks"]["vector"] = rank
        item["cosine"] = similarity
        item["rrf"] += 1 / (60 + rank)
    return sorted(fused.values(), key=lambda item: (-item["rrf"], item["card"]["card_id"]))[:limit]


def _select_seeds(pool, limit, *, population_by_root=None, population_by_table=None):
    """One floor per available root/table, then sqrt-population fair scheduling.

    Population comes from the *complete indexed definition corpus* when called
    by the pipeline. A truncated retrieval pool only bounds which cards are
    available; it is never used as the population estimate in that path.
    """
    if limit <= 0 or not pool:
        return []
    order = ("Metric", "Measure", "Dimension", "Term", "GeneralObject")
    root_rank = {root: position for position, root in enumerate(order)}
    buckets = defaultdict(list)
    for card in pool:
        buckets[(card.get("root_hint") or "GeneralObject", card["table"])].append(card)
    for cards in buckets.values():
        def quality(card):
            fields = card.get("fields", {})
            return (2 * bool(fields.get("description")) + 2 * bool(fields.get("formula"))
                    + bool(fields.get("unit")) + bool(fields.get("alias")))
        cards.sort(key=lambda card: (-quality(card), card["card_id"]))
        cards.reverse()  # pop() retrieves the best card without shifting lists.
    fallback_roots = defaultdict(int)
    fallback_tables = defaultdict(int)
    for (root, table), cards in buckets.items():
        fallback_roots[root] += len(cards)
        fallback_tables[table] += len(cards)
    roots = sorted({root for root, _ in buckets}, key=lambda root: (root_rank.get(root, len(order)), root))
    full_roots = population_by_root or fallback_roots
    full_tables = population_by_table or fallback_tables
    selected, root_uses, table_uses = [], defaultdict(int), defaultdict(int)

    def choose(bucket):
        root, table = bucket
        selected.append(buckets[bucket].pop())
        root_uses[root] += 1
        table_uses[table] += 1

    def table_weight(bucket):
        return sqrt(max(1, full_tables.get(bucket[1], fallback_tables[bucket[1]])))

    def best_table(root, *, unrepresented=False):
        choices = [key for key, cards in buckets.items()
                   if key[0] == root and cards and (not unrepresented or table_uses[key[1]] == 0)]
        if not choices:
            return None
        return min(choices, key=lambda key: (
            (table_uses[key[1]] + 1) / table_weight(key), key[1]))

    # A floor is useful even for a small Dimension/Term corpus. When the
    # budget permits, follow it with one seed from every source table.
    for root in roots:
        if len(selected) >= limit:
            break
        table = best_table(root)
        if table:
            choose(table)
    for root, table in sorted(buckets, key=lambda key: (
            root_rank.get(key[0], len(order)), key[0], key[1])):
        if len(selected) >= limit:
            break
        if buckets[(root, table)] and table_uses[table] == 0:
            choose((root, table))

    while len(selected) < limit:
        available = [root for root in roots if best_table(root) is not None]
        if not available:
            break
        root = min(available, key=lambda name: (
            (root_uses[name] + 1) / sqrt(max(1, full_roots.get(name, fallback_roots[name]))),
            root_rank.get(name, len(order)), name))
        choose(best_table(root))
    return selected


def _concept_bundle(data, seed, candidates, byte_limit):
    records = [seed]
    retrieval = []
    for candidate in candidates:
        card = candidate["card"]
        records.append(card)
        retrieval.append({"candidate_id": card["card_id"], "channels": sorted(set(candidate["channels"])),
                          "ranks": candidate["ranks"], "rrf": round(candidate["rrf"], 6),
                          "candidate_status": "unjudged"})
    bundle = {"task_kind": "concept_induction", "snapshot_id": data.snapshot_id,
              "semantic_signature": _exact_evidence_signature(seed),
              "seed_ids": [seed["card_id"]], "records": records,
              "pattern": {"pattern_id": seed.get("pattern_id"),
                          "card_count": seed.get("pattern_card_count", 1),
                          "reference_variant_count": seed.get("reference_variant_count", 0),
                          "nonrepresentative_cards_candidate_only": seed.get(
                              "pattern_nonrepresentative_cards", 0),
                          "status": "candidate_only_not_business_identity"},
              "exact_alignment_record_ids": [seed["record_id"]],
              "retrieval": retrieval,
              "examples": {"candidate_only": [card["card_id"] for card in records[1:]],
                           "validated_edges": []},
              "limits": {"semantic_similarity_is_identity": False,
                         "pattern_same_is_identity": False,
                         "only_representative_record_may_be_exact": True}}
    while _size(bundle) > byte_limit and len(records) > 1:
        records.pop()
        retrieval.pop()
        bundle["examples"]["candidate_only"] = [card["card_id"] for card in records[1:]]
    if _size(bundle) > byte_limit:
        return None, "seed_over_budget"
    if len(records) < 2 and not _sufficient_single_definition(seed):
        return None, "no_candidate_and_seed_lacks_complete_definition"
    bundle["input_mode"] = "comparison" if len(records) > 1 else "single_definition"
    bundle["bundle_id"] = "bundle:" + digest([data.snapshot_id, bundle["task_kind"],
                                               seed["card_id"], [r["card_id"] for r in records]])[:24]
    bundle["size"] = {"bundle_bytes": _size(bundle), "limit_bytes": byte_limit}
    return bundle, None


def _example_fields(table, key):
    roles = _field_roles(table)
    selected = [key]
    for role in ("name", "description", "formula", "unit", "scope", "alias"):
        selected.extend(roles.get(role, ())[:1])
    return list(dict.fromkeys(selected))[:7]


def _example_record(data, item, key):
    columns = {col["column_name"]: col for col in data.tables[item["table"]]["columns"]}
    fields = defaultdict(list)
    roles = _field_roles(data.tables[item["table"]])
    role_by_field = {field: role for role, names in roles.items() for field in names}
    for column, value in item["fields"].items():
        if value is None or str(value).strip() == "":
            continue
        role = role_by_field.get(column, "reference" if column == key else "context")
        fields[role].append({"column": column, "value": str(value)[:512],
                             "truncated": len(str(value)) > 512,
                             "schema_evidence_id": f"schema:{item['table']}:{column}",
                             "column_comment": columns[column].get("column_comment")})
    return {"table": item["table"], "record_id": item["record_id"],
            "row_number": item["row_number"], "fields": dict(fields)}


def _relation_bundle(data, rule, limits):
    source, target = rule["source"], rule["target"]
    if rule["status"] != "checked_technical" or rule.get("transform", {}).get("operator") != "identity":
        return None, "rule_not_compilable"
    source_info, target_info = data.tables[source["table"]], data.tables[target["table"]]
    source_root, target_root = _root_hint(source_info), _root_hint(target_info)
    source_tokens = set(source_info["table_name"].casefold().split("_"))
    target_tokens = set(target_info["table_name"].casefold().split("_"))
    # A shared definition key commonly joins two records describing the same
    # concept. That is an alignment lead, not an object-to-object predicate.
    same_definition_surface = (source_root == target_root != "GeneralObject" or
                               (_context_table(source_info["table_name"]) and target_root != "GeneralObject") or
                               (_context_table(target_info["table_name"]) and source_root != "GeneralObject"))
    if (same_definition_surface and source["field"] == target["field"]
            and not ({"member", "value", "item", "child"} & (source_tokens | target_tokens))):
        return None, "same_concept_key_requires_alignment"
    # Equal descriptive literals (unit/definition/formula) describe or align
    # records; they are not an executable reference between business objects.
    non_reference_roles = ("unit", "description", "formula")
    for endpoint, info in ((source, source_info), (target, target_info)):
        roles = _field_roles(info)
        if any(endpoint["field"] in roles.get(role, ()) for role in non_reference_roles):
            return None, "descriptive_value_requires_alignment"
    inverse_scope = {target_field: source_field
                     for source_field, target_field in rule.get("scope_bindings", {}).items()}
    check = {"candidate_id": rule["candidate_id"], "snapshot_id": data.snapshot_id,
             "source": source, "target": target, "decision": {"status": "checked"},
             "scan_scope": "full_input", "normalization": "identity",
             "selector": rule.get("selector") or {}, "scope_bindings": inverse_scope,
             "checks": rule["verification"]["checks"]}
    candidate = {"candidate_id": rule["candidate_id"], "source": source, "target": target}
    examples = joint_examples(
        data, candidate, check, limit=limits["max_joint_pairs_per_rule"],
        source_fields=_example_fields(data.tables[source["table"]], source["field"]),
        target_fields=_example_fields(data.tables[target["table"]], target["field"]),
    )
    records, positives = {}, []
    for pair in examples["matched_pairs"]:
        left = _example_record(data, pair["source_record"], source["field"])
        right = _example_record(data, pair["target_record"], target["field"])
        records[left["record_id"]] = left
        records[right["record_id"]] = right
        positives.append({"source_record_id": left["record_id"],
                          "target_record_id": right["record_id"],
                          "matching_raw_value": pair["matching_raw_value"]})
    if not positives:
        return None, "no_unique_example"
    negatives = []
    for counter in examples.get("counterexamples", []):
        item = _example_record(data, counter["source_record"], source["field"])
        records[item["record_id"]] = item
        negatives.append({"record_id": item["record_id"], "reason": counter["reason"]})
    bundle = {"task_kind": "relation_meaning", "snapshot_id": data.snapshot_id,
              "semantic_signature": _exact_relation_signature(rule),
              "seed_ids": [rule["rule_id"]], "rule": rule,
              "records": list(records.values()),
              "examples": {"positive": positives, "counterexamples": negatives},
              "retrieval": [{"candidate_id": rule["candidate_id"],
                             "channels": rule.get("retrieval_channels", []),
                             "candidate_status": "checked_technical"}],
              "limits": {"technical_match_is_business_relation": False}}
    # Prefer preserving a positive pair and its counterexample over a second pair.
    while _size(bundle) > limits["max_bundle_bytes"] and len(positives) > 1:
        removed = positives.pop()
        keep = {value for pair in positives for value in
                (pair["source_record_id"], pair["target_record_id"])}
        keep.update(item["record_id"] for item in negatives)
        bundle["records"] = [item for item in bundle["records"] if item["record_id"] in keep]
    if _size(bundle) > limits["max_bundle_bytes"]:
        return None, "relation_over_budget"
    bundle["bundle_id"] = "bundle:" + digest([data.snapshot_id, bundle["task_kind"],
                                               rule["rule_id"], positives])[:24]
    bundle["size"] = {"bundle_bytes": _size(bundle), "limit_bytes": limits["max_bundle_bytes"]}
    return bundle, None


def _round_robin_rules(rules, limit):
    chosen = []
    # Common numeric auto IDs collide easily. Keep them in the evidence pool,
    # but spend the scarce LLM slots on stronger field pairs first.
    for numeric_only in (False, True):
        groups = defaultdict(list)
        for rule in sorted(rules, key=lambda item: (-_reference_priority(item), item["rule_id"])):
            if (rule["status"] == "checked_technical"
                    and bool(rule.get("numeric_overlap_only", False)) == numeric_only):
                groups[rule["source"]["table"]].append(rule)
        while groups and len(chosen) < limit:
            for table in sorted(groups, key=lambda name: (-_reference_priority(groups[name][0]), name)):
                if groups[table]:
                    chosen.append(groups[table].pop(0))
                    if len(chosen) >= limit:
                        break
            groups = {table: remaining for table, remaining in groups.items() if remaining}
        if len(chosen) >= limit:
            break
    return chosen


def _reference_priority(rule):
    """Spend scarce packet slots on likely reference keys, not name collisions."""
    source = rule.get("source", {}).get("field", "").casefold()
    target = rule.get("target", {}).get("field", "").casefold()
    target_table = rule.get("target", {}).get("table", "").casefold()
    key_suffix = ("_id", "_code", "_key")
    score = 2 * source.endswith(key_suffix) + target.endswith(key_suffix)
    score += 3 * any(token in source and token in target_table
                     for token in ("measure", "metric", "dimension", "dim"))
    return score


def build_instance_bundles(data, index, association, options=None, *, embedding=None):
    """Build auditable packets without per-row LLM calls or similarity-as-fact."""
    options = options or {}
    limits = _limits(options)
    vector_pool, vector_report = _vector_pool(
        index, limits, embedding, options.get("vector_enabled", False))
    bundles, skipped = [], []
    if limits["max_concept_bundles"] > 1000:
        raise ValueError("max_concept_bundles exceeds the bounded seed selector")
    seed_window_index = options.get("seed_window_index", 0)
    if type(seed_window_index) is not int or not 0 <= seed_window_index <= 1000000:
        raise ValueError("seed_window_index must be an integer in 0..1000000")
    total_definition = index.db.execute("SELECT count(*) FROM cards WHERE kind='definition'").fetchone()[0]
    definition_by_table = ({row[0]: row[1] for row in index.db.execute(
        "SELECT table_name, count(*) FROM cards WHERE kind='definition' GROUP BY table_name ORDER BY table_name")}
        if total_definition else {})
    definition_by_root = ({row[0]: row[1] for row in index.db.execute(
        "SELECT root_hint, count(*) FROM cards WHERE kind='definition' GROUP BY root_hint ORDER BY root_hint")}
        if total_definition else {})
    pattern_by_table = ({row[0]: row[1] for row in index.db.execute(
        "SELECT table_name, count(DISTINCT pattern_id) FROM cards WHERE kind='definition' "
        "GROUP BY table_name ORDER BY table_name")}
        if total_definition else {})
    pattern_by_root = ({row[0]: row[1] for row in index.db.execute(
        "SELECT root_hint, count(DISTINCT pattern_id) FROM cards WHERE kind='definition' "
        "GROUP BY root_hint ORDER BY root_hint")}
        if total_definition else {})
    # A page must fit in the concept budget. Otherwise, e.g. a pool of 1000
    # with 80 packets silently strands the other 920 patterns inside page 0:
    # page 1 starts after pattern 1000, not after the 80 examined patterns.
    requested_seed_pool = limits["max_pattern_seed_pool"]
    window_pattern_limit = (min(requested_seed_pool, limits["max_concept_bundles"])
                            if limits["max_concept_bundles"] else requested_seed_pool)
    pattern_window = (index.pattern_window(window_pattern_limit,
                                           window_index=seed_window_index, kind="definition")
                      if limits["max_concept_bundles"] else
                      {"patterns": [], "total_patterns": sum(pattern_by_table.values()),
                       "patterns_before_window": 0, "patterns_after_window": sum(pattern_by_table.values()),
                       "cards_before_window": 0, "cards_in_window": 0,
                       "cards_after_window": total_definition, "next_window_index": None})
    seed_pool = pattern_window["patterns"]
    # Keep filling the finite packet budget when an early seed is a duplicate
    # or lacks enough definition evidence. This cannot exceed the seed pool.
    seeds = _select_seeds(seed_pool, len(seed_pool),
                          population_by_root=pattern_by_root,
                          population_by_table=pattern_by_table)
    concept_signatures = set()
    concept_seeds_inspected = 0
    concept_bundles = 0
    exact_duplicate_seeds = 0
    singleton_bundles = 0
    for seed in seeds:
        if concept_bundles >= limits["max_concept_bundles"]:
            break
        concept_seeds_inspected += 1
        signature = _exact_evidence_signature(seed)
        if signature in concept_signatures:
            exact_duplicate_seeds += 1
            skipped.append({"seed_id": seed["card_id"], "reason": "exact_evidence_already_packaged"})
            continue
        query = seed.get("name") or _card_text(seed)[:160]
        lexical = index.search(query, limit=max(1, limits["lexical_top_k"]),
                               kind="definition", exclude_card_id=seed["card_id"],
                               exclude_pattern_id=seed.get("pattern_id"))
        vectors = _vector_hits(seed, vector_pool, embedding,
                               limits["vector_top_k"]) if vector_pool else []
        selected = _select_candidates(seed, lexical, vectors,
                                      limits["max_candidates_per_bundle"])
        bundle, reason = _concept_bundle(data, seed, selected, limits["max_bundle_bytes"])
        if bundle:
            bundles.append(bundle)
            concept_signatures.add(signature)
            concept_bundles += 1
            singleton_bundles += bundle["input_mode"] == "single_definition"
        else:
            skipped.append({"seed_id": seed["card_id"], "reason": reason})
    checked_rules = sum(item["status"] == "checked_technical"
                        for item in association.get("rules", []))
    # Inspect additional checked rules when an earlier one is only a concept
    # alignment lead. A skipped lead must not consume a relation-bundle slot.
    ordered_rules = _round_robin_rules(association.get("rules", []), checked_rules)
    rules = []
    relation_bundles = 0
    relation_signatures = set()
    exact_duplicate_rules = 0
    for rule in ordered_rules:
        if relation_bundles >= limits["max_relation_bundles"]:
            break
        rules.append(rule)
        relation_signature = _exact_relation_signature(rule)
        if relation_signature in relation_signatures:
            exact_duplicate_rules += 1
            skipped.append({"seed_id": rule["rule_id"], "reason": "exact_relation_already_packaged"})
            continue
        bundle, reason = _relation_bundle(data, rule, limits)
        if bundle:
            bundles.append(bundle)
            relation_bundles += 1
            relation_signatures.add(relation_signature)
        else:
            skipped.append({"seed_id": rule["rule_id"], "reason": reason})
    seeded_by_table = defaultdict(int)
    for seed in seeds[:concept_seeds_inspected]:
        seeded_by_table[seed["table"]] += 1
    packet_cards_by_table = defaultdict(set)
    bundled_pattern_by_table = defaultdict(set)
    for bundle in bundles:
        if bundle["task_kind"] == "concept_induction":
            if bundle.get("pattern", {}).get("pattern_id"):
                bundled_pattern_by_table[bundle["records"][0]["table"]].add(
                    bundle["pattern"]["pattern_id"])
            for card in bundle["records"]:
                packet_cards_by_table[card["table"]].add(card["card_id"])
    window_pattern_by_table = defaultdict(int)
    window_reference_variants = defaultdict(int)
    for pattern in seed_pool:
        window_pattern_by_table[pattern["table"]] += 1
        window_reference_variants[pattern["table"]] += pattern["reference_variant_count"]
    inventory = {}
    for table_name, count in definition_by_table.items():
        sample = [dict(card_id=row[0], name=row[1], root_hint=row[2],
                       status="candidate_only")
                  for row in index.db.execute(
                      "SELECT card_id, name, root_hint FROM cards WHERE kind='definition' "
                      "AND table_name=? ORDER BY card_id LIMIT 5", (table_name,))]
        inventory[table_name] = {
            "definition_cards_indexed": count,
            "definition_patterns_indexed": pattern_by_table.get(table_name, 0),
            "patterns_in_requested_window": window_pattern_by_table[table_name],
            "patterns_with_built_bundle": len(bundled_pattern_by_table[table_name]),
            "patterns_unprocessed": max(0, pattern_by_table.get(table_name, 0)
                                        - len(bundled_pattern_by_table[table_name])),
            "reference_variants_in_requested_window": window_reference_variants[table_name],
            "seeds_inspected": seeded_by_table[table_name],
            "cards_in_candidate_bundles": len(packet_cards_by_table[table_name]),
            "cards_not_in_candidate_bundles": max(0, count - len(packet_cards_by_table[table_name])),
            "candidate_only_sample": sample,
        }
    coverage = {"snapshot_id": data.snapshot_id, "definition_cards_indexed": total_definition,
                "seed_window_index": seed_window_index,
                "seed_window_pattern_limit": window_pattern_limit,
                "seed_window_pattern_limit_requested": requested_seed_pool,
                "seed_window_limited_by_concept_budget": window_pattern_limit < requested_seed_pool,
                "next_seed_window_index": pattern_window["next_window_index"],
                "seed_window_order": "interleaved_by_table_rank_then_pattern_id",
                "definition_cards_in_requested_window": pattern_window["cards_in_window"],
                "definition_cards_before_requested_window": pattern_window["cards_before_window"],
                "definition_cards_after_requested_window": pattern_window["cards_after_window"],
                "definition_cards_in_window_not_loaded_due_to_pool_cap": 0,
                "seed_windows_to_visit_indexed_cards": (
                    (pattern_window["total_patterns"] + window_pattern_limit - 1)
                    // window_pattern_limit),
                "definition_patterns_indexed": pattern_window["total_patterns"],
                "definition_patterns_in_requested_window": len(seed_pool),
                "definition_patterns_before_requested_window": pattern_window["patterns_before_window"],
                "definition_patterns_after_requested_window": pattern_window["patterns_after_window"],
                "definition_patterns_inspected": concept_seeds_inspected,
                "definition_patterns_with_built_bundle": sum(len(v) for v in bundled_pattern_by_table.values()),
                "definition_patterns_unprocessed": max(0, pattern_window["total_patterns"]
                                                        - sum(len(v) for v in bundled_pattern_by_table.values())),
                "definition_cards_not_submitted_as_exact_candidates": max(
                    0, total_definition - concept_bundles),
                "definition_cards_not_in_candidate_bundles": max(
                    0, total_definition - len({card_id for ids in packet_cards_by_table.values()
                                               for card_id in ids})),
                "pattern_variants_candidate_only_in_requested_window": sum(
                    item["pattern_nonrepresentative_cards"] for item in seed_pool),
                "pattern_reference_variants_in_requested_window": sum(
                    item["reference_variant_count"] for item in seed_pool),
                "definition_seed_pool_examined": len(seed_pool),
                "definition_seeds_selected": concept_seeds_inspected,
                "seed_roots": {root: sum(item.get("root_hint") == root for item in seeds[:concept_seeds_inspected])
                               for root in ("GeneralObject", "Measure", "Metric", "Dimension", "Term")},
                "definition_cards_not_seeded": max(0, total_definition - concept_seeds_inspected),
                "definition_cards_by_table": definition_by_table,
                "definition_cards_by_root": definition_by_root,
                "definition_patterns_by_table": pattern_by_table,
                "definition_patterns_by_root": pattern_by_root,
                "seed_selection_method": "one_available_root_and_table_floor_then_sqrt_full_indexed_pattern_counts",
                "definition_inventory_by_table": inventory,
                "concept_bundles_by_table": {table: sum(item["task_kind"] == "concept_induction"
                                                       and item["records"][0]["table"] == table
                                                       for item in bundles)
                                             for table in definition_by_table},
                "novelty": {"method": "complete_normalized_field_evidence_exact_match",
                            "semantic_similarity_is_identity": False,
                            "concept_exact_duplicate_seeds_skipped": exact_duplicate_seeds,
                            "relation_exact_duplicate_rules_skipped": exact_duplicate_rules,
                            "single_definition_bundles": singleton_bundles},
                "joint_distinct": {"source": "semantic_cards.coverage.by_table.*.joint_distinct",
                                   "method": "bounded_exact_joint_distinct_no_cartesian_enumeration"},
                "checked_rules": checked_rules, "rules_selected": len(rules),
                "checked_rules_not_selected": max(0, checked_rules - len(rules)),
                "bundles_built": len(bundles), "bundles_by_task": {
                    kind: sum(item["task_kind"] == kind for item in bundles)
                    for kind in ("concept_induction", "relation_meaning")},
                "skipped": skipped, "vector": vector_report,
                "max_bundle_bytes": limits["max_bundle_bytes"],
                "partial": bool(skipped or pattern_window["total_patterns"] > concept_bundles
                                or checked_rules > len(rules)
                                or vector_report["status"] in ("over_pattern_cap", "model_unavailable"))}
    return {"bundles": bundles, "coverage": coverage}
