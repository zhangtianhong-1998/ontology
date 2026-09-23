-- Synthetic design probes. No user data, LLM call or semantic acceptance.
CREATE TEMP TABLE unrelated_left AS SELECT i::VARCHAR AS code FROM range(1, 4) t(i);
CREATE TEMP TABLE unrelated_right AS SELECT i::VARCHAR AS code FROM range(1, 4) t(i);

CREATE TEMP TABLE partial_source AS
SELECT i AS row_id,
       CASE WHEN i <= 20 THEN 'linked' ELSE 'local' END AS kind,
       'K' || i::VARCHAR AS ref
FROM range(1, 1001) t(i);
CREATE TEMP TABLE partial_target AS
SELECT 'K' || i::VARCHAR AS code FROM range(1, 21) t(i);

CREATE TEMP TABLE scoped_source(ns VARCHAR, ref VARCHAR);
INSERT INTO scoped_source VALUES ('north', 'A'), (NULL, 'A');
CREATE TEMP TABLE scoped_target(ns VARCHAR, code VARCHAR);
INSERT INTO scoped_target VALUES ('north', 'A'), ('south', 'A');

CREATE TEMP TABLE duplicate_target(code VARCHAR);
INSERT INTO duplicate_target VALUES ('A'), ('A');

CREATE TEMP TABLE skewed_source AS
SELECT CASE WHEN i < 1000 THEN 'A' ELSE 'Z' END AS ref FROM range(1, 1001) t(i);
CREATE TEMP TABLE skewed_target(code VARCHAR);
INSERT INTO skewed_target VALUES ('A');

CREATE TEMP TABLE raw_codes(code VARCHAR);
INSERT INTO raw_codes VALUES ('001'), ('1');
CREATE TEMP TABLE tuple_keys(a VARCHAR, b VARCHAR);
INSERT INTO tuple_keys VALUES ('a|b', 'c'), ('a', 'b|c');

CREATE TEMP TABLE nested_source AS
SELECT i AS row_id,
       CASE WHEN i <= 980 THEN '{"note":"independent"}'
            ELSE '{"binding":{"code":"K' || (i - 980)::VARCHAR || '"}}' END AS payload
FROM range(1, 1001) t(i);

CREATE TEMP TABLE design_measurements AS
SELECT 'unrelated_numeric_overlap' AS name,
       (SELECT count(*)::DOUBLE FROM unrelated_left l SEMI JOIN unrelated_right r USING(code)) / 3 AS value
UNION ALL SELECT 'whole_column_containment',
       (SELECT count(*)::DOUBLE FROM partial_source s SEMI JOIN partial_target t ON s.ref=t.code) / 1000
UNION ALL SELECT 'conditional_containment',
       (SELECT count(*)::DOUBLE FROM partial_source s SEMI JOIN partial_target t ON s.ref=t.code WHERE s.kind='linked') /
       (SELECT count(*) FROM partial_source WHERE kind='linked')
UNION ALL SELECT 'unscoped_target_matches',
       (SELECT count(*) FROM scoped_source s JOIN scoped_target t ON s.ref=t.code WHERE s.ns='north')
UNION ALL SELECT 'scoped_target_matches',
       (SELECT count(*) FROM scoped_source s JOIN scoped_target t ON s.ref=t.code AND s.ns=t.ns WHERE s.ns='north')
UNION ALL SELECT 'unknown_scope_records',
       (SELECT count(*) FROM scoped_source WHERE ns IS NULL)
UNION ALL SELECT 'duplicate_target_multiplicity',
       (SELECT count(*) FROM duplicate_target WHERE code='A')
UNION ALL SELECT 'skewed_row_hit_ratio',
       (SELECT count(*)::DOUBLE FROM skewed_source s SEMI JOIN skewed_target t ON s.ref=t.code) / 1000
UNION ALL SELECT 'skewed_distinct_containment',
       (SELECT count(DISTINCT ref)::DOUBLE FROM skewed_source s SEMI JOIN skewed_target t ON s.ref=t.code) /
       (SELECT count(DISTINCT ref) FROM skewed_source)
UNION ALL SELECT 'raw_code_matches', (SELECT count(*) FROM raw_codes WHERE code='001')
UNION ALL SELECT 'numeric_normalized_matches', (SELECT count(*) FROM raw_codes WHERE try_cast(code AS BIGINT)=1)
UNION ALL SELECT 'unsafe_concatenated_keys', (SELECT count(DISTINCT a || '|' || b) FROM tuple_keys)
UNION ALL SELECT 'safe_tuple_keys', (SELECT count(DISTINCT (a,b)) FROM tuple_keys)
UNION ALL SELECT 'json_path_present',
       (SELECT count(*) FROM nested_source WHERE json_extract_string(payload, '$.binding.code') IS NOT NULL)
UNION ALL SELECT 'json_path_matched',
       (SELECT count(*) FROM nested_source s SEMI JOIN partial_target t ON json_extract_string(s.payload, '$.binding.code')=t.code)
UNION ALL SELECT 'empty_denominator_ratio', 0.0 / NULLIF(0,0);

SELECT name, value FROM design_measurements ORDER BY name;
