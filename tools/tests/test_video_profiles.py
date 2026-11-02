"""Offline integration checks; extracts definitions to avoid app DB initialization."""
import ast
import asyncio
import copy
import json
import math
import re
import sys
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi import HTTPException
from pydantic import BaseModel
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _extract import extract
from backend.models.adapters.video_b15 import B15_MODEL, B15_FILENAME, apply_b15_profile
from backend.services.continuation import freeze_inputs
from backend.models.video import DEFAULT_VIDEO_MODEL, video_model, video_adapter, video_catalog, normalize_video_parameters, video_workflow_paths, select_final_video


def b15_workflow_paths(mode):
    return video_workflow_paths({'video_model': B15_MODEL, 'mode': mode})


def namespace():
    ns = dict(Any=Any, Path=Path, math=math, re=re, json=json, ComfyClient=object,
              HTTPException=HTTPException, BaseModel=BaseModel, Field=__import__('pydantic').Field,
              B15_MODEL=B15_MODEL, DEFAULT_VIDEO_MODEL=DEFAULT_VIDEO_MODEL,
              video_model=video_model, video_adapter=video_adapter, video_catalog=video_catalog,
              normalize_video_parameters=normalize_video_parameters,
              video_workflow_paths=video_workflow_paths, select_final_video=select_final_video,
              rt=SimpleNamespace(current_shot_id=None, active_shot_task=None))
    extract(ns, {
        'backend/core/media.py': {'dims_from_mp', 'frame_count'},
        'backend/core/comfy.py': {'set_workflow_value', 'workflow_node_class', 'workflow_node_inputs', 'workflow_node_label'},
        'backend/core/schemas.py': {'ShotIn', 'ShotPatch'},
        'backend/services/video.py': {'REALISM_TRIGGER_WORD', 'prepare_workflow', 'ensure_realism_trigger',
            'overall_progress_for_event', 'post_sampler_progress', 'monitor_prompt', 'video_progress_label', 'run_shot'},
        'backend/routers/shots.py': {'create_shot', 'shots_create', 'shots_patch'},
    })
    ns['read_json_file'] = lambda name: json.loads((ROOT/name).read_text(encoding='utf-8'))
    ns['get_settings'] = lambda: dict(fps='24')
    ns['compose_prompt'] = lambda shot: shot['prompt']
    return ns


class Client:
    def __init__(self): self.uploads = []
    async def upload_image(self, path):
        self.uploads.append(path)
        return path.name


