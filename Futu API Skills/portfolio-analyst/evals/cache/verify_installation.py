"""Offline wheel/install smoke in an isolated temporary venv; never installs dependencies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def run(command, *, cwd, env):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def verify():
    with tempfile.TemporaryDirectory(prefix='cache-wheel-check-') as temp:
        base = Path(temp).resolve()
        env = {key: value for key, value in os.environ.items() if key not in {'PYTHONPATH', 'PYTHONHOME'}}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        wheels = base / 'wheels'
        wheels.mkdir()
        for source, items in ((ROOT, ('portfolio_analyst', 'schemas', 'references', 'SKILL.md', 'pyproject.toml')),
                              (ROOT.parent / 'sec-research-core', ('sec_research', 'pyproject.toml'))):
            stage = base / source.name
            stage.mkdir()
            for name in items:
                origin, destination = source / name, stage / name
                if origin.is_dir():
                    shutil.copytree(origin, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                else:
                    shutil.copyfile(origin, destination)
            run([sys.executable, '-B', '-c', 'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])', str(wheels)], cwd=stage, env=env)
        venv = base / 'venv'
        run([sys.executable, '-m', 'venv', '--system-site-packages', str(venv)], cwd=base, env=env)
        python = str(venv / 'bin/python')
        run([python, '-m', 'pip', 'install', '--no-index', '--no-deps', *map(str, wheels.glob('*.whl'))], cwd=base, env=env)
        probe = '''
import json,sys
from pathlib import Path
import portfolio_analyst,sec_research
from portfolio_analyst.cache.paths import stores
from portfolio_analyst.cache.workflow import CachedWorkflow
from portfolio_analyst.cache.resources import policy_root
from portfolio_analyst.schema import _schema_path
from portfolio_analyst.settings import Settings
base=Path(sys.argv[1]);home=base/'private'
public,private=stores(home)
flow=CachedWorkflow(Settings(home),private,public)
assert Path(portfolio_analyst.__file__).is_relative_to(Path(sys.prefix))
assert Path(sec_research.__file__).is_relative_to(Path(sys.prefix))
assert policy_root().is_relative_to(Path(sys.prefix))
assert _schema_path('cache_request.schema.json').is_file()
assert _schema_path('cached_analysis_request.schema.json').is_file()
from sec_research.cache import CacheBridge
assert CacheBridge().entry==['-m','portfolio_analyst.cache.cli']
receipt=CacheBridge(root=base/'public')._call('status')
assert receipt['scope']=='public' and receipt['records']==0
print(json.dumps({'package_version':portfolio_analyst.__version__,'installed_import':True,'policies':True,'schemas':True,'sec_bridge':True}))
'''
        result = json.loads(run([python, '-B', '-c', probe, str(base)], cwd=base, env=env))
        for command in ('portfolio-cache', 'portfolio-cached-analysis', 'portfolio-analyst'):
            assert 'usage:' in run([str(venv / 'bin' / command), '--help'], cwd=base, env=env)
        result['cli_help'] = 3
        return result


if __name__ == '__main__':
    print(json.dumps(verify(), indent=2))
