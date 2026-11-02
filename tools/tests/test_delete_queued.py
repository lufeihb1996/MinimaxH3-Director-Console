"""Queued deletion tests with isolated SQLite and real queue removal."""
import ast
import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from fastapi import HTTPException

class DeleteQueuedTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.db'
        def db():
            conn=sqlite3.connect(self.path);conn.row_factory=sqlite3.Row;self.addCleanup(conn.close);return conn
        self.db=db
        with db() as conn:
            conn.execute('CREATE TABLE shots (id INTEGER PRIMARY KEY, scene_id INTEGER, status TEXT)')
            conn.executemany('INSERT INTO shots VALUES(?,1,?)',[(1,'QUEUED'),(2,'RUNNING'),(3,'QUEUED'),(4,'PREPARING')])
        def get_shot(i):
            conn=db();row=conn.execute('SELECT * FROM shots WHERE id=?',(i,)).fetchone();conn.close()
            if row is None:raise HTTPException(404,'missing')
            return dict(row)
        async def broadcast(msg):pass
        self.queue=asyncio.Queue()
        for i in [1,3,1]:self.queue.put_nowait(i)
        self.ns=dict(asyncio=asyncio,db=db,get_shot=get_shot,HTTPException=HTTPException,
                     rt=SimpleNamespace(current_shot_id=2),job_queue=self.queue,
                     hub=SimpleNamespace(broadcast=broadcast))
        root=Path(__file__).resolve().parents[2]
        for rel in ('backend/routers/shots.py','backend/core/tasks.py'):
            path=root/rel;nodes=[]
            for n in ast.parse(path.read_text(encoding='utf-8')).body:
                if isinstance(n,ast.AsyncFunctionDef) and n.name in {'shot_delete','_delete_shot_internal','_remove_from_queue'}:
                    n.decorator_list=[];nodes.append(n)
            exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),self.ns)

    async def test_delete_waiting_and_remove_duplicates_keep_other_jobs(self):
        result=await self.ns['shot_delete'](1)
        self.assertEqual(result['removed_from_queue'],2)
        with self.db() as conn:self.assertIsNone(conn.execute('SELECT * FROM shots WHERE id=1').fetchone())
        self.assertEqual(self.queue.get_nowait(),3);self.queue.task_done()
        await asyncio.wait_for(self.queue.join(),1)
        self.assertEqual(self.ns['rt'].current_shot_id,2)

    async def test_running_and_preparing_stay_protected(self):
        for i in (2,4):
            with self.assertRaises(HTTPException) as ctx:await self.ns['shot_delete'](i)
            self.assertEqual(ctx.exception.status_code,409)
        self.assertEqual(self.queue.qsize(),3)

    async def test_popped_waiting_job_can_be_deleted(self):
        self.queue.get_nowait();self.queue.task_done()
        result=await self.ns['shot_delete'](1)
        self.assertTrue(result['ok'])
        with self.assertRaises(HTTPException):self.ns['get_shot'](1)

if __name__=='__main__':unittest.main()
