"""Deferred CONTINUE checks: no production DB, network, or GPU."""
import json
import ast
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_video_profiles import namespace
from backend.services.continuation import freeze_inputs, auto_wait_reason, resolve_auto_input, trim_for_continuity, is_auto_continue
from fastapi import HTTPException

class AutoContinueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.ns = namespace()
        self.ns['ensure_shot_dir'] = lambda shot: self.folder
        self.source = self.folder/'latest.mp4'; self.source.write_bytes(b'new-video')
        self.prev = dict(id=1, scene_id=3, shot_no=1, status='RUNNING', error='', output_path=str(self.source))
        self.ns['previous_shot'] = lambda shot: self.prev
        self.ns['get_shot'] = lambda shot_id: self.prev
        self.ns['preferred_shot_video'] = lambda prev: Path(prev['output_path']) if Path(prev['output_path']).is_file() else None
        self.shot = dict(id=2, scene_id=3, shot_no=2, mode='CONTINUE', video_model='b15_49', steps=10,
            seed=123, megapixels=.4, aspect_ratio='16:9', seconds=5, prompt='Saved prompt',
            first_frame_path='', continuation_selection='', realism_lora_enabled=False, naughty_lora_enabled=False)

    def frozen(self):
        path = freeze_inputs(self.shot, self.ns)
        shot = json.loads(Path(path).read_text(encoding='utf-8'))['shot']
        shot['queued_input_path'] = path
        return shot

    def test_enqueue_without_frame_and_without_completed_source(self):
        frozen = self.frozen()
        self.assertEqual(frozen['auto_source_shot_id'], 1)
        self.assertEqual(frozen['first_frame_path'], '')
        self.assertIn('等待上一镜完成', auto_wait_reason(frozen, self.ns))

    def test_failure_with_old_video_waits_then_resumes(self):
        frozen = self.frozen(); self.prev.update(status='DONE', error='retry failed')
        self.assertIn('失败', auto_wait_reason(frozen, self.ns))
        self.prev['error'] = ''
        self.assertEqual(auto_wait_reason(frozen, self.ns), '')

    def test_missing_video_waits(self):
        frozen = self.frozen(); self.prev.update(status='DONE', output_path=str(self.folder/'missing.mp4'))
        self.assertIn('不可用', auto_wait_reason(frozen, self.ns))

    def test_auto_never_crosses_scene(self):
        self.prev['scene_id'] = 4
        with self.assertRaises(HTTPException): self.frozen()

    def test_resolve_uses_latest_and_keeps_queued_prompt(self):
        frozen = self.frozen(); self.prev.update(status='DONE')
        latest = self.folder/'replacement.mp4';latest.write_bytes(b'latest-version')
        self.prev['output_path'] = str(latest)
        self.shot['prompt'] = 'Edited after enqueue'
        frame = self.folder/'picked.png';frame.write_bytes(b'frame')
        self.ns.update(media_duration_seconds=lambda p: 5, media_video_info=lambda p: {'avg_frame_rate':'24/1'}, _rate_to_float=lambda r:24)
        updates=[];self.ns['update_shot_state'] = lambda *args, **kwargs:updates.append(kwargs)
        def candidates(shot, ns, center):
            self.assertEqual(ns['preferred_shot_video'](self.prev), latest)
            self.assertAlmostEqual(center,5-1/24)
            return dict(batch='batch',source_shot_id=1,source_hash='latest-hash',candidates=[dict(index=0,time=center)])
        with patch('backend.services.continuation.make_candidates', side_effect=candidates), patch('backend.services.continuation.load_candidate', return_value=(frame,{},{})):
            resolved = resolve_auto_input(frozen, self.ns)
        self.assertEqual(resolved['prompt'], 'Saved prompt')
        self.assertEqual(Path(resolved['first_frame_path']).read_bytes(), b'frame')
        self.assertEqual(json.loads(resolved['continuation_selection'])['mode'], 'auto')
        payload=json.loads(Path(resolved['queued_input_path']).read_text(encoding='utf-8'))
        self.assertIn('first_frame_path',payload['hashes'])
        self.assertEqual(len(updates),1)

    def test_manual_frame_stays_manual(self):
        frame=self.folder/'manual.png';frame.write_bytes(b'manual')
        self.shot['first_frame_path']=str(frame)
        frozen=self.frozen()
        self.assertNotIn('auto_source_shot_id',frozen)
        self.assertEqual(Path(frozen['first_frame_path']).read_bytes(),b'manual')

    def test_auto_regeneration_defers_old_frame(self):
        self.shot.update(first_frame_path=str(self.source),continuation_selection='{"mode":"auto"}')
        frozen=self.frozen()
        self.assertEqual(frozen['first_frame_path'],'')
        self.assertEqual(frozen['auto_source_shot_id'],1)

    def test_queue_wait_does_not_block_source_behind_it(self):
        frozen = self.frozen()
        child = dict(self.shot, status='QUEUED', queued_input_path=frozen['queued_input_path'], current_node='')
        self.prev['status'] = 'QUEUED'
        shots = {1: self.prev, 2: child}
        async def scenario():
            queue = asyncio.Queue()
            await queue.put(2)
            await queue.put(1)
            done = asyncio.Event(); order = []
            async def run(shot_id):
                order.append(shot_id)
                shots[shot_id]['status'] = 'DONE'
                if shot_id == 2: done.set()
            async def broadcast(msg): pass
            from types import SimpleNamespace
            ns = dict(self.ns, asyncio=asyncio, Path=Path, json=json,
                auto_wait_reason=auto_wait_reason, job_queue=queue,
                rt=SimpleNamespace(current_scene_image_job_id=None, active_shot_task=None, current_shot_id=None),
                run_shot=run, get_shot=lambda shot_id: shots[shot_id],
                update_shot_state=lambda shot_id, **values: shots[shot_id].update(values),
                hub=SimpleNamespace(broadcast=broadcast))
            ns['namespace'] = lambda: ns  # replaces the merged helper namespace used by the worker
            source = Path(__file__).resolve().parents[2]/'backend/services/video.py'
            node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == 'queue_worker')
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),ns)
            worker = asyncio.create_task(ns['queue_worker']())
            try:
                await asyncio.wait_for(done.wait(), timeout=4)
                self.assertEqual(order,[1,2])
                self.assertIn('等待上一镜完成', child['current_node'])
            finally:
                worker.cancel()
                await asyncio.gather(worker,return_exceptions=True)
        asyncio.run(scenario())

    def test_auto_merge_does_not_trim_source(self):
        class Conn:
            def execute(self,*args):return self
            def fetchall(self):return [dict(continuation_selection=json.dumps(dict(source_shot_id=1,mode='auto',time=4.9)),completed_input_path='')]
            def close(self):pass
        self.assertEqual(trim_for_continuity(dict(id=1),self.source,{'db':Conn}),self.source)

if __name__=='__main__':unittest.main()
