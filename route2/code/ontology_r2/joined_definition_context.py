"""Bounded actual-row context from checked technical links, without identity claims.

A rule is materialized once for the complete seed window. Queries preserve its
selector, scope and transform, and only uniquely matched forward links enter
an adjacency table. Reverse expansion samples those same links; it never forms
an independent many-to-many join.
"""
from collections import Counter, defaultdict

from .concept_candidates import _field_roles
from .discovery import association_match_sql
from .storage import digest, qi


class JoinedDefinitionContext:
    def __init__(self, data, index, association, seeds, *, max_records_per_seed=3,
                 max_rules=64, max_value_chars=2048):
        self.data, self.index = data, index
        self.limit, self.max_chars = max_records_per_seed, max_value_chars
        if any(type(value) is not int or value < 0 for value in
               (max_records_per_seed, max_rules, max_value_chars)) or max_value_chars == 0:
            raise ValueError('Joined context limits must be nonnegative; value characters must be positive')
        self.tables, self.skipped, self.seed_coverage = [], [], []
        self.stats = Counter()
        by_table = defaultdict(set)
        for seed in seeds:
            by_table[seed['table']].add(seed['row_number'])
        rules = [rule for rule in association.get('rules', [])
                 if rule.get('status') == 'checked_technical' and
                 (rule['source']['table'] in by_table or rule['target']['table'] in by_table)]
        rules.sort(key=lambda rule: (rule.get('numeric_overlap_only', False), rule['rule_id']))
        self.stats['relevant_checked_rules'] = len(rules)
        self.stats['rules_not_materialized_due_to_limit'] = max(0, len(rules)-max_rules)
        if not self.limit:
            self.stats['rules_not_materialized_due_to_limit'] = len(rules)
            return
        for rule in rules[:max_rules]:
            counts = rule.get('verification', {}).get('checks', {})
            if (rule.get('snapshot_id') != data.snapshot_id
                    or rule.get('verification', {}).get('scan_scope') != 'full_input'
                    or counts.get('eligible_references', 0) <= 0
                    or counts.get('unique_matches') != counts.get('eligible_references')
                    or any(counts.get(key, 0) for key in ('ambiguous_matches', 'missing_scope', 'missing_in_input'))):
                self.skipped.append({'rule_id': rule['rule_id'], 'reason': 'invalid_full_input_check'})
                continue
            table_name = '__r2_definition_context_' + digest([
                data.snapshot_id, rule['rule_id'], sorted((name, sorted(rows)) for name, rows in by_table.items())])[:20]
            try:
                self._materialize(rule, by_table, table_name)
            except Exception as exc:
                self.skipped.append({'rule_id': rule['rule_id'], 'reason': 'context_query_error',
                                     'error_type': type(exc).__name__})
                continue
            self.tables.append((table_name, rule))
        self.stats['rules_materialized'] = len(self.tables)

    def _materialize(self, rule, by_table, table_name):
        source, target = rule['source'], rule['target']
        scope = rule.get('scope_bindings', {})
        if len(set(scope.values())) != len(scope):
            raise ValueError('Scope bindings collide')
        query, params = association_match_sql(self.data, rule,
            selector=rule.get('selector'), scope_bindings={v: k for k, v in scope.items()},
            transform=rule.get('transform', {}).get('operator', 'identity'))
        branches = []
        for direction, local, remote in [('forward', source, target), ('reverse', target, source)]:
            positions = sorted(by_table.get(local['table'], []))
            if not positions:
                continue
            seed_col, neighbor_col = ('source_row_number', 'target_row_number') if direction == 'forward' else (
                'target_row_number', 'source_row_number')
            marks = ','.join('?' for _ in positions)
            branches.append(f"SELECT '{direction}' AS direction, {seed_col} AS seed_row, "
                            f"{neighbor_col} AS neighbor_row FROM matched WHERE {seed_col} IN ({marks})")
            params.extend(positions)
        self.data.db.execute(f"""CREATE OR REPLACE TEMP TABLE {qi(table_name)} AS
            WITH matched AS MATERIALIZED ({query}), adjacent AS ({' UNION ALL '.join(branches)}),
            ranked AS (SELECT *, count(*) OVER (PARTITION BY direction, seed_row) AS neighbor_count,
                       row_number() OVER (PARTITION BY direction, seed_row ORDER BY neighbor_row) AS rank
                       FROM adjacent)
            SELECT * FROM ranked WHERE rank <= ?""", [*params, self.limit])
        self.data.db.execute(f"CREATE INDEX {qi(table_name+'_lookup')} ON {qi(table_name)} (direction, seed_row)")

    def _record(self, table, row_number, rule, direction):
        info = self.data.tables[table]
        roles = _field_roles(info)
        role_by_column = {field: role for role, fields in roles.items() for field in fields}
        source_side = direction == 'reverse'
        endpoint = rule['source' if source_side else 'target']
        keys = {endpoint['field']}
        if source_side:
            keys.update(rule.get('selector', {}))
            keys.update(rule.get('scope_bindings', {}))
        else:
            keys.update(rule.get('scope_bindings', {}).values())
        columns = list(dict.fromkeys([*info.get('pk', []), *role_by_column, *sorted(keys)]))
        selected = ','.join(qi(column) for column in columns)
        values = self.data.db.execute(f'SELECT __r2_row, {selected} FROM {qi(info["sql_name"])} '
                                      'WHERE __r2_row=?', [row_number]).fetchone()
        if values is None:
            raise ValueError('Matched source row no longer exists')
        row = dict(zip(['__r2_row', *columns], values))
        fields, omitted = defaultdict(list), []
        for column in columns:
            if column not in role_by_column and column not in keys:
                continue
            value = row.get(column)
            if value is None or not str(value).strip():
                continue
            raw = str(value)
            if len(raw) > self.max_chars:
                omitted.append({'column': column, 'reason': 'whole_field_exceeds_context_limit',
                                'characters': len(raw)})
                continue
            fields[role_by_column.get(column, 'reference')].append({
                'column': column, 'value': raw, 'truncated': False,
                'schema_evidence_id': f'schema:{table}:{column}'})
        rid = self.data.record_id(table, row)
        source_card = self.index.db.execute('SELECT card_id FROM card_sources WHERE record_id=?', (rid,)).fetchone()
        return {'card_id': source_card['card_id'] if source_card else 'context:' + digest([self.data.snapshot_id, rid])[:24],
                'record_id': rid, 'table': table, 'row_number': row_number,
                'kind': 'related_context', 'context_role': 'related_context',
                'eligible_for_exact_alignment': False, 'fields': dict(fields),
                'name': next((v['value'] for v in fields.get('name', [])), ''),
                'scope': {v['column']: v['value'] for v in fields.get('scope', [])},
                'unit': next((v['value'] for v in fields.get('unit', [])), ''),
                'omitted_fields': omitted,
                'connection': {'rule_id': rule['rule_id'], 'direction': direction,
                               'source': rule['source'], 'target': rule['target'],
                               'selector': rule.get('selector', {}), 'scope_bindings': rule.get('scope_bindings', {}),
                               'transform': rule.get('transform', {'operator': 'identity'}),
                               'semantic_relation': 'unresolved', 'identity_claim': False}}

    def for_seed(self, seed):
        leads, available = [], 0
        for table_name, rule in self.tables:
            for direction, endpoint, remote in [('forward', rule['source'], rule['target']),
                                                 ('reverse', rule['target'], rule['source'])]:
                if endpoint['table'] != seed['table']:
                    continue
                rows = self.data.db.execute(f'SELECT neighbor_row, neighbor_count FROM {qi(table_name)} '
                                             'WHERE direction=? AND seed_row=? ORDER BY neighbor_row',
                                             [direction, seed['row_number']]).fetchall()
                if rows:
                    available += rows[0][1]
                leads.extend((remote['table'], row, rule, direction) for row, _ in rows)
        records, seen, read_errors = [], set(), []
        for table, row, rule, direction in leads:
            key = (table, row)
            if key in seen or (table == seed['table'] and row == seed['row_number']):
                continue
            seen.add(key)
            if len(records) >= self.limit:
                continue
            try:
                records.append(self._record(table, row, rule, direction))
            except Exception as exc:
                read_errors.append({'table': table, 'row': row, 'error_type': type(exc).__name__})
        omitted = max(0, available-len(records))
        coverage = {'seed_record_id': seed['record_id'], 'neighbor_edges_available': available,
                    'records_expanded': len(records), 'neighbor_edges_not_expanded': omitted,
                    'fields_not_expanded': sum(len(record['omitted_fields']) for record in records),
                    'read_errors': read_errors, 'max_records': self.limit,
                    'partial': bool(omitted or read_errors or any(record['omitted_fields'] for record in records))}
        self.seed_coverage.append(coverage)
        return records, coverage

    def coverage(self):
        return {**self.stats, 'skipped_rules': self.skipped, 'seeds': self.seed_coverage,
                'records_expanded': sum(item['records_expanded'] for item in self.seed_coverage),
                'neighbor_edges_not_expanded': sum(item['neighbor_edges_not_expanded'] for item in self.seed_coverage),
                'partial': bool(self.skipped or self.stats['rules_not_materialized_due_to_limit']
                                or any(item['partial'] for item in self.seed_coverage)),
                'identity_claim': False, 'scan_strategy': 'once_per_checked_rule_for_current_seed_window'}

    def close(self):
        for name, _ in self.tables:
            self.data.db.execute(f'DROP TABLE IF EXISTS {qi(name)}')
