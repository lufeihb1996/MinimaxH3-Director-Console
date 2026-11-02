"""Full API/queue/finalizer checks in a copied workspace with a fake Pika server."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CHECK = r'''
import asyncio, json, subprocess, uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from backend.app import app, DB_PATH
from backend.core.db import db, get_settings, save_settings
from backend.services.repo import get_shot, update_shot_state
from backend.services.video import run_shot, recover_running_shot

assert DB_PATH.resolve().is_relative_to(Path.cwd().resolve())
subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=64x64:r=24:d=0.25',
                '-c:v','libx264','-pix_fmt','yuv420p','fixture.mp4'],check=True)
video = Path('fixture.mp4').read_bytes()
jobs, submissions, uploads, seen_hosts = {}, [], [], []
fail_next = False
async def fake(request):
    global fail_next
    seen_hosts.append(request.url.host)
    path = request.url.path
    if path == '/api/health':
        return httpx.Response(200,json={'service':'pika-h3-flex-studio','engine_ready':True,'authenticated':True})
    if path == '/api/uploads':
        uid = uuid.uuid4().hex
        uploads.append((uid, bytes(request.content)))
        return httpx.Response(201,json={'id':uid,'kind':'image'})
    if path == '/api/jobs' and request.method == 'GET':
        return httpx.Response(200,json={'jobs':list(jobs.values())})
    if path == '/api/jobs' and request.method == 'POST':
        body = json.loads(request.content); submissions.append(body)
        job = {'id':body['shot_id'],'version':1,'status':'failed' if fail_next else 'done',
               'error':'simulated generation failure' if fail_next else '',
               'video_bytes':len(video),'progress':{'phase':'done'}}
        jobs[job['id']] = job
        return httpx.Response(201,json=job)
    if path.startswith('/media/'):
        return httpx.Response(200,content=video,headers={'Content-Type':'video/mp4'})
    return httpx.Response(404,json={'error':'fake endpoint not found'})

api = TestClient(app)  # No lifespan: workers never submit anything automatically.
save_settings({'pika_url':'http://pika.test','comfy_url':'http://unreachable-comfy.test'})
project = api.post('/api/projects',json={'name':'isolated Pika fixture'}).json()
scene = api.post(f"/api/projects/{project['id']}/scenes",json={'name':'fixture'}).json()
sid = scene['id']
catalog = api.get('/api/video-models').json()
assert catalog['default_model'] == 'b15_49'
assert any(m['id']=='pika_h3' for m in catalog['models'])

def create(mode='T2V'):
    response=api.post(f'/api/scenes/{sid}/shots',json={'video_model':'pika_h3','mode':mode,'seconds':5,
                                                            'megapixels':0.4,'prompt':'Test prompt'})
    assert response.status_code==200,response.text
    return response.json()

shot=create()
assert shot['steps']==6 and not shot['second_pass_enabled']
assert api.patch(f"/api/shots/{shot['id']}",json={'seconds':5.5}).status_code==400
assert api.patch(f"/api/shots/{shot['id']}",json={'lip_sync_enabled':True}).status_code==400
assert api.post(f"/api/shots/{shot['id']}/generate").status_code==200
queued=get_shot(shot['id'])
snapshot=json.loads(Path(queued['queued_input_path']).read_text(encoding='utf-8'))
assert snapshot['shot']['pika_endpoint']=='http://pika.test'
save_settings({'pika_url':'http://changed-after-queue.test'})

async def run_check():
    global fail_next
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as remote:
        with patch('backend.core.pika.get_comfy_http_client',return_value=remote):
            await run_shot(shot['id'])
            done=get_shot(shot['id'])
            assert done['status']=='DONE',done
            assert Path(done['output_path']).is_file() and Path(done['last_frame_path']).is_file()
            assert done['completed_input_path']==queued['queued_input_path']
            assert set(seen_hosts)=={'pika.test'},seen_hosts
            old_path=done['output_path'];old_bytes=Path(old_path).read_bytes()
            assert submissions[-1]['task']=='t2va' and submissions[-1]['duration']==5

            # New local version, independent remote ID. A failed redo retains old MP4.
            save_settings({'pika_url':'http://pika.test'})
            assert api.post(f"/api/shots/{shot['id']}/generate").status_code==200
            fail_next=True
            await run_shot(shot['id'])
            failed=get_shot(shot['id'])
            assert failed['status']=='DONE' and failed['output_path']==old_path,failed
            assert 'simulated generation failure' in failed['error']
            assert Path(old_path).read_bytes()==old_bytes
            assert submissions[0]['shot_id']!=submissions[1]['shot_id']
            fail_next=False

            # Recovery uses the pinned execution endpoint and produces version 2.
            assert api.post(f"/api/shots/{shot['id']}/generate").status_code==200
            current=get_shot(shot['id']);folder=Path(current['queued_input_path']).parent
            jid=uuid.uuid4().hex
            (folder/'execution.json').write_text(json.dumps({'backend':'pika','endpoint':'http://pika.test','job_id':jid}))
            jobs[jid]={'id':jid,'status':'done','version':1,'video_bytes':len(video),'progress':{'phase':'done'}}
            update_shot_state(shot['id'],status='RUNNING',prompt_id=jid)
            save_settings({'pika_url':'http://changed-again.test'})
            await recover_running_shot(shot['id'],jid)
            recovered=get_shot(shot['id'])
            assert recovered['status']=='DONE' and recovered['output_path']!=old_path,recovered
            assert Path(old_path).is_file()
            with db() as c:
                versions=[r[0] for r in c.execute('SELECT version FROM generations WHERE shot_id=? ORDER BY version',(shot['id'],))]
            assert versions==[1,2],versions

            # CUT uploads refs; CONTINUE uploads only its resolved opening frame.
            save_settings({'pika_url':'http://pika.test'})
            for mode,task in [('CUT','ref2va'),('CONTINUE','i2va')]:
                row=create(mode)
                pic=Path('manual.png');pic.write_bytes(b'image-fixture')
                if mode=='CUT': update_shot_state(row['id'],ref1_path=str(pic.resolve()),ref3_path=str(pic.resolve()))
                else: update_shot_state(row['id'],first_frame_path=str(pic.resolve()),continuation_selection='')
                response=api.post(f"/api/shots/{row['id']}/generate")
                assert response.status_code==200,response.text
                await run_shot(row['id'])
                result=get_shot(row['id']);assert result['status']=='DONE',result
                body=submissions[-1];assert body['task']==task
                assert len(body['references'])==(2 if mode=='CUT' else 0)
                assert bool(body['first_image'])==(mode=='CONTINUE')
            assert len(uploads)==3

asyncio.run(run_check())

# An MV project cannot queue a Pika shot and does not silently disable MV.
api.put(f"/api/projects/{project['id']}/mv",json={'native_audio_enabled':True})
blocked=create()
response=api.post(f"/api/shots/{blocked['id']}/generate")
assert response.status_code==400 and 'MV' in response.text,response.text
assert get_shot(blocked['id'])['status']=='PENDING'
assert api.get(f"/api/projects/{project['id']}/mv").json()['native_audio_enabled']

# Scene-wide B15 second-pass toggle must not enable it for Pika.
api.post(f'/api/scenes/{sid}/second-pass',json={'second_pass_enabled':True})
assert not get_shot(shot['id'])['second_pass_enabled']
print('PASS: Pika API/three modes, frozen endpoint, real MP4 finalization, versions, recovery, old-output protection and MV rejection; isolated workspace; no GPU calls.')
'''

with tempfile.TemporaryDirectory(prefix="anime-pika-api-") as folder:
    target = Path(folder)
    for name in ("backend", "config", "workflows", "frontend"):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ, PYTHONPATH=str(target), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-c", CHECK], cwd=target, env=env, capture_output=True, text=True, encoding="utf-8")
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
