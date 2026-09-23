"""Lossless source import and bounded, disk-backed semantic output."""
import csv
import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path

import duckdb
import yaml


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    out = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=deep)
        if key in out:
            raise ValueError(f"Duplicate YAML key: {key}")
        out[key] = loader.construct_object(value, deep=deep)
    return out


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path):
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueLoader)


def write_yaml(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def qi(name):
    return '"' + name.replace('"', '""') + '"'


class Dataset:
    def __init__(self, root, work, memory="1GB", profiling_config=None, progress=None):
        self.root, self.tables, self.evidence, self.files = Path(root), {}, {}, {}
        self.indexes = set()
        self.db = duckdb.connect(str(Path(work) / "sources.duckdb"))
        self.db.execute("SET memory_limit = ?", [memory])
        self.db.execute("SET preserve_insertion_order = true")
        files = sorted((self.root / "schema/tables").glob("*.yaml"))
        import_task = progress.task("导入 CSV", len(files)) if progress else nullcontext(None)
        with import_task as stage:
            for index, f in enumerate(files):
                table = read_yaml(f)
                name = f"{table['schema']}.{table['table_name']}"
                if stage:
                    stage.note(f"{name} 导入中")
                if name in self.tables:
                    raise ValueError(f"Duplicate table: {name}")
                columns = [c["column_name"] for c in table["columns"]]
                if len(set(columns)) != len(columns) or "__r2_row" in columns:
                    raise ValueError(f"Duplicate/reserved column in {name}")
                table.update(name=name, sql_name=f"src_{index}", column_names=columns, pk=[])
                self.tables[name] = table
                self.files[str(f.relative_to(self.root))] = file_hash(f)
                for folder in ("constraints", "foreign_keys"):
                    other = self.root / "schema" / folder / f.name
                    info = read_yaml(other)
                    if (info["schema"], info["table_name"]) != (table["schema"], table["table_name"]):
                        raise ValueError(f"Mismatched metadata: {other}")
                    table[folder] = info[folder]
                    self.files[str(other.relative_to(self.root))] = file_hash(other)
                for c in table["constraints"]:
                    match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", c.get("definition", ""), re.I)
                    if match:
                        table["pk"] = [v.strip().strip('"') for v in match[1].split(",")]
                        if not set(table["pk"]) <= set(columns):
                            raise ValueError(f"Invalid primary key in {name}")
                for col in [None, *table["columns"]]:
                    key = f"schema:{name}" + (f":{col['column_name']}" if col else "")
                    text = col.get("column_comment") if col else table.get("table_comment")
                    self.evidence[key] = {"id": key, "origin": "declared_metadata", "raw_fragment": text or "", "source_ref": {"table": name, "column": col["column_name"] if col else None, "file": str(f.relative_to(self.root))}}
                path = self.root / "data" / (table["table_name"] + ".csv")
                # Repeated bare table names require an explicit schema subdirectory.
                scoped = self.root / "data" / table["schema"] / (table["table_name"] + ".csv")
                if scoped.exists():
                    path = scoped
                with path.open(newline="", encoding="utf-8-sig") as stream:
                    header = next(csv.reader(stream))
                if set(header) != set(columns) or len(header) != len(columns):
                    raise ValueError(f"CSV columns differ from YAML: {name}")
                self.files[str(path.relative_to(self.root))] = file_hash(path)
                table["csv_path"], table["csv_hash"] = str(path), file_hash(path)
                self.db.execute(f"CREATE TABLE {qi(table['sql_name'])} AS SELECT row_number() OVER () AS __r2_row, * FROM read_csv(?, header=true, all_varchar=true, nullstr='', allow_quoted_nulls=false)", [str(path)])
                table["rows"] = self.db.execute(f"SELECT count(*) FROM {qi(table['sql_name'])}").fetchone()[0]
                if stage:
                    stage.advance(detail=f"{name} {table['rows']} 行")
        if not self.tables:
            raise ValueError("No schema/tables/*.yaml inputs")
        bare_names = [t["table_name"] for t in self.tables.values()]
        for t in self.tables.values():
            if bare_names.count(t["table_name"]) > 1 and Path(t["csv_path"]).parent.name != t["schema"]:
                raise ValueError("Duplicate bare table names require data/<schema>/<table>.csv")
        self.snapshot_id = digest(self.files)
        from .profiling import MAX_COLUMNS_PER_GROUP, profile_fields
        requested = int((profiling_config or {}).get("sql_columns_per_group", MAX_COLUMNS_PER_GROUP))
        group_size = min(requested, MAX_COLUMNS_PER_GROUP)
        if group_size <= 0:
            raise ValueError("sql_columns_per_group must be positive")
        groups = sum((len(table["column_names"]) + group_size - 1) // group_size
                     for table in self.tables.values())
        profile_task = progress.task("字段统计", groups) if progress else nullcontext(None)
        with profile_task as stage:
            on_group = (lambda table: stage.advance(detail=table)) if stage else None
            on_group_start = (lambda table, start, end: stage.note(
                f"{table} 第 {start + 1}-{end} 列统计中")) if stage else None
            for name, profiles in profile_fields(self, profiling_config,
                                                 on_group=on_group,
                                                 on_group_start=on_group_start).items():
                self.tables[name]["profiles"] = profiles

    def context(self, tables=None):
        selected = set(self.tables) if tables is None else set(tables)
        # Keep the LLM context compact; full P01 statistics stay in profiles.yaml.
        context_keys = ("column", "non_null", "approx_distinct", "distinct_sample", "sample_exhaustive_in_input", "null_count", "empty_count", "usable_count")
        return {"tables": [{"name": name, "comment": t.get("table_comment"), "columns": t["columns"], "constraints": t["constraints"], "foreign_keys": t["foreign_keys"], "profiles": [{k: p[k] for k in context_keys} for p in t["profiles"]], "sample": list(self.rows(name, limit=3))} for name, t in self.tables.items() if name in selected], "evidence": [e for e in self.evidence.values() if e["source_ref"].get("table") in selected]}

    def rows(self, table, limit=None):
        info = self.tables[table]
        sql = f"SELECT * FROM {qi(info['sql_name'])} ORDER BY __r2_row"
        cursor = self.db.cursor().execute(sql + (f" LIMIT {int(limit)}" if limit is not None else ""))
        names = [d[0] for d in cursor.description]
        try:
            while batch := cursor.fetchmany(1000):
                for row in batch:
                    yield dict(zip(names, row))
        finally:
            cursor.close()

    @lru_cache(maxsize=4096)
    def lookup(self, table, bindings):
        info = self.tables[table]
        if any(v is None or v == "" for _, v in bindings):
            return []
        cols = [c for c, _ in bindings]
        if not set(cols) <= set(info["column_names"]):
            raise ValueError("Unknown lookup columns")
        index = "idx_" + digest([table, cols])[:20]
        if index not in self.indexes:
            self.db.execute(f"CREATE INDEX {qi(index)} ON {qi(info['sql_name'])} ({','.join(map(qi, cols))})")
            self.indexes.add(index)
        sql = f"SELECT * FROM {qi(info['sql_name'])} WHERE " + " AND ".join(f"{qi(c)} = ?" for c in cols) + " LIMIT 2"
        cursor = self.db.execute(sql, [v for _, v in bindings])
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]

    def record_id(self, table, row):
        # A source-local identity never claims cross-source business equality.
        t = self.tables[table]
        key = {k: row[k] for k in t["pk"]} if t["pk"] and all(row.get(k) not in (None, "") for k in t["pk"]) else {"file": t["csv_hash"], "row": row["__r2_row"]}
        # The row position disambiguates invalid/repeated declared keys as well.
        return table + ":" + digest([key, row["__r2_row"], self.snapshot_id])[:24]

    def close(self):
        self.lookup.cache_clear()
        self.db.close()


