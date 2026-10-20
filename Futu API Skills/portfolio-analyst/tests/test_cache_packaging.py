import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def test_wheel_configuration_includes_cache_entrypoints_schemas_and_policy():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    assert 'portfolio_analyst.cache' in config['tool']['setuptools']['packages']
    assert config['project']['scripts']['portfolio-cached-analysis'] == 'portfolio_analyst.cache.workflow_cli:main'
    assert config['project']['scripts']['portfolio-cache'] == 'portfolio_analyst.cache.cli:main'
    data = config['tool']['setuptools']['data-files']
    assert 'schemas/cache_request.schema.json' in data['schemas']
    assert 'schemas/cached_analysis_request.schema.json' in data['schemas']
    assert data['share/moomoo-portfolio-analyst'] == ['SKILL.md']


def test_policy_lookup_works_outside_a_source_checkout(tmp_path, monkeypatch):
    from portfolio_analyst.cache import resources
    prefix = tmp_path / 'installed'
    policy = prefix / 'share/moomoo-portfolio-analyst'
    policy.mkdir(parents=True)
    (policy / 'SKILL.md').write_text('installed policy')
    (policy / 'references').mkdir()
    monkeypatch.setattr(resources, '__file__', str(tmp_path / 'lib/portfolio_analyst/cache/resources.py'))
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    assert resources.policy_root() == policy


def test_module_workflow_entrypoint_does_not_silently_exit():
    result = subprocess.run([sys.executable, '-B', '-m', 'portfolio_analyst.cache.workflow_cli', '--help'],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert '--research-refs' in result.stdout
