"""Locate reviewed source identities without rescanning a table for every plan.

Locators are hints: actual rows must reproduce the snapshot-local record ID.
Only requested IDs are cached; incomplete evidence falls back once per table.
"""
from collections import Counter, defaultdict


class WitnessLocator:
    def __init__(self, data, plans):
        self.data = data
        self.wanted = defaultdict(set)
        self.evidence_ids = defaultdict(set)
        self.locations = {}
        self.stats = {"evidence_rows_fetched": 0, "evidence_records_verified": 0,
                      "fallback_tables": 0, "fallback_rows_scanned": 0,
                      "witness_rows_fetched": 0, "witness_rows_delivered": 0,
                      "locator_issues": {}}
        self.issues = Counter()
        for plan in plans:
            if plan.witnessed_pairs and plan.witness_snapshot_id != data.snapshot_id:
                raise ValueError("Relation witness belongs to another input snapshot")
            self.wanted[plan.source_table].update(pair.source_record_id for pair in plan.witnessed_pairs)
            self.evidence_ids[plan.source_table].update(plan.evidence_ids)

    def _issue(self, reason):
        self.issues[reason] += 1
        self.stats["locator_issues"] = dict(sorted(self.issues.items()))

    def _fetched(self, phase, count):
        # A batch may be fetched before the executor stops at its budget. Keep
        # database return counts separate from records delivered or examined.
        self.stats[phase + "_rows_fetched"] += count

    def resolve(self, table):
        if table in self.locations:
            return self.locations[table]
        wanted = self.wanted[table]
        candidates = defaultdict(set)
        evidence = getattr(self.data, "evidence", {})
        for evidence_id in sorted(self.evidence_ids[table]):
            ref = evidence.get(evidence_id, {}).get("source_ref", {})
            record_id = ref.get("record_id")
            if record_id not in wanted:
                continue
            if ref.get("table") != table or ref.get("snapshot_id") != self.data.snapshot_id:
                self._issue("evidence_table_or_snapshot_mismatch")
                continue
            number = ref.get("row")
            if type(number) is not int or number < 1:
                self._issue("evidence_row_missing_or_invalid")
                continue
            if number > self.data.tables[table]["rows"]:
                self._issue("evidence_row_out_of_range")
                continue
            candidates[number].add(record_id)
        found = {}
        visited = set()
        for row in self.data.rows_at(
                table, candidates, on_fetch=lambda count: self._fetched("evidence", count)):
            number = row["__r2_row"]
            visited.add(number)
            actual = self.data.record_id(table, row)
            if actual in candidates[number]:
                found[actual] = number
            self.issues["evidence_record_id_mismatch"] += len(candidates[number] - {actual})
        if set(candidates) - visited:
            self._issue("evidence_row_not_found")
        self.stats["evidence_records_verified"] += len(found)
        self.stats["locator_issues"] = dict(sorted((key, count) for key, count in self.issues.items() if count))
        missing = wanted - set(found)
        if missing:
            # One pass serves all plans for this table, including IDs absent from
            # the snapshot. Cache absence too; later plans must not scan again.
            self.stats["fallback_tables"] += 1
            for row in self.data.rows(table):
                self.stats["fallback_rows_scanned"] += 1
                actual = self.data.record_id(table, row)
                if actual in missing:
                    found[actual] = row["__r2_row"]
        self.locations[table] = found
        return found

    def rows(self, table, wanted, coverage):
        """Preserve original source order and pre-budget outside-witness counts."""
        locations = self.resolve(table)
        numbers = {locations[record_id]: record_id for record_id in wanted if record_id in locations}
        yielded = 0
        for row in self.data.rows_at(
                table, numbers, on_fetch=lambda count: self._fetched("witness", count)):
            number = row["__r2_row"]
            # Recheck at consumption, even though evidence resolution checked it.
            if self.data.record_id(table, row) != numbers[number]:
                raise ValueError("Witness locator no longer matches the source snapshot")
            # Dataset import guarantees contiguous 1-based row numbers. A budget
            # break sees exactly the nonwitness prefix the old iterator scanned.
            coverage["outside_witness"] = number - 1 - yielded
            self.stats["witness_rows_delivered"] += 1
            yield row
            yielded += 1
        if yielded != len(numbers):
            raise ValueError("Witness locator row disappeared from the source snapshot")
        coverage["outside_witness"] = self.data.tables[table]["rows"] - yielded