class Sink:
    def __init__(self, output, shard_size=5000):
        self.output, self.shard_size = Path(output), shard_size
        self.db = sqlite3.connect(self.output / "work/results.sqlite")
        self.db.execute("CREATE TABLE items (kind TEXT, id TEXT, body TEXT, PRIMARY KEY(kind,id))")

    def put(self, kind, item):
        result = self.db.execute("INSERT OR IGNORE INTO items VALUES (?,?,?)", (kind, item["id"], json.dumps(item, ensure_ascii=False)))
        if result.rowcount == 0 and "evidence_ids" in item:
            old = json.loads(self.db.execute("SELECT body FROM items WHERE kind=? AND id=?", (kind, item["id"])).fetchone()[0])
            old["evidence_ids"] = sorted(set(old.get("evidence_ids", [])) | set(item["evidence_ids"]))
            if "plan_id" in item:
                old["plan_ids"] = sorted(set(old.get("plan_ids", [old.get("plan_id")])) | {item["plan_id"]})
            self.db.execute("UPDATE items SET body=? WHERE kind=? AND id=?", (json.dumps(old, ensure_ascii=False), kind, item["id"]))

    def count(self, kind):
        return self.db.execute("SELECT count(*) FROM items WHERE kind=?", (kind,)).fetchone()[0]

    def export(self, progress=None):
        self.db.commit()
        counts = {}
        sizes = self.db.execute("SELECT kind, count(*) FROM items GROUP BY kind ORDER BY kind").fetchall()
        total = sum((size + self.shard_size - 1) // self.shard_size for _, size in sizes)
        export_task = progress.task("导出 YAML", total) if progress else nullcontext(None)
        with export_task as stage:
            for kind, _ in sizes:
                cursor = self.db.execute("SELECT body FROM items WHERE kind=? ORDER BY id", (kind,))
                counts[kind], part = 0, 0
                while rows := cursor.fetchmany(self.shard_size):
                    write_yaml(self.output / kind / f"part-{part:05}.yaml", [json.loads(r[0]) for r in rows])
                    counts[kind] += len(rows)
                    part += 1
                    if stage:
                        stage.advance(detail=kind)
        return counts

    def close(self):
        self.db.close()
