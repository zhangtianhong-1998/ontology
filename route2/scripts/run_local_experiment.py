"""Run one local experiment in a terminal; write its exit state, never poll it.

Credentials are read in memory. Environment paths and model names may be logged;
secret values and provider exception bodies are deliberately not printed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import traceback

from dotenv import dotenv_values
from ontology_r2.pipeline import build, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--expect-model', default='deepseek-v4-flash')
    args = parser.parse_args()
    # Detached shells may inherit SIGINT as ignored; keep the run stoppable.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if args.output.exists():
        parser.error('Output exists; choose a new directory')
    config = load_config(args.config)
    environment = args.env_file or Path(config.get('env_file', '.env'))
    values = dotenv_values(environment) if environment.is_file() else {}
    if config.get('llm', {}).get('mode') != 'mock':
        for suffix in ('MODEL', 'BASE_URL', 'API_KEY'):
            key = 'ONTOLOGY_LLM_' + suffix
            value = values.get(key) or values.get('LLM_' + suffix) or os.environ.get(key)
            if not value:
                parser.error(f'Missing {key}; configure the local environment file')
            os.environ[key] = value
        if os.environ['ONTOLOGY_LLM_MODEL'] != args.expect_model:
            parser.error('Configured model does not match --expect-model')
    root = Path(__file__).resolve().parents[2]
    def git(*command):
        return subprocess.check_output(['git', *command], cwd=root, text=True).strip()
    snapshot = {'commit': git('rev-parse', 'HEAD'), 'branch': git('branch', '--show-current'),
                'working_tree_clean': not bool(git('status', '--porcelain')),
                'config': str(args.config.resolve()),
                'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
                'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'pid': os.getpid(), 'model': os.environ.get('ONTOLOGY_LLM_MODEL', 'mock')}
    print(json.dumps({'experiment_started': snapshot, 'output': str(args.output.resolve())}, ensure_ascii=False), flush=True)
    started = time.monotonic()
    exit_code = 1
    result = {}
    try:
        result = asyncio.run(build(config, args.output))
        exit_code = 1 if result.get('status') == 'failed' else 0
        print(json.dumps({key: result.get(key) for key in ('status', 'input_tables', 'input_records', 'elapsed_seconds', 'partial_reasons', 'llm')}, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        result = {'status': 'interrupted'}
        exit_code = 130
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__,
                  'frames': [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                             for frame in traceback.extract_tb(exc.__traceback__)]}
        print(json.dumps(result), flush=True)
    finally:
        if args.output.exists():
            (args.output / 'experiment_snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
            (args.output / 'process_exit.json').write_text(json.dumps({
                'status': result.get('status'), 'exit_code': exit_code,
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'finished_at': dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2))
    print(f'Experiment exited with code {exit_code}.', flush=True)
    if (args.output / 'viewer.html').exists():
        print(f'Viewer: {args.output / "viewer.html"}', flush=True)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
