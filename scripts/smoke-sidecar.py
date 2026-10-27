"""Validate a native frozen service with isolated local fixtures; no user files or cloud model."""
from pathlib import Path
import argparse,hashlib,json,os,secrets,socket,subprocess,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import requests

project=Path.cwd()
parser=argparse.ArgumentParser(description='Frozen service regression using only generated sample files and a local fixture provider.')
parser.add_argument('--executable',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
exe=args.executable.resolve(strict=True)
args.output.parent.mkdir(parents=True,exist_ok=True)
root=Path(tempfile.mkdtemp(prefix='Sortly-Packaged-'))
state=Path(tempfile.mkdtemp(prefix='sidecar-state-',dir=str(project/'build')))
content='Plant research notes describe soil, tomato roots, compost and watering. '*4
originals={('Plant_habitat_research_notes_'*4)+'.txt':content,'notes.txt':content+' A second set of observations.','credentials.txt':'API_KEY=fixture-only-not-a-real-secret'}
for name,value in originals.items(): (root/name).write_text(value,encoding='utf-8')
hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in originals}
seen=[]; chat_calls=0
class Provider(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  global chat_calls
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  seen.append(body)
  assert 'fixture-only-not-a-real-secret' not in json.dumps(body)
  assert 'credentials.txt' not in json.dumps(body)
  system=body['messages'][0]['content']
  if system.startswith('You analyze files'):
   payload=json.loads(body['messages'][1]['content'])
   output={'files':[{'id':item['id'],'category':'Study','subcategory':'','confidence':'high','reasoning':'Plant research notes.','evidence_quote':'Plant research notes','filename_conflict':'none','suggested_name':'','name_evidence':''} for item in payload['files']]}
  else:
   chat_calls+=1
   selector={'extension':'.txt','glob':'*.txt'} if chat_calls==1 else {'extension':'.txt'}
   output={'reply':'Text files grouped under Study/General.','rules':[{'action':'move','match':selector,'target':'Study/General','reason':'user request'}],'needs_clarification':None}
  encoded=json.dumps({'choices':[{'message':{'content':json.dumps(output)}}]}).encode()
  self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

provider=ThreadingHTTPServer(('127.0.0.1',0),Provider)
threading.Thread(target=provider.serve_forever,daemon=True).start()
with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
env=os.environ.copy()
for name in ['DEEPSEEK_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','GOOGLE_API_KEY','ANTHROPIC_API_KEY','OPENROUTER_API_KEY','GROQ_API_KEY','HUGGINGFACE_API_KEY','HF_TOKEN']:
 env.pop(name,None)
token=secrets.token_hex(24)
env.update({'OPENAI_API_KEY':'fixture-key','ORGANIZER_MODELS':'openai/fixture','ORGANIZER_OPENAI_BASE_URL':f'http://127.0.0.1:{provider.server_port}/v1','ORGANIZER_HOME':str(state),'ORGANIZER_CACHE_DB':str(state/'cache.db'),'ORGANIZER_RECENT_SECONDS':'0','SORTLY_SHUTDOWN_TOKEN':token,'PYTHON_DOTENV_DISABLED':'1'})
log=args.output.with_suffix('.log').open('w',encoding='utf-8')
proc=subprocess.Popen([str(exe),'--port',str(port),'--log-level','warning'],cwd=root,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
base=f'http://127.0.0.1:{port}'
def post(route,body):
 r=requests.post(base+route,json=body,timeout=30); r.raise_for_status(); return r.json()
try:
 deadline=time.monotonic()+60
 while time.monotonic()<deadline:
  try:
   if requests.get(base+'/api/health',timeout=1).status_code==200: break
  except requests.RequestException: pass
  if proc.poll() is not None: raise RuntimeError('Packaged sidecar exited during startup.')
  time.sleep(.15)
 else: raise RuntimeError('Packaged API did not become healthy.')
 configured=post('/api/providers/config',{'primary':'openai','fallbacks':[],'connections':{'openai':{'model':'fixture','base_url':f'http://127.0.0.1:{provider.server_port}/v1','api_key':'fixture-key'}}})
 assert configured['connections']['openai']['has_api_key']
 stored=(state/'providers.json').read_text(encoding='utf-8')
 assert 'fixture-key' not in stored, 'Provider credentials must not be saved in plaintext.'
 plan=post('/api/preview',{'source':str(root),'models':['openai/fixture'],'rename':True})
 assert len(plan['files'])==3
 assert all(f['target_path']=='Study/General' for f in plan['files'] if not f['sensitive'])
 private=next(f for f in plan['files'] if f['name']=='credentials.txt')
 assert private['sensitive'] and not private['selected']
 long=next(f for f in plan['files'] if len(f['name'])>80)
 assert len(long['destination_name'])<=80 and long['renamed']
 chat=post('/api/chat',{'plan_id':plan['plan_id'],'message':'Put all text files in Study/General'})
 assert chat_calls==2, 'Packaged schema repair must retry once.'
 refined=chat['preview']; assert not next(f for f in refined['files'] if f['sensitive'])['selected']
 applied=post('/api/apply',{'plan_id':refined['plan_id'],'files':[{'id':f['id'],'accepted':f['selected'],'target_path':f['target_path'],'destination_name':f['destination_name']} for f in refined['files']]})
 assert applied['moved']==2 and applied['failed']==0
 rerun=post('/api/preview',{'source':str(root),'models':['openai/fixture'],'rename':True,'recursive':True})
 assert rerun['already_organized']==2
 assert all(not f['renamed'] and not f['selected'] for f in rerun['files'] if not f['sensitive'])
 undone=post('/api/undo',{'source':str(root)})
 assert undone['restored']==2 and not undone['skipped']
 for name,expected in hashes.items(): assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected
 report={'executable':str(exe),'sample_folder':str(root),'health':True,'native_credential_storage':True,'blank_subcategory_fallback':True,'long_name_shortening':True,'sensitive_excluded_and_not_sent':True,'schema_repair_calls':chat_calls,'moved':applied['moved'],'rerun_already_organized':rerun['already_organized'],'undo_restored':undone['restored'],'all_original_hashes_restored':True}
 args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
 print(json.dumps(report,indent=2))
finally:
 try: requests.post(base+'/api/shutdown',headers={'X-Sortly-Shutdown-Token':token},timeout=2)
 except requests.RequestException: pass
 try: proc.wait(timeout=10)
 except subprocess.TimeoutExpired: proc.terminate(); proc.wait(timeout=5)
 provider.shutdown(); log.close()