class VideoProfilesTest(unittest.TestCase):
    def setUp(self):
        self.ns = namespace()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.ref = self.folder/'ref.png'
        self.ref.write_bytes(b'isolated-upload-fixture')
        self.ns['ensure_shot_dir'] = lambda shot: self.folder

    def shot(self, mode='CUT', model=B15_MODEL, steps=10):
        return dict(id=1, mode=mode, video_model=model, steps=steps, seed=123,
            megapixels=0.7, aspect_ratio='16:9', seconds=5, prompt='Test prompt',
            realism_lora_enabled=True, naughty_lora_enabled=True,
            ref1_path=str(self.ref), ref2_path=str(self.ref),
            first_frame_path=str(self.ref), continuation_selection='')

    def prepare(self, shot):
        client = Client()
        result = asyncio.run(self.ns['prepare_workflow'](shot, client))
        return result, client

    def assert_graph(self, workflow):
        for key, node in workflow.items():
            for name, value in node.get('inputs', {}).items():
                if isinstance(value, list) and len(value) == 2:
                    self.assertIn(str(value[0]), workflow, (key, name, value))
        # Walk every dependency of the final save node and detect cycles.
        seen, stack = set(), set()
        def visit(key):
            self.assertNotIn(key, stack)
            if key in seen: return
            stack.add(key)
            for value in workflow[key]['inputs'].values():
                if isinstance(value, list) and len(value) == 2: visit(str(value[0]))
            stack.remove(key)
            seen.add(key)
        visit('92')
        self.assertEqual(seen, set(workflow))

    def test_all_modes_two_pass_and_style_switches(self):
        for mode in ['CUT', 'CONTINUE', 'T2V']:
            with self.subTest(mode=mode):
                wf, client = self.prepare(self.shot(mode))
                self.assert_graph(wf)
                self.assertEqual(wf['127']['inputs']['unet_name'], B15_FILENAME)
                self.assertEqual(wf['128']['inputs']['clip_name'], 'qwen3vl_32b_minimax_h3_int8_convrot.safetensors')
                self.assertNotIn('942', wf)
                self.assertEqual(wf['124']['inputs']['steps'], 10)
                self.assertEqual(wf['b15_schedule']['class_type'], 'OpenH3RegenSigmas')
                self.assertEqual(wf['b15_schedule']['inputs']['steps'], 3)
                self.assertEqual(wf['b15_schedule']['inputs']['start_sigma'], 0.78)
                self.assertEqual(wf['b15_regen_lora']['class_type'], 'OpenH3RegenLora')
                self.assertEqual(wf['b15_sampler']['inputs']['sampler_name'], 'euler')
                self.assertEqual(wf['b15_upscale']['inputs']['mode.scale'], 2.0)
                self.assertEqual(wf['129']['inputs']['noise_seed'], wf['b15_noise']['inputs']['noise_seed'])
                self.assertEqual(wf['129']['inputs']['noise_seed'], 123)
                self.assertEqual((wf['131']['inputs']['width'], wf['131']['inputs']['height']), self.ns['dims_from_mp'](0.4, '16:9'))
                self.assertEqual(wf['943']['inputs']['strength_model'], 0.5)
                self.assertEqual(wf['910']['inputs']['strength_model'], 0.85)
                self.assertEqual(wf['122']['inputs']['samples'], ['b15_refine', 0])
                self.assertEqual(wf['121']['inputs']['samples'], ['b15_refine', 0])
                self.assertEqual(len(client.uploads) == 0, mode == 'T2V')
                if mode == 'CONTINUE':
                    self.assertEqual(wf['126']['inputs']['conditioning'], ['903', 0])
                    self.assertEqual(wf['903']['inputs']['image'], ['902', 0])
                elif mode == 'CUT':
                    self.assertIn('ref_images.ref_image_1', wf['131']['inputs'])

    def test_dedicated_templates_only_save_final_video(self):
        for mode in ['CUT', 'CONTINUE', 'T2V']:
            filename, bindings = b15_workflow_paths(mode)
            self.assertTrue((ROOT/bindings).is_file())
            workflow = self.ns['read_json_file'](filename)
            self.assert_graph(workflow)
            self.assertEqual([key for key, node in workflow.items() if node['class_type'] == 'SaveVideo'], ['92'])
            self.assertEqual(len([node for node in workflow.values() if node['class_type'] == 'CreateVideo']), 1)
            self.assertEqual(len([node for node in workflow.values() if node['class_type'] == 'VAEDecode']), 1)
            self.assertEqual(workflow['122']['inputs']['samples'], ['b15_refine', 0])
            self.assertEqual(workflow['121']['inputs']['samples'], ['b15_refine', 0])

    def test_continue_first_frame_only_for_both_models(self):
        for model, steps in [(B15_MODEL, 10), ('fused_h3', 4)]:
            shot = self.shot('CONTINUE', model, steps)
            shot.update(ref1_path='', ref2_path='', ref3_path='', ref4_path='')
            frozen_path = freeze_inputs(shot, self.ns)
            frozen = json.loads(Path(frozen_path).read_text(encoding='utf-8'))['shot']
            frozen['queued_input_path'] = frozen_path
            wf, client = self.prepare(frozen)
            self.assert_graph(wf)
            self.assertNotIn('901', wf)
            self.assertEqual(wf['131']['inputs'].get('ref_images.ref_image_0'), ['902', 0])
            self.assertEqual(len(client.uploads), 1)
            self.assertEqual(wf['903']['inputs']['image'], ['902', 0])

    def test_continue_optional_ref2_and_required_first_frame(self):
        shot = self.shot('CONTINUE')
        shot['ref1_path'] = ''
        wf, _ = self.prepare(shot)
        self.assert_graph(wf)
        self.assertIn('ref_images.ref_image_0', wf['131']['inputs'])
        shot['first_frame_path'] = ''
        self.ns['previous_shot'] = lambda shot: None
        with self.assertRaises(HTTPException): freeze_inputs(shot, self.ns)
        with self.assertRaisesRegex(RuntimeError, '首帧'): self.prepare(shot)
        shot = self.shot('CUT')
        shot['ref1_path'] = ''
        with self.assertRaisesRegex(RuntimeError, 'Ref1'): self.prepare(shot)

    def test_continue_refines_upscaled_result_with_second_keyframe_guide(self):
        for with_refs in [True, False]:
            shot = self.shot('CONTINUE')
            if not with_refs: shot.update(ref1_path='', ref2_path='')
            wf, _ = self.prepare(shot)
            self.assert_graph(wf)
            self.assertEqual(wf['903']['inputs']['latent'], ['131', 1])
            self.assertEqual(wf['125']['inputs']['guider'], ['126', 0])
            self.assertEqual(wf['126']['inputs']['conditioning'], ['903', 0])
            self.assertEqual(wf['b15_refine']['inputs']['latent_image'], ['b15_concat', 0])
            self.assertEqual(wf['b15_refine']['inputs']['guider'], ['b15_guider', 0])
            self.assertIn('b15_guide', wf)
            self.assertEqual(wf['b15_guide']['inputs']['latent'], ['b15_concat', 0])
            self.assertEqual(wf['b15_guide']['inputs']['image'], ['902', 0])
            self.assertEqual(wf['b15_guider']['inputs']['conditioning'], ['b15_guide', 0])
            self.assertEqual(sum(node['class_type']=='MiniMaxH3AddGuide' for node in wf.values()), 2)
            apply_b15_profile(wf)
            self.assertIn('b15_guide', wf)
            self.assertEqual(wf['b15_guider']['inputs']['conditioning'], ['b15_guide', 0])
            self.assert_graph(wf)

    def test_snapshot_survives_later_edits(self):
        shot = self.shot()
        frozen_path = freeze_inputs(shot, self.ns)
        data = json.loads(Path(frozen_path).read_text(encoding='utf-8'))
        shot.update(video_model='fused_h3', steps=20, realism_lora_enabled=False)
        frozen = data['shot']
        frozen['queued_input_path'] = frozen_path
        wf, _ = self.prepare(frozen)
        self.assertEqual(wf['127']['inputs']['unet_name'], B15_FILENAME)
        self.assertEqual(wf['943']['inputs']['strength_model'], 0.5)
        self.assertEqual(len([v for v in wf.values() if v['class_type']=='SamplerCustomAdvanced']), 2)
        self.assertTrue((Path(frozen_path).parent/'workflow.json').is_file())

    def test_missing_frozen_template_is_not_silently_replaced(self):
        shot = self.shot()
        frozen_path = freeze_inputs(shot, self.ns)
        frozen = json.loads(Path(frozen_path).read_text(encoding='utf-8'))['shot']
        frozen['queued_input_path'] = frozen_path
        (Path(frozen_path).parent/'template.json').unlink()
        with self.assertRaisesRegex(RuntimeError, '快照缺失'):
            self.prepare(frozen)

    def test_legacy_profiles_unchanged(self):
        for steps in [4, 8, 20]:
            wf, _ = self.prepare(self.shot('T2V', 'fused_h3', steps))
            self.assertNotIn('b15_refine', wf)
            self.assertEqual(wf['124']['inputs']['steps'], steps)
            self.assertEqual(wf['127']['inputs']['unet_name'], 'Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors')
            self.assertEqual('943' in wf, steps != 20)

    def test_style_off_and_b15_ignores_legacy_20_steps(self):
        shot = self.shot(steps=20)
        shot.update(realism_lora_enabled=False, naughty_lora_enabled=False)
        wf, _ = self.prepare(shot)
        self.assertEqual(wf['943']['inputs']['strength_model'], 0)
        self.assertEqual(wf['910']['inputs']['strength_model'], 0)
        self.assertEqual(wf['124']['inputs']['steps'], 10)

    def test_patch_switch_normalizes_model_specific_parameters(self):
        current = self.shot(model='fused_h3', steps=4)
        self.ns['get_shot'] = lambda sid: dict(current)
        self.ns['update_shot_state'] = lambda sid, **values: current.update(values)
        patch = self.ns['ShotPatch'](video_model='b15_49', steps=8, megapixels=2)
        asyncio.run(self.ns['shots_patch'](1, patch))
        self.assertEqual((current['video_model'], current['steps'], current['megapixels']), ('b15_49', 10, 0.4))
        asyncio.run(self.ns['shots_patch'](1, self.ns['ShotPatch'](video_model='fused_h3')))
        self.assertEqual(current['steps'], 4)
        self.assertEqual(self.ns['ShotIn']().video_model, B15_MODEL)
        with self.assertRaises(HTTPException):
            asyncio.run(self.ns['shots_patch'](1, self.ns['ShotPatch'](video_model='unsupported')))

    def test_create_default_uses_b15_without_migrating_existing_shots(self):
        db_path = self.folder/'isolated.db'
        connections = []
        self.addCleanup(lambda: [conn.close() for conn in connections])
        def connect():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            connections.append(conn)
            return conn
        with connect() as conn:
            conn.execute("CREATE TABLE shots(id INTEGER PRIMARY KEY, scene_id, shot_no, title, mode, prompt, dialogue, seconds, megapixels, aspect_ratio, steps, seed, video_model, enhance_mode, realism_lora_enabled DEFAULT 0, naughty_lora_enabled DEFAULT 0)")
        def get_shot(sid):
            with connect() as conn:
                return dict(conn.execute('SELECT * FROM shots WHERE id=?', (sid,)).fetchone())
        def update(sid, **values):
            with connect() as conn:
                conn.execute('UPDATE shots SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', [*values.values(), sid])
        self.ns.update(db=connect, get_shot=get_shot, update_shot_state=update, inherit_mother_refs=get_shot)
        legacy = asyncio.run(self.ns['shots_create'](1, self.ns['ShotIn'](video_model='fused_h3', steps=8)))
        created = asyncio.run(self.ns['shots_create'](1, self.ns['ShotIn'](realism_lora_enabled=True, naughty_lora_enabled=True)))
        self.assertEqual((created['video_model'], created['steps'], created['megapixels']), ('b15_49', 10, 0.4))
        self.assertEqual((created['realism_lora_enabled'], created['naughty_lora_enabled']), (1, 1))
        self.assertEqual(get_shot(legacy['id'])['video_model'], 'fused_h3')
        self.assertEqual(get_shot(legacy['id'])['steps'], 8)

    def test_progress_does_not_restart_at_second_sampler(self):
        wf, _ = self.prepare(self.shot('T2V'))
        progress = self.ns['overall_progress_for_event']
        self.assertLess(progress(wf, '125', 10, 10), progress(wf, 'b15_refine', 0, 5))

    def test_two_pass_stage_ranges(self):
        wf, _ = self.prepare(self.shot('T2V'))
        progress, stage = self.ns['overall_progress_for_event'], self.ns['post_sampler_progress']
        self.assertEqual([progress(wf, '125', i, 10) for i in [0, 5, 10]], [5, 30, 55])
        self.assertEqual([progress(wf, 'b15_refine', i, 5) for i in [0, 3, 5]], [65, 77, 85])
        self.assertEqual(stage(wf, 'b15_upscale'), 56)
        self.assertEqual(progress(wf, 'b15_upscale', 1, 2), 60)
        self.assertEqual(stage(wf, 'b15_concat'), 64)
        self.assertEqual(stage(wf, '122'), 90)
        self.assertEqual(stage(wf, '130'), 96)
        self.assertEqual(stage(wf, '92'), 98)

    def test_monitor_handles_node_less_events_without_progress_rollback(self):
        wf, _ = self.prepare(self.shot('T2V'))
        recorded, broadcasts = [], []
        events = iter([
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': '125'}},
            {'type': 'progress', 'data': {'value': 10, 'max': 10}},
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': 'b15_upscale'}},
            {'type': 'progress', 'data': {'value': 1, 'max': 2}},
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': 'b15_refine'}},
            {'type': 'progress', 'data': {'prompt_id': 'other', 'value': 5, 'max': 5}},
            {'type': 'progress', 'data': {'value': 3, 'max': 5}},
            {'type': 'progress', 'data': {'value': 1, 'max': 5}},
            {'type': 'progress', 'data': {'value': 5, 'max': 5}},
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': '122'}},
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': '92'}},
            {'type': 'executing', 'data': {'prompt_id': 'test', 'node': None}},
        ])
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def recv(self): return json.dumps(next(events))
        async def history(client, pid):
            return (bool(recorded and recorded[-1].get('progress') == 98), '')
        async def broadcast(event): broadcasts.append(event)
        self.ns.update(asyncio=asyncio, websockets=SimpleNamespace(connect=lambda *a, **k: Socket()),
            history_terminal_state=history, hub=SimpleNamespace(broadcast=broadcast),
            update_shot_state=lambda sid, **values: recorded.append(values))
        asyncio.run(self.ns['monitor_prompt'](SimpleNamespace(ws_base='ws://unused'), 'client', 'test', 1, wf))
        numbers = [r['progress'] for r in recorded if 'progress' in r]
        self.assertEqual(numbers, [5, 55, 56, 60, 65, 77, 85, 90, 98])
        self.assertTrue(any('细化' in e.get('node', '') for e in broadcasts))

    def test_sampler_labels_use_remote_step_counts(self):
        wf, _ = self.prepare(self.shot('T2V'))
        label = self.ns['video_progress_label']
        self.assertIn('4/10步', label(wf, '125', 4, 10))
        self.assertIn('2/5步', label(wf, 'b15_refine', 2, 5))
        self.assertNotIn('/0步', label(wf, '125', 0, 0))
        self.assertEqual(self.ns['ShotIn']().aspect_ratio, '16:9')
        self.assertEqual(normalize_video_parameters({'video_model': 'fused_h3'})['aspect_ratio'], '16:9')
        self.assertEqual(normalize_video_parameters({'title': 'keep ratio'}, self.shot()) .get('aspect_ratio'), None)

    def test_explicit_final_output_never_chooses_preview(self):
        preview = {'filename': 'preview.mp4'}
        final = {'filename': 'final.mp4'}
        outputs = {'preview': {'videos': [preview]}, '92': {'images': [final]}}
        self.assertEqual(select_final_video(outputs, '92'), final)
        with self.assertRaises(RuntimeError): select_final_video(outputs, 'missing')
        with self.assertRaises(RuntimeError): select_final_video({'92': {'images': [{'filename': 'frame.png'}]}}, '92')
        self.assertEqual(select_final_video({'legacy': {'gifs': [final]}}, None), final)

    def test_catalog_is_copy_and_model_parameters_have_one_validator(self):
        catalog = video_catalog()
        catalog['models'][0]['steps'].append(999)
        self.assertNotIn(999, video_model(B15_MODEL)['steps'])
        self.assertEqual(normalize_video_parameters({'video_model': ' B15_49 ', 'mode': 't2v', 'steps': 4})['steps'], 10)
        with self.assertRaises(ValueError): normalize_video_parameters({'video_model': 'missing'})
        with self.assertRaises(ValueError): normalize_video_parameters({'video_model': 'fused_h3', 'steps': 10})
        self.assertEqual(video_workflow_paths({'video_model': 'fused_h3', 'mode': 'CUT'}), ('workflows/video/cut_ref2va_api.json', 'workflows/video/cut_bindings.json'))
        with self.assertRaises(HTTPException): freeze_inputs(self.shot(model='missing'), self.ns)
        self.assertFalse((self.folder/'inputs').exists())

    def test_failed_regeneration_keeps_successful_video_and_error(self):
        old = self.folder/'old.mp4'
        old.write_bytes(b'previous successful media')
        current = dict(self.shot(), status='QUEUED', output_path=str(old), last_frame_path='old.png', generation_started_at=100)
        async def fail_prepare(shot, client): raise RuntimeError('missing model fixture')
        async def broadcast(event): pass
        self.ns.update(asyncio=asyncio, uuid=SimpleNamespace(uuid4=lambda: 'test'),
            get_shot=lambda sid: dict(current), prepare_workflow=fail_prepare,
            get_settings=lambda: {'comfy_url': 'http://unused'}, ComfyClient=lambda url: object(),
            update_shot_state=lambda sid, **values: current.update(values), hub=SimpleNamespace(broadcast=broadcast))
        asyncio.run(self.ns['run_shot'](1))
        self.assertEqual(current['status'], 'DONE')
        self.assertEqual(current['output_path'], str(old))
        self.assertIn('missing model fixture', current['error'])
        self.assertEqual(old.read_bytes(), b'previous successful media')

if __name__ == '__main__': unittest.main()
