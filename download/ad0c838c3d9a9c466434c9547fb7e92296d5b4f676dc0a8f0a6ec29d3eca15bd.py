"""Old/new executor equivalence and identity-preserving witness lookup guards."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path
import json
import sqlite3
from copy import deepcopy

import duckdb
import pytest

from ontology_r2.models import BuildPlan, RelationPlan, TablePlan, Condition, WitnessedPair
from ontology_r2.relations import Extractor
from ontology_r2.storage import Dataset, Sink

# Frozen full old executor, including its independent budget/coverage code.
BASELINE = Path(__file__).parent / 'fixtures/witness_executor_baseline.py'
assert hashlib.sha256(BASELINE.read_text(encoding='utf-8').encode('utf-8')).hexdigest() == 'ff25c5f6713e395ef413125f69dac4eb2e7dc484f35f65b343a18eb1ba78e0d8'
spec = importlib.util.spec_from_file_location('ontology_r2._witness_executor_baseline', BASELINE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BaselineExtractor = module.Extractor


class NoLLM:
    async def ask(self, *args, **kwargs):
        raise AssertionError('No LLM allowed during execution comparison')


def make_data():
    data = Dataset.__new__(Dataset)
    data.db = duckdb.connect()
    data.db.execute('SET threads=1')
    data.indexes, data.evidence, data.tables = set(), {}, {}
    data.snapshot_id = 'fixed-test-snapshot'
    source = [
        ('s1','a','CN','yes'), ('s2','a','CN','yes'),
        ('s3','missing','CN','yes'), ('s4','b','CN','yes'),
        ('s5','a','CN','no'), ('s6','a','','yes'),
        ('s7','a','EU','yes'), ('s8','a','CN',''),
        ('s9','a','CN','yes'), ('s10','a','CN','yes'),
    ]
    target = [('t1','a','CN','yes'), ('t2','b','CN','yes'),
              ('t3','b','CN','yes'), ('t4','a','EU','yes')]
    for table, rows in [('source', source), ('target', target)]:
        sql = table + '_data'
        columns = ['id', 'code', 'scope', 'kind']
        data.db.execute(f'CREATE TABLE {sql} (__r2_row BIGINT, id VARCHAR, code VARCHAR, scope VARCHAR, kind VARCHAR)')
        data.db.executemany(f'INSERT INTO {sql} VALUES (?,?,?,?,?)', [(i, *r) for i,r in enumerate(rows,1)])
        data.tables[table] = {'sql_name':sql, 'pk':['id'], 'rows':len(rows),
                              'column_names':columns, 'csv_hash':table+'-hash'}
    return data


def make_plan(data, source_positions=(2,9), count=1, evidence=True):
    src, tgt = list(data.rows('source')), list(data.rows('target'))
    evidence_ids = []
    if evidence:
        for row in src:
            rid = data.record_id('source',row)
            evidence_id = 'e:'+rid
            data.evidence[evidence_id] = {'source_ref': {'table':'source', 'record_id':rid,
                'row': row['__r2_row'], 'snapshot_id':data.snapshot_id}}
            evidence_ids.append(evidence_id)
    relations = [RelationPlan(id=f'p{i}', source_table='source', target_table='target',
        source_column='code', target_column='code', mode='identifier', predicate='points_to',
        selector=Condition(op='eq',field='kind',value='yes'), scope_bindings={'scope':'scope'},
        evidence_scope='sample_semantic_with_full_technical_check', witness_snapshot_id=data.snapshot_id,
        witnessed_pairs=[WitnessedPair(source_record_id=data.record_id('source',src[n-1]),
            target_record_id=data.record_id('target',tgt[0])) for n in source_positions],
        evidence_ids=evidence_ids) for i in range(count)]
    return BuildPlan(tables=[TablePlan(table=t, object_type='source_record_type:'+t,
        label_column='id', evidence_ids=[]) for t in ('source','target')], relations=relations)


def execute(data, plan, extractor=Extractor, cap=1000):
    sink = Sink.__new__(Sink)
    sink.db = sqlite3.connect(':memory:')
    sink.db.execute('CREATE TABLE items (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))')
    try:
        stats = asyncio.run(extractor(data,sink,plan,NoLLM(),{'max_relation_records':cap}).execute())
        items = [(kind,identity,json.loads(body)) for kind,identity,body in
                 sink.db.execute('SELECT kind,id,body FROM items ORDER BY kind,id')]
        return stats, items
    finally:
        sink.db.close()


def original_stats(stats):
    stats = deepcopy(stats)
    stats.pop('witness_lookup',None)
    for item in stats['plans']:
        item.pop('missing_witness_source_records',None)
    return stats


@pytest.mark.parametrize('cap',[0,1,2,3,4,6,1000])
@pytest.mark.parametrize('count',[1,2,3])
@pytest.mark.parametrize('positions',[(1,), (10,), (1,10), (2,9), (2,3,4),
                                      (1,3,5,7,9), (2,4,6,8,10), (1,2,9,10)])
def test_original_output_and_budget_coverage_equivalence(cap,count,positions):
    data = make_data()
    try:
        plan = make_plan(data,positions,count=count)
        old_stats,old_items = execute(data,plan,BaselineExtractor,cap)
        stats,items = execute(data,plan,cap=cap)
        assert items == old_items
        assert original_stats(stats) == old_stats
        assert stats['witness_lookup']['fallback_tables'] == 0
        assert stats['witness_lookup']['evidence_records_verified'] == len(positions)
        assert stats['witness_lookup']['evidence_rows_fetched'] == len(positions)
    finally:
        data.close()


def test_missing_evidence_scans_once_for_all_plans_and_only_caches_wanted_ids():
    from ontology_r2.witness_rows import WitnessLocator
    data = make_data()
    try:
        plan = make_plan(data,(2,),count=3,evidence=False)
        plan.relations[-1].witnessed_pairs = make_plan(data,(9,),evidence=False).relations[0].witnessed_pairs
        baseline,old_items = execute(data,plan,BaselineExtractor)
        stats,items = execute(data,plan)
        assert original_stats(stats) == baseline and items == old_items
        assert stats['witness_lookup']['fallback_tables'] == 1
        assert stats['witness_lookup']['fallback_rows_scanned'] == 10
        locator = WitnessLocator(data,plan.relations)
        assert len(locator.resolve('source')) == 2
        assert len(locator.locations['source']) == 2
        assert locator.resolve('source') is locator.locations['source']
        assert locator.stats['fallback_rows_scanned'] == 10
    finally:
        data.close()


@pytest.mark.parametrize('corruption',['row','table','snapshot','missing_row','unrelated_evidence','conflicting_row'])
def test_untrusted_evidence_locator_cannot_replace_record_identity(corruption):
    data = make_data()
    try:
        plan = make_plan(data,(2,))
        expected = execute(data,plan,BaselineExtractor)
        rid = plan.relations[0].witnessed_pairs[0].source_record_id
        key = 'e:'+rid
        ref = data.evidence[key]['source_ref']
        if corruption == 'row': ref['row'] = 9
        if corruption == 'table': ref['table'] = 'target'
        if corruption == 'snapshot': ref['snapshot_id'] = 'different'
        if corruption == 'missing_row': ref.pop('row')
        if corruption == 'unrelated_evidence': plan.relations[0].evidence_ids.remove(key)
        if corruption == 'conflicting_row':
            data.evidence['conflicting'] = {'source_ref': {**ref,'row':9}}
            plan.relations[0].evidence_ids.append('conflicting')
        expected = execute(data,plan,BaselineExtractor)
        stats,items = execute(data,plan)
        assert items == expected[1]
        assert original_stats(stats) == expected[0]
        assert stats['accepted'] == 1
        assert stats['witness_lookup']['fallback_tables'] == (corruption != 'conflicting_row')
    finally:
        data.close()


def test_unknown_source_is_reported_once_per_plan_without_repeat_scan():
    data = make_data()
    try:
        plan = make_plan(data,(2,),count=2,evidence=False)
        for p in plan.relations:
            p.witnessed_pairs.append(WitnessedPair(source_record_id='unknown',target_record_id='unknown-target'))
        stats,items = execute(data,plan,cap=1)
        assert stats['accepted'] == 1
        assert stats['witness_lookup']['fallback_tables'] == 1
        assert stats['witness_lookup']['fallback_rows_scanned'] == 10
        missing = [body for kind,_,body in items if kind=='unresolved' and body['reason']=='witness_source_not_found']
        assert len(missing) == 2 and {body['source_record'] for body in missing} == {'unknown'}
        assert stats['unprocessed_records'] == 3  # Two missing IDs + one budget-deferred known source.
        assert [p['outside_witness'] for p in stats['plans']] == [9,1]
        assert [p['missing_witness_source_records'] for p in stats['plans']] == [1,1]
    finally:
        data.close()


def test_all_missing_ids_do_not_fake_execution_or_success():
    data=make_data()
    try:
        plan=make_plan(data,(2,),evidence=False)
        plan.relations[0].witnessed_pairs[0].source_record_id='missing'
        stats,items=execute(data,plan)
        assert stats['accepted']==stats['records_examined']==0
        assert stats['unprocessed_records']==stats['unresolved']==1
        assert stats['plans'][0]['outside_witness']==10
        assert all(kind != 'assertions' for kind,_,_ in items)
    finally:
        data.close()


def test_wrong_plan_snapshot_rejected_before_any_source_read():
    data=make_data()
    try:
        plan=make_plan(data)
        plan.relations[0].witness_snapshot_id='other'
        data.rows=lambda *args,**kwargs: pytest.fail('Must not read stale witnesses')
        data.rows_at=lambda *args,**kwargs: pytest.fail('Must not read stale witnesses')
        with pytest.raises(ValueError,match='another input snapshot'):
            execute(data,plan)
    finally:
        data.close()


@pytest.mark.parametrize('position',[3,4,5,6,7,8])
def test_missing_ambiguous_selector_scope_and_target_mismatch_are_preserved(position):
    data=make_data()
    try:
        plan=make_plan(data,(position,))
        old_stats,old_items=execute(data,plan,BaselineExtractor)
        stats,items=execute(data,plan)
        assert items==old_items and original_stats(stats)==old_stats
        assert stats['accepted']==0
    finally:
        data.close()


def test_unwitnessed_plan_keeps_original_full_execution():
    data=make_data()
    try:
        plan=make_plan(data)
        p=plan.relations[0]; p.witnessed_pairs=[]; p.evidence_scope=None; p.witness_snapshot_id=None
        old_stats,old_items=execute(data,plan,BaselineExtractor)
        stats,items=execute(data,plan)
        assert items==old_items and original_stats(stats)==old_stats
        assert stats['witness_lookup']['fallback_rows_scanned']==0
    finally:
        data.close()


def test_rows_at_deduplicates_sorts_chunks_and_indexes_once():
    data=make_data()
    try:
        assert [row['__r2_row'] for row in data.rows_at('source',[9,2,2,10])]==[2,9,10]
        count=len(data.indexes)
        assert list(data.rows_at('source',[]))==[]
        with pytest.raises(ValueError,match='row count'):
            list(data.rows_at('source',[1,2000]))
        assert len(data.indexes)==count==1
        for values in ([True], [0], [-1], ['2']):
            with pytest.raises(ValueError,match='positive integers'):
                list(data.rows_at('source',values))
    finally:
        data.close()


def test_valid_evidence_does_not_enter_source_scan():
    data=make_data()
    try:
        plan=make_plan(data,(2,9))
        data.rows=lambda *args,**kwargs: pytest.fail('Evidence-located witnesses must not scan the source')
        stats,items=execute(data,plan)
        assert stats['accepted']==2
        assert stats['witness_lookup']['witness_rows_delivered']==2
    finally:
        data.close()


@pytest.mark.parametrize('pk_mode',['missing','duplicate'])
def test_identity_locating_does_not_assume_unique_primary_keys(pk_mode):
    data=make_data()
    try:
        if pk_mode=='missing': data.tables['source']['pk']=[]
        else: data.db.execute("UPDATE source_data SET id='repeated'")
        plan=make_plan(data,(2,9))
        old_stats,old_items=execute(data,plan,BaselineExtractor)
        stats,items=execute(data,plan)
        assert old_items==items and original_stats(stats)==old_stats
        assert stats['accepted']==2
    finally:
        data.close()


def test_duplicate_witness_pairs_do_not_duplicate_processing():
    data=make_data()
    try:
        plan=make_plan(data,(2,))
        plan.relations[0].witnessed_pairs *= 3
        old_stats,old_items=execute(data,plan,BaselineExtractor)
        stats,items=execute(data,plan)
        assert old_items==items and original_stats(stats)==old_stats
        assert stats['records_examined']==1
    finally:
        data.close()


def test_rows_at_more_than_one_batch_keeps_source_order():
    data=make_data()
    try:
        data.db.execute("INSERT INTO source_data SELECT i, 's'||i, 'a', 'CN', 'yes' FROM range(11,2025) t(i)")
        data.tables['source']['rows']=2024
        numbers=list(range(2024,0,-1))
        assert [r['__r2_row'] for r in data.rows_at('source',numbers)]==sorted(numbers)
        assert len(data.indexes)==1
    finally:
        data.close()


@pytest.mark.parametrize('mutation',['changed','deleted'])
def test_verified_locator_is_rechecked_before_consumption(mutation):
    from ontology_r2.witness_rows import WitnessLocator
    data=make_data()
    try:
        plan=make_plan(data,(2,))
        locator=WitnessLocator(data,plan.relations)
        wanted={p.source_record_id for p in plan.relations[0].witnessed_pairs}
        locator.resolve('source')
        if mutation=='changed': data.db.execute("UPDATE source_data SET id='forged' WHERE __r2_row=2")
        else: data.db.execute('DELETE FROM source_data WHERE __r2_row=2')
        with pytest.raises(ValueError,match='Witness locator'):
            list(locator.rows('source',wanted,{}))
    finally:
        data.close()


@pytest.mark.parametrize('operator',['nfkc_whitespace_casefold','target_alias_items'])
@pytest.mark.parametrize('cap',[0,2,1000])
def test_transformed_targets_and_budget_remain_equivalent(operator,cap):
    data=make_data()
    try:
        data.db.execute("UPDATE source_data SET code=' '||upper(code)||' '")
        if operator=='target_alias_items':
            data.db.execute("UPDATE target_data SET code='other;'||code")
        plan=make_plan(data,(2,4,7,9))
        plan.relations[0].transform={'operator':operator}
        old_stats,old_items=execute(data,plan,BaselineExtractor,cap)
        stats,items=execute(data,plan,cap=cap)
        assert items==old_items and original_stats(stats)==old_stats
    finally:
        data.close()


@pytest.mark.parametrize('claimed_row',[11, 2**63, 2**100, 10**100])
def test_out_of_range_evidence_never_reaches_duckdb_and_falls_back_once(claimed_row):
    data=make_data()
    try:
        plan=make_plan(data,(2,),count=2)
        record_id=plan.relations[0].witnessed_pairs[0].source_record_id
        data.evidence['e:'+record_id]['source_ref']['row']=claimed_row
        rows_at=data.rows_at
        seen=[]
        def checked_rows_at(table,numbers,**kwargs):
            numbers=list(numbers)
            assert all(1 <= number <= data.tables[table]['rows'] for number in numbers)
            seen.extend(numbers)
            yield from rows_at(table,numbers,**kwargs)
        data.rows_at=checked_rows_at
        old_stats,old_items=execute(data,plan,BaselineExtractor)
        stats,items=execute(data,plan)
        assert items==old_items and original_stats(stats)==old_stats
        lookup=stats['witness_lookup']
        assert lookup['locator_issues']['evidence_row_out_of_range']==1
        assert lookup['fallback_tables']==1 and lookup['fallback_rows_scanned']==10
        assert lookup['evidence_rows_fetched']==0
        assert seen==[2,2]
    finally:
        data.close()


@pytest.mark.parametrize('count',[10,1001])
def test_zero_budget_reports_prefetched_and_delivered_rows_separately(count):
    data=make_data()
    try:
        if count>10:
            data.db.execute("INSERT INTO source_data SELECT i, 's'||i, 'a', 'CN', 'yes' FROM range(11,?) t(i)",[count+1])
            data.tables['source']['rows']=count
        plan=make_plan(data,tuple(range(1,count+1)))
        old_stats,old_items=execute(data,plan,BaselineExtractor,cap=0)
        stats,items=execute(data,plan,cap=0)
        assert items==old_items and original_stats(stats)==old_stats
        lookup=stats['witness_lookup']
        assert lookup['evidence_rows_fetched']==count
        assert lookup['witness_rows_fetched']==min(count,1000)
        assert lookup['witness_rows_delivered']==1
        assert stats['records_examined']==stats['accepted']==0
        assert stats['unprocessed_records']==count
    finally:
        data.close()


def test_rows_at_counts_fetched_batch_even_when_consumer_stops_early():
    data=make_data()
    try:
        batches=[]
        rows=data.rows_at('source',range(1,11),on_fetch=batches.append)
        assert next(rows)['__r2_row']==1
        rows.close()
        assert batches==[10]
        for number in [2**63,10**100]:
            with pytest.raises(ValueError,match='row count'):
                list(data.rows_at('source',[number]))
    finally:
        data.close()
