"""Bounded cross-table evidence packets from verified rules and definition cards."""

from __future__ import annotations

import json
from collections import defaultdict

from .concept_candidates import _field_roles
from .embedding import top_cosine
from .row_bundles import joint_examples
from .semantic_cards import _context_table, _root_hint
from .storage import digest


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _limits(options):
    defaults = {"max_concept_bundles": 12, "max_relation_bundles": 12,
                "max_seeds_per_table": 8, "lexical_top_k": 8,
                "max_candidates_per_bundle": 3, "max_bundle_bytes": 16000,
                "max_vector_cards": 20000, "vector_top_k": 8,
                "max_joint_pairs_per_rule": 2}
    limits = {key: options.get(key, value) for key, value in defaults.items()}
    if any(type(value) is not int or value < 0 for value in limits.values()):
        raise ValueError("Instance bundle limits must be nonnegative integers")
    if not 0 <= limits["max_joint_pairs_per_rule"] <= 2:
        raise ValueError("max_joint_pairs_per_rule must be 0..2")
    if limits["max_bundle_bytes"] < 1000 or limits["max_candidates_per_bundle"] < 1:
        raise ValueError("Bundle bytes and candidate count are too small")
    return limits


def _card_text(card):
    parts = []
    for role in ("name", "alias", "description", "formula", "unit", "scope", "unknown"):
        parts.extend(str(item["value"]) for item in card.get("fields", {}).get(role, ()))
    return "\n".join(parts)[:2400]


def _compatible(seed, candidate):
    if seed["card_id"] == candidate["card_id"]:
        return False
    a, b = seed.get("root_hint"), candidate.get("root_hint")
    return not (a and b and a != "GeneralObject" and b != "GeneralObject" and a != b)


def _vector_pool(index, limits, embedding, enabled):
    report = {"enabled": bool(enabled), "status": "disabled", "cards_total": 0,
              "cards_encoded": 0, "model_sha256": None}
    if not enabled:
        return None, report
    if embedding is None:
        report["status"] = "model_unavailable"
        return None, report
    selection = index.all_cards(limits["max_vector_cards"], kind="definition")
    report["cards_total"] = selection["total"]
    report["model_sha256"] = embedding.model_sha256
    if not selection["complete"]:
        report["status"] = "over_card_cap"
        return None, report
    cards = selection["cards"]
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


def _select_seeds(pool, limit):
    """Spread scarce calls across business roots and source tables."""
    order = ("Metric", "Measure", "Dimension", "Term", "GeneralObject")
    groups = {root: [card for card in pool if card.get("root_hint") == root]
              for root in order}
    selected, table_uses = [], defaultdict(int)
    while len(selected) < limit and any(groups.values()):
        for root in order:
            cards = groups[root]
            if not cards or len(selected) >= limit:
                continue
            def rank(card):
                fields = card.get("fields", {})
                quality = (2 * bool(fields.get("description"))
                           + 2 * bool(fields.get("formula"))
                           + bool(fields.get("unit")) + bool(fields.get("alias")))
                return (table_uses[card["table"]], -quality, card["table"], card["card_id"])
            card = min(cards, key=rank)
            cards.remove(card)
            selected.append(card)
            table_uses[card["table"]] += 1
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
              "seed_ids": [seed["card_id"]], "records": records,
              "retrieval": retrieval,
              "examples": {"candidate_only": [card["card_id"] for card in records[1:]],
                           "validated_edges": []},
              "limits": {"semantic_similarity_is_identity": False}}
    while _size(bundle) > byte_limit and len(records) > 1:
        records.pop()
        retrieval.pop()
        bundle["examples"]["candidate_only"] = [card["card_id"] for card in records[1:]]
    if _size(bundle) > byte_limit:
        return None, "seed_over_budget"
    if len(records) < 2:
        return None, "no_candidate_within_budget"
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
        for rule in sorted(rules, key=lambda item: item["rule_id"]):
            if (rule["status"] == "checked_technical"
                    and bool(rule.get("numeric_overlap_only", False)) == numeric_only):
                groups[rule["source"]["table"]].append(rule)
        while groups and len(chosen) < limit:
            for table in sorted(groups):
                if groups[table]:
                    chosen.append(groups[table].pop(0))
                    if len(chosen) >= limit:
                        break
            groups = {table: remaining for table, remaining in groups.items() if remaining}
        if len(chosen) >= limit:
            break
    return chosen


def build_instance_bundles(data, index, association, options=None, *, embedding=None):
    """Build auditable packets without per-row LLM calls or similarity-as-fact."""
    options = options or {}
    limits = _limits(options)
    vector_pool, vector_report = _vector_pool(
        index, limits, embedding, options.get("vector_enabled", False))
    bundles, skipped = [], []
    if limits["max_concept_bundles"] > 1000:
        raise ValueError("max_concept_bundles exceeds the bounded seed selector")
    seed_pool = (index.seeds(min(1000, max(64, limits["max_concept_bundles"] * 8)),
                             per_table=max(1, limits["max_seeds_per_table"]),
                             kind="definition") if limits["max_concept_bundles"] else [])
    seeds = _select_seeds(seed_pool, limits["max_concept_bundles"])
    for seed in seeds:
        query = seed.get("name") or _card_text(seed)[:160]
        lexical = index.search(query, limit=max(1, limits["lexical_top_k"]),
                               kind="definition", exclude_card_id=seed["card_id"])
        vectors = _vector_hits(seed, vector_pool, embedding,
                               limits["vector_top_k"]) if vector_pool else []
        selected = _select_candidates(seed, lexical, vectors,
                                      limits["max_candidates_per_bundle"])
        bundle, reason = _concept_bundle(data, seed, selected, limits["max_bundle_bytes"])
        if bundle:
            bundles.append(bundle)
        else:
            skipped.append({"seed_id": seed["card_id"], "reason": reason})
    rules = _round_robin_rules(association.get("rules", []), limits["max_relation_bundles"])
    for rule in rules:
        bundle, reason = _relation_bundle(data, rule, limits)
        if bundle:
            bundles.append(bundle)
        else:
            skipped.append({"seed_id": rule["rule_id"], "reason": reason})
    total_definition = index.db.execute("SELECT count(*) FROM cards WHERE kind='definition'").fetchone()[0]
    checked_rules = sum(item["status"] == "checked_technical"
                        for item in association.get("rules", []))
    coverage = {"snapshot_id": data.snapshot_id, "definition_cards_indexed": total_definition,
                "definition_seed_pool_examined": len(seed_pool),
                "definition_seeds_selected": len(seeds),
                "seed_roots": {root: sum(item.get("root_hint") == root for item in seeds)
                               for root in ("GeneralObject", "Measure", "Metric", "Dimension", "Term")},
                "definition_cards_not_seeded": max(0, total_definition - len(seeds)),
                "checked_rules": checked_rules, "rules_selected": len(rules),
                "checked_rules_not_selected": max(0, checked_rules - len(rules)),
                "bundles_built": len(bundles), "bundles_by_task": {
                    kind: sum(item["task_kind"] == kind for item in bundles)
                    for kind in ("concept_induction", "relation_meaning")},
                "skipped": skipped, "vector": vector_report,
                "max_bundle_bytes": limits["max_bundle_bytes"],
                "partial": bool(skipped or total_definition > len(seeds)
                                or checked_rules > len(rules)
                                or vector_report["status"] in ("over_card_cap", "model_unavailable"))}
    return {"bundles": bundles, "coverage": coverage}
