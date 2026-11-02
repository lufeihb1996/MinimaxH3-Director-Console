"""Explicit reference ownership regression tests in a disposable app copy."""
from pathlib import Path
import tempfile, shutil, subprocess, sys, os
ROOT=Path(__file__).resolve().parents[2]
CHECK=r'''
from pathlib import Path
from fastapi.testclient import TestClient
from types import SimpleNamespace
from backend import app as appmod
from backend.core.ns import namespace
from backend.core.runtime import rt
import backend.services.images as images_svc
m=SimpleNamespace(**namespace());m.app=appmod.app
import asyncio
c=TestClient(m.app)
p=c.post('/api/projects',json={'name':'scope'}).json()['id']
q=c.post('/api/projects',json={'name':'other'}).json()['id']
s=[c.post(f'/api/projects/{p}/scenes',json={'name':str(i)}).json()['id'] for i in range(3)]
other=c.post(f'/api/projects/{q}/scenes',json={'name':'other'}).json()['id']
def cfg(scene,role,scope='scene',prompt='same'):
 r=c.put(f'/api/scenes/{scene}/image-configs/{role}',json={'prompt':prompt,'ref_scope':scope,'aspect_ratio':'16:9'});assert r.status_code==200,r.text
for scene in s+[other]:cfg(scene,'ref1')
assert m.matching_project_scene_ids_for_ref(s[0],'ref1','same')==[s[0]]
cfg(s[0],'ref1','project');cfg(s[1],'ref1','project');cfg(other,'ref1','project')
assert m.shared_ref_scene_ids(s[1],'ref1')==s[:2]
assert m.shared_ref_scene_ids(other,'ref1')==[other]
assert c.put(f'/api/scenes/{s[0]}/image-configs/ref3',json={'ref_scope':'project'}).status_code==400
# Joining a shared slot retains the shared prompt instead of erasing it.
assert c.get(f'/api/scenes/{s[1]}/image-configs').json()[0]['prompt']=='same'
a=c.post(f'/api/scenes/{s[0]}/shots',json={'mode':'CUT'}).json()['id']
b=c.post(f'/api/scenes/{s[1]}/shots',json={'mode':'CUT'}).json()['id']
d=c.post(f'/api/scenes/{s[1]}/shots',json={'mode':'CONTINUE'}).json()['id']
e=c.post(f'/api/scenes/{s[1]}/shots',json={'mode':'CUT'}).json()['id']
m.update_shot_state(e,status='RUNNING')
image=Path('picture.jpg');image.write_bytes(b'image-bytes')
count,ids=m.apply_scene_image_to_matching_cut_shots(s[0],image,'ref1','same',replace_existing=True)
assert count==2 and ids==s[:2]
assert not m.get_shot(d)['ref1_path'] and not m.get_shot(e)['ref1_path']
assert Path(m.get_shot(a)['ref1_path']).read_bytes()==b'image-bytes'
# Shared slot queue requests resolve to exactly one task and owner; no GPU workers start.
async def check_queue():
 first,reason=await m.enqueue_scene_image_job(s[1],'ref1');assert reason=='queued'
 second,reason=await m.enqueue_scene_image_job(s[0],'ref1');assert reason=='already_active' and first==second
 assert m.get_scene_image_job(first)['scene_id']==s[0]
 # Re-generating an already queued item must remove and then truly re-enqueue it.
 async def no_remote(*args):return False
 images_svc._interrupt_remote_prompt=no_remote
 await m.prepare_image_job_regeneration(first)
 assert m.get_scene_image_job(first)['status']=='PENDING'
 third,reason=await m.enqueue_scene_image_job(s[0],'ref1')
 assert third==first and reason=='queued' and m.scene_image_queue.qsize()==1

asyncio.run(check_queue())
# Dashboard resolves shared history to the owner, local scene remains separate.
conn=m.db();conn.execute("INSERT INTO scene_images(scene_id,role,prompt,aspect_ratio,width,height,steps,seed,prompt_id,output_path,applied_count) VALUES(?, 'ref1','same','16:9',10,10,4,42,'',?,0)",(s[0],str(image.resolve())));conn.commit();conn.close()
dash=c.get(f'/api/projects/{p}/image-dashboard').json()['scenes']
assert dash[1]['roles']['ref1']['latest_image']['id']==dash[0]['roles']['ref1']['latest_image']['id']
assert dash[2]['roles']['ref1']['latest_image'] is None
assert c.get(f'/api/scenes/{s[1]}/images').json()[0]['scene_id']==s[0]
# Future shared CUT inherits the applied default.
f=c.post(f'/api/scenes/{s[1]}/shots',json={'mode':'CUT'}).json()
assert Path(f['ref1_path']).read_bytes()==b'image-bytes'
# Project queue metadata must not expose another project's task.
pq=c.get(f'/api/image-queue/status?project_id={p}').json()
qq=c.get(f'/api/image-queue/status?project_id={q}').json()
assert pq['queue_size']==1 and qq['queue_size']==0
assert all(job['project_id']==p for job in pq['jobs']) and qq['jobs']==[]
rt.current_scene_image_job_id=pq['jobs'][0]['id']
assert c.get(f'/api/image-queue/status?project_id={q}').json()['current_job_id'] is None
assert c.get(f'/api/image-queue/status?project_id={p}').json()['current_job_id']==rt.current_scene_image_job_id
print('PASS explicit scope, same-prompt isolation, project boundary, CUT/active protection, queue dedup, shared history and future-shot inheritance')
'''
with tempfile.TemporaryDirectory(prefix='studio-ref-scope-') as folder:
 target=Path(folder)
 for name in ['backend','config','workflows','frontend']:
  shutil.copytree(ROOT/name,target/name,ignore=shutil.ignore_patterns('__pycache__'))
 staged=ROOT/'data/ref_scope_backend.pending'
 if staged.exists():(target/'backend/app.py').write_bytes(staged.read_bytes())
 r=subprocess.run([sys.executable,'-c',CHECK],cwd=target,env={**os.environ,'PYTHONPATH':str(target),'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
 print(r.stdout)
 if r.returncode:print(r.stderr);raise SystemExit(r.returncode)
