import json
import os
from pathlib import Path
import subprocess
import sys

from test_cache_report import REQUEST


def test_two_host_processes_publish_then_replay_with_no_source_repetition(tmp_path):
    """The synthetic host invokes real production orchestration in separate interpreters."""
    root = Path(__file__).parents[1]
    code = '''
import json,sys
from pathlib import Path
from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.account import AccountScope
from portfolio_analyst.cache.public import PublicCache
from portfolio_analyst.cache.workflow import CachedWorkflow
from portfolio_analyst.settings import Settings
from test_cache_account import Source
from test_cache_core import NOW
from test_cache_public import QUERY,URL
from test_cache_report import REQUEST,report,coverage
base=Path(sys.argv[1]);home=base/'private';home.mkdir(mode=0o700,exist_ok=True)
source=Source([NOW]);source.home=home
pub=CacheStore(base/'public',clock=lambda:NOW)
priv=CacheStore(home/'cache/v1',scope='private',clock=lambda:NOW)
flow=CachedWorkflow(Settings(home),priv,pub,version='test-v1',invoke=source,
                    resolver=lambda settings,ids:AccountScope('a'*64,('90000001',)))
trace=[]
result=flow.prepare(REQUEST)
trace.append({'tool':'cached_analysis.prepare','route':result['route']})
if result['route']=='components_ready':
 discovery=PublicCache(pub).publish({**QUERY,'dataset':'news','parameters':{'window':'recent'}},{'observations':[]},source_url=URL,source_as_of=NOW,available_at=NOW)
 evidence=PublicCache(pub).publish(QUERY,{'observations':[{'text':'facts'}]},source_url=URL,source_as_of=NOW,available_at=NOW,dependencies=[pub.discovery_ref(discovery)])
 snap=result['envelope']['snapshot']
 result=flow.finalize(REQUEST,result['context'],report(snap),coverage(snap),[evidence['ref']])
 trace.append({'tool':'cached_analysis.finalize','route':result['route']})
print(json.dumps({'route':result['route'],'commands':source.calls,'trace':trace}))
'''
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root), str(root / "tests")))}
    receipts = []
    for _ in range(2):
        result = subprocess.run([sys.executable, "-B", "-c", code, str(tmp_path)],
                                env=env, capture_output=True, text=True, check=True)
        receipts.append(json.loads(result.stdout))
    assert receipts[0]["route"] == "report_published"
    assert receipts[0]["commands"] == ["snapshot", "risk", "technical"]
    assert receipts[1]["route"] == "report_hit"
    assert receipts[1]["commands"] == []
    assert receipts[1]["trace"] == [{"tool": "cached_analysis.prepare", "route": "report_hit"}]


def test_cli_rejects_unverified_scope_and_unknown_flags_without_cache_writes(tmp_path):
    root = Path(__file__).parents[1]
    path = tmp_path / "request.json"
    path.write_text(json.dumps({**REQUEST, "account_scope": "forged"}))
    path.chmod(0o600)
    env = {**os.environ, "PORTFOLIO_AGENT_HOME": str(tmp_path / "private"),
           "RESEARCH_CACHE_HOME": str(tmp_path / "public")}
    result = subprocess.run([sys.executable, "-B", str(root / "scripts/cached_analysis.py"),
                             "--request", str(path), "--json"], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "INVALID_REQUEST"
    assert not (tmp_path / "private").exists()
    assert not (tmp_path / "public").exists()


def test_cli_help_does_not_initialize_account_or_cache(tmp_path):
    root = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "-B", str(root / "scripts/cached_analysis.py"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "--research-refs" in result.stdout


def test_actual_cli_prepare_finalize_and_replay_receipts_in_distinct_processes(tmp_path):
    """Invoke the actual CLI parser/control-file route, replacing only identity/source at the boundary."""
    from test_cache_report import report, coverage
    root = Path(__file__).parents[1]
    driver = '''
import json,sys
from pathlib import Path
from portfolio_analyst.cache import CacheStore
from portfolio_analyst.cache.account import AccountScope
from portfolio_analyst.cache.public import PublicCache
from portfolio_analyst.cache.workflow import CachedWorkflow
import portfolio_analyst.cache.workflow_cli as cli
from test_cache_account import Source
from test_cache_core import NOW
from test_cache_public import QUERY,URL
home=Path(sys.argv[1]);home.mkdir(mode=0o700,exist_ok=True)
source=Source([NOW]);source.home=home
original=CachedWorkflow
pub=CacheStore(home.parent/'public',clock=lambda:NOW)
priv=CacheStore(home/'cache/v1',scope='private',clock=lambda:NOW)
def factory(settings,private,public):
 return original(settings,priv,pub,version='cli-fixture-v1',invoke=source,
                 resolver=lambda settings,ids:AccountScope('a'*64,('90000001',)))
cli.CachedWorkflow=factory
if '--report' in sys.argv:
 public=PublicCache(pub)
 discovery=public.publish({**QUERY,'dataset':'news','parameters':{'window':'recent'}},{'observations':[]},source_url=URL,source_as_of=NOW,available_at=NOW)
 evidence=public.publish(QUERY,{'observations':[{'text':'facts'}]},source_url=URL,source_as_of=NOW,available_at=NOW,dependencies=[pub.discovery_ref(discovery)])
 path=Path(sys.argv[sys.argv.index('--research-refs')+1]);path.write_text(json.dumps([evidence['ref']]))
 path.chmod(0o600)
code=cli.main(sys.argv[2:])
print(json.dumps({'source_commands':source.calls}),file=sys.stderr)
raise SystemExit(code)
'''
    def control(name, value):
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)
    request = control('request', REQUEST)
    home = tmp_path / 'private'
    env = {**os.environ, 'PYTHONPATH':os.pathsep.join((str(root),str(root/'tests'))),
           'PORTFOLIO_AGENT_HOME':str(home), 'RESEARCH_CACHE_HOME':str(tmp_path/'public')}
    def execute(*arguments):
        output = subprocess.run([sys.executable,'-B','-c',driver,str(home),'--request',request,'--json',*arguments],
                                capture_output=True,text=True,env=env,check=True)
        return json.loads(output.stdout), json.loads(output.stderr)
    first, cold_trace = execute()
    assert first['route'] == 'components_ready'
    snap = first['envelope']['snapshot']
    published, finish_trace = execute('--context',control('context',first['context']),
        '--report',control('markdown',report(snap)), '--coverage',control('coverage',coverage(snap)),
        '--research-refs',control('refs',[]))
    hot, hot_trace = execute()
    assert published['route'] == 'report_published' and hot['route'] == 'report_hit'
    assert hot['markdown'] == published['markdown']
    assert cold_trace['source_commands'] == ['snapshot','risk','technical']
    assert finish_trace['source_commands'] == hot_trace['source_commands'] == []
