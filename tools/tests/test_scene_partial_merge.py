"""Scene partial merge regression checks; no production database or FFmpeg."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'backend' / 'services' / 'merge.py'

class SceneMergeTests(unittest.TestCase):
    def setup_merge(self, statuses):
        rows = [dict(id=i, shot_no=i, status=status, usable=usable) for i, (status, usable) in enumerate(statuses, 1)]
        class Cursor:
            def fetchone(self): return {'project_id': 7}
            def fetchall(self): return rows
        class Connection:
            def execute(self, *args): return Cursor()
            def close(self): pass
        calls = []
        ns = dict(Path=Path, hashlib=__import__('hashlib'), db=Connection, PROJECTS_DIR=Path('isolated'),
                  preferred_shot_video=lambda row: Path(f"{row['id']}.mp4") if row['usable'] else None,
                  timeline_ready_shot_video=lambda row: Path(f"{row['id']}.mp4"),
                  concat_videos=lambda paths, output: calls.append(paths) or output)
        tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
        nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in {'build_scene_master', '_scene_inputs_signature'}) or (isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'SCENE_CACHE_VERSION')]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), ns)
        return ns['build_scene_master'], calls

    def test_partial_order_and_counts(self):
        merge, calls = self.setup_merge([('DONE', True), ('RUNNING', True), ('DONE', True), ('DONE', False), ('FAILED', True)])
        summary = {}
        merge(1, completed_only=True, summary=summary)
        self.assertEqual(calls, [[Path('1.mp4'), Path('3.mp4')]])
        self.assertEqual(summary, {'merged_count': 2, 'skipped_count': 3})

    def test_no_ready_does_not_merge(self):
        merge, calls = self.setup_merge([('RUNNING', False), ('DONE', False)])
        with self.assertRaisesRegex(RuntimeError, '暂无已完成'):
            merge(1, completed_only=True)
        self.assertEqual(calls, [])

    def test_project_default_stays_strict(self):
        merge, calls = self.setup_merge([('DONE', True), ('RUNNING', False)])
        with self.assertRaisesRegex(RuntimeError, '#002'):
            merge(1)
        self.assertEqual(calls, [])

    def test_all_ready(self):
        merge, calls = self.setup_merge([('DONE', True), ('DONE', True)])
        summary = {}
        merge(1, completed_only=True, summary=summary)
        self.assertEqual(summary, {'merged_count': 2, 'skipped_count': 0})

if __name__ == '__main__':
    unittest.main()
