"""Check synthetic source consistency without asserting model-produced ontology."""
import csv
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import generate_fruit_semantic_data as fixture


def test_coherent_source_has_real_formula_endpoints_and_instance_bindings(tmp_path):
    root = tmp_path / 'coherent'
    result = fixture.generate(root, scale=.001)
    assert result['table_count'] == 23
    assert result['distinct_quantity_symbols'] == len(fixture.QUANTITIES)
    assert result['formulas_with_resolved_symbols'] > 0
    with (root / 'data/fruit_measure_def.csv').open(newline='') as f:
        quantities = list(csv.DictReader(f))
    rate = next(r for r in quantities if r['measure_name'] == '合格率')
    assert 'tested_quantity' in rate['source_field']
    assert rate['standard_name'] == '合格率'
    assert '苹果' not in rate['measure_description']
    with (root / 'data/fruit_metric_detail.csv').open(newline='') as f:
        metrics = list(csv.DictReader(f))
    assert any(r['metric_name'] == '水果合格率' for r in metrics)
    assert any('配置对象' in r['metric_definition'] for r in metrics)
    with (root / 'data/fruit_dashboard_card.csv').open(newline='') as f:
        assert all('具体看板实例' in r['card_description'] for r in csv.DictReader(f))


def test_unbound_formula_is_detected_not_repaired_by_common_sense(tmp_path):
    root = tmp_path / 'coherent'
    fixture.generate(root, scale=.001)
    path = root / 'data/fruit_metric_detail.csv'
    with path.open(newline='') as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    rows[0]['calculation_formula'] = 'x = absent_quantity + sales_volume'
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match='Unbound formula symbols'):
        fixture.validate_semantics(root)


def test_v2_remains_the_legacy_input_not_silently_repaired():
    counts = fixture.base.scaled_counts(1.0)
    table = fixture.base.TABLE_BY_NAME['fruit_measure_def']
    legacy = fixture.base.make_row(table, 30, counts)
    assert legacy['standard_name'] == '苹果合格率'
    assert legacy['source_field'] == 'avg_price_cny_per_kg'
    assert sum(counts.values()) == 1_102_238
