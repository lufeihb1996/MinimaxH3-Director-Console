"""Import the real app and exercise HTTP routes in a disposable workspace.
No lifespan workers, production DB access, remote requests or generation calls.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CHECK = r'''
from pathlib import Path
from fastapi.testclient import TestClient
from backend.app import app, DB_PATH
assert DB_PATH.resolve().is_relative_to(Path.cwd().resolve())
# No context manager: do not start lifespan queues or recovery tasks.
client = TestClient(app)
assert client.get('/api/video-models').json()['default_model'] == 'b15_49'
assert client.get('/api/video-models').headers['cache-control'] == 'no-store'
assert client.get('/static/app.js').headers['cache-control'] == 'no-cache'
page = client.get('/').text
assert '快捷标签' not in page and 'prompt-toolbar' not in page
p = client.post('/api/projects', json={'name': 'isolated regression fixture'})
assert p.status_code == 200, p.text
s = client.post(f"/api/projects/{p.json()['id']}/scenes", json={'name': 'fixture'})
assert s.status_code == 200, s.text
scene = s.json()['id']
a = client.post(f'/api/scenes/{scene}/shots', json={'mode': 'T2V'})
assert a.status_code == 200, a.text
shot = a.json()
assert (shot['video_model'], shot['steps'], shot['megapixels']) == ('b15_49', 10, 0.4)
assert shot['aspect_ratio'] == '9:16'
portrait = client.post(f'/api/scenes/{scene}/shots', json={'mode': 'T2V', 'aspect_ratio': '9:16'}).json()
assert client.patch(f"/api/shots/{portrait['id']}", json={'title': 'keep portrait'}).json()['aspect_ratio'] == '9:16'
c = client.patch(f"/api/shots/{shot['id']}", json={'video_model': 'b15_49', 'steps': 20})
assert c.status_code == 200 and c.json()['steps'] == 10, c.text
assert client.patch(f"/api/shots/{shot['id']}", json={'video_model': 'missing'}).status_code == 400
assert client.get(f'/api/scenes/{scene}/shots').json()[0]['status'] == 'PENDING'
print('PASS: real app import, model catalog, shot create/patch and validation; isolated DB; workers never started.')
'''

with tempfile.TemporaryDirectory(prefix='anime-studio-api-') as folder:
    target = Path(folder)
    for name in ['backend', 'config', 'workflows', 'frontend']:
        shutil.copytree(ROOT/name, target/name, ignore=shutil.ignore_patterns('__pycache__'))
    env = dict(os.environ, PYTHONPATH=str(target), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', CHECK], cwd=target, env=env, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
