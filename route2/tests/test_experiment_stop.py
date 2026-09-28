"""A model SDK swallowing task cancellation must not continue paid work."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='Windows TerminateProcess bypasses Python signal handlers')
def test_detached_runner_stops_even_if_build_swallows_cancellation(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text('llm:\n  mode: mock\n')
    output = tmp_path / 'run'
    runner = Path(__file__).resolve().parents[1] / 'scripts/run_local_experiment.py'
    program = '''
import asyncio, importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('experiment', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
async def stubborn_build(config, output):
    output.mkdir()
    print('ready', flush=True)
    while True:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
module.build = stubborn_build
sys.argv = ['experiment', '--config', sys.argv[2], '--output', sys.argv[3]]
module.main()
'''
    child = subprocess.Popen([sys.executable, '-u', '-c', program, str(runner), str(config), str(output)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert 'experiment_started' in child.stdout.readline()
        assert child.stdout.readline().strip() == 'ready'
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 128 + signal.SIGTERM
        state = json.loads((output / 'process_exit.json').read_text())
        assert state['status'] == 'interrupted'
        assert state['signal'] == signal.SIGTERM
        assert (output / 'experiment_snapshot.json').exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
