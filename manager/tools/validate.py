#!/usr/bin/env python3
"""Run each Manager unittest module with private synthetic data and no real keys.

For a network guarantee on Linux: sudo unshare --net /path/to/venv/bin/python
tools/validate.py --isolated. Nothing here loads production data or credentials.
"""
import argparse,concurrent.futures,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def module(name,directory):
    directory=Path(directory);directory.mkdir(mode=0o700,exist_ok=True)
    auth=directory/'auths';auth.mkdir(mode=0o700,exist_ok=True)
    config=directory/'upstream.json'
    config.write_text(json.dumps({'api_key':'synthetic-fixture','auth_dir':str(auth)}))
    os.environ.update({'WB_DATA_DIR':str(directory),'WB_DB':str(directory/'manager.db'),
        'WB_USERS_FILE':str(directory/'users.json'),'WB_AUTH_DIR':str(auth),
        'WB_UPSTREAM_CONFIG':str(config),'WB_STATIC_DIR':str(ROOT/'web/out'),
        'WB2API_BASE':'http://127.0.0.1:17863','WB2API_CONTAINER':'wb-ci-isolation',
        'WB_UPSTREAM_DIR':str(directory/'upstream'),'WB2API_MODE':'docker',
        'WB_TRUST_PROXY':'0','WB_ENABLE_DOCS':'0','WB_INSTALL_DIR':str(directory),
        'WB_SERVICE_NAME':'workbuddy-ci-isolation.service','WB_MANAGER_HOST':'127.0.0.1',
        'PYTHONDONTWRITEBYTECODE':'1'})
    os.chdir(ROOT);sys.path.insert(0,str(ROOT))
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName('server.tests.'+name))
    data={'module':name,'tests':result.testsRun,'failures':len(result.failures),
          'errors':len(result.errors),'skips':len(result.skipped)}
    (directory/'result.json').write_text(json.dumps(data))
    return 0 if result.wasSuccessful() else 1

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--module');parser.add_argument('--data')
    parser.add_argument('--isolated',action='store_true');parser.add_argument('--output',default='validation-results')
    options=parser.parse_args()
    if options.module:return module(options.module,options.data)
    if options.isolated:
        if os.readlink('/proc/self/ns/net')==os.readlink('/proc/1/ns/net'):
            raise RuntimeError('isolated verification requires a separate network namespace')
        subprocess.run(['ip','link','set','lo','up'],check=True)
    output=Path(options.output).resolve();output.mkdir(parents=True,exist_ok=True)
    def run(name):
        directory=output/name;directory.mkdir(mode=0o700,exist_ok=True)
        with (directory/'test.log').open('wb') as log:
            try:code=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--module',name,'--data',str(directory)],
                                    stdout=log,stderr=subprocess.STDOUT,timeout=180).returncode
            except subprocess.TimeoutExpired:code=124
        result=directory/'result.json'
        data=json.loads(result.read_text()) if result.exists() else {'module':name,'tests':0,'errors':1,'failures':0,'skips':0}
        data['exit_code']=code;return data
    names=sorted(p.stem for p in (ROOT/'server/tests').glob('test_*.py'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(run,names))
    summary={'modules':len(results),**{key:sum(row[key] for row in results) for key in ('tests','failures','errors','skips')},
             'failed_modules':[row['module'] for row in results if row['exit_code']],
             'production_model_calls':0,'network_namespace_isolated':options.isolated}
    (output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary))
    return int(bool(summary['failed_modules']))
if __name__=='__main__':raise SystemExit(main())
