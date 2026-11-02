import asyncio,io,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backend.services.audio_analysis import run_local_analysis
class LocalAnalysisTests(unittest.IsolatedAsyncioTestCase):
 async def test_progress_and_outputs_without_api_thread_blocking(self):
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp);python=home/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python');python.parent.mkdir(parents=True);python.touch()
   events=[]
   async def progress(p,msg):events.append((p,msg))
   class Process:
    returncode=0
    stdout=io.BytesIO(b'@@PROGRESS {"progress":35,"message":"ASR"}\nfinished\n')
    def wait(self):return 0
    def poll(self):return 0
   def spawn(args,**kwargs):
    output=Path(args[args.index('--output')+1])
    (output/'timeline.json').write_text(json.dumps({'lines':[{'text':'hello'}],'beats':[0.2]}))
    for name in ['vocals.wav','no_vocals.wav','lyrics.srt']:(output/name).write_bytes(b'data')
    self.assertEqual(kwargs['env']['ANIME_STUDIO_ANALYSIS_HOME'],str(home))
    return Process()
   with patch.dict(os.environ,{'ANIME_STUDIO_ANALYSIS_HOME':str(home)}),patch('backend.services.audio_analysis.subprocess.Popen',side_effect=spawn):
    result=await run_local_analysis(home/'source.wav','hello',home/'result',progress)
   self.assertEqual(events,[(35.0,'ASR')]);self.assertEqual(result['lines'][0]['text'],'hello')
 async def test_missing_environment_has_actionable_error(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'ANIME_STUDIO_ANALYSIS_HOME':tmp}):
   with self.assertRaisesRegex(RuntimeError,'环境不存在'):
    await run_local_analysis(Path(tmp)/'song.wav','hello',Path(tmp)/'result',None)
if __name__=='__main__':unittest.main()
