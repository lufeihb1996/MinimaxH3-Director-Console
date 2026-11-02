"""A deferred video item must not starve the independent image worker."""
import ast,asyncio
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
source=ROOT/'data/image_wait_fix.pending'
if not source.exists():source=ROOT/'backend/services/images.py'
tree=ast.parse(source.read_text(encoding='utf-8'))
node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='scene_image_queue_worker')
async def check():
 image_queue=asyncio.Queue();video_queue=asyncio.Queue();video_queue.put_nowait(179)
 finished=asyncio.Event();calls=[]
 async def run_image(job):calls.append(job);finished.set()
 ns=dict(asyncio=asyncio,scene_image_queue=image_queue,job_queue=video_queue,rt=__import__('types').SimpleNamespace(current_shot_id=None,current_scene_image_job_id=None,active_scene_image_task=None),run_scene_image_job=run_image)
 exec(compile(ast.Module(body=[node],type_ignores=[]),'worker','exec'),ns)
 worker=asyncio.create_task(ns['scene_image_queue_worker']())
 try:
  image_queue.put_nowait(94)
  await asyncio.wait_for(finished.wait(),timeout=1)
  assert calls==[94] and video_queue.qsize()==1
 finally:
  worker.cancel()
  try:await worker
  except asyncio.CancelledError:pass
 print('PASS queued deferred video remains intact and does not prevent image execution; no GPU used')
asyncio.run(check())
