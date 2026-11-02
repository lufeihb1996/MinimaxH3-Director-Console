import json,sys,tempfile,unittest,sqlite3,subprocess,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backend.services.native_audio import wire_native_audio,freeze_song_audio,native_snapshot,finish_native_video
ROOT=Path(__file__).resolve().parents[2]
class NativeAudioTests(unittest.TestCase):
 def test_both_passes_lock_audio_and_preserve_guide(self):
  for mode in ['cut','continue','t2v']:
   with self.subTest(mode=mode):
    graph=json.loads((ROOT/f'workflows/video/b15_49_{mode}_api.json').read_text(encoding='utf-8'))
    old_guide=graph['b15_refine']['inputs']['guider'][:]
    old_latent=graph['125']['inputs']['latent_image'][:]
    wire_native_audio(graph,'voice.wav')
    self.assertEqual(graph['mv_zero']['inputs']['value'],0)
    self.assertEqual(graph['mv_initial']['inputs']['video_latent'],old_latent)
    self.assertEqual(graph['125']['inputs']['latent_image'],['mv_initial',0])
    self.assertEqual(graph['b15_concat']['inputs']['audio_latent'],['mv_lock',0])
    self.assertEqual(graph['b15_refine']['inputs']['guider'],old_guide)
    self.assertNotIn('audio',graph['130']['inputs'])
    self.assertNotIn('mv_mix',graph)
    self.assertNotIn('121',graph)
    for node in graph.values():
     for value in node['inputs'].values():
      if isinstance(value,list) and len(value)==2 and isinstance(value[0],str):self.assertIn(value[0],graph)
 def test_freeze_exact_audio_window_and_pad_only_end(self):
  if not shutil.which('ffmpeg'):self.skipTest('FFmpeg missing')
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);source=root/'song.wav';folder=root/'input';folder.mkdir()
   subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=4','-ac','2',str(source)],check=True)
   database=root/'db.sqlite';c=sqlite3.connect(database)
   c.executescript('CREATE TABLE scenes(id INTEGER,project_id INTEGER); CREATE TABLE mv_projects(project_id INTEGER,native_audio_enabled INTEGER,analysis_status TEXT,vocals_path TEXT,audio_path TEXT); INSERT INTO scenes VALUES(1,2);')
   c.execute('INSERT INTO mv_projects VALUES(2,1,?,?,?)',('DONE',str(source),str(source)));c.commit();c.close()
   def db():
    c=sqlite3.connect(database);c.row_factory=sqlite3.Row;return c
   def frames(seconds,fps):
    import math
    raw=round(seconds*fps);return raw+(5-raw%17)%17
   result=freeze_song_audio({'scene_id':1,'video_model':'b15_49','timeline_start':.7,'timeline_end':2.4},folder,{'db':db,'frame_count':frames,'get_settings':lambda:{'fps':'24'}})
   self.assertAlmostEqual(result['native_target_duration'],1.7)
   self.assertGreaterEqual(result['native_render_duration'],1.7)
   self.assertAlmostEqual(result['native_delivery_duration'],1.68)
   old=folder/'old.json';old.write_text(json.dumps({'shot':result}),encoding='utf-8')
   newer=folder/'new.json';newer.write_text(json.dumps({'shot':{}}),encoding='utf-8')
   self.assertIsNone(native_snapshot({'queued_input_path':str(newer),'completed_input_path':str(old)}))
   for field in ['native_vocals_path','native_mix_path']:
    probe=subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',result[field]],text=True)
    self.assertAlmostEqual(float(probe),result['native_render_duration'],places=3)
   record=folder/'input.json';record.write_text(json.dumps({'shot':result}),encoding='utf-8')
   self.assertEqual(native_snapshot({'queued_input_path':str(record)}),result)
 def test_local_mux_replaces_cloud_sound_and_preserves_duration(self):
  if not shutil.which('ffmpeg'):self.skipTest('FFmpeg missing')
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);video=root/'clip.mp4';mix=root/'mix.wav'
   subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=96x64:r=24:d=3','-f','lavfi','-i','sine=frequency=880:duration=3','-c:v','libx264','-c:a','aac','-shortest',str(video)],check=True)
   subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=3',str(mix)],check=True)
   finish_native_video(video,{'native_mix_path':str(mix),'native_delivery_duration':1.68})
   meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(video)],text=True))
   self.assertEqual([s['codec_type'] for s in meta['streams']],['video','audio'])
   self.assertAlmostEqual(float(meta['format']['duration']),1.68,places=2)
   # Verify source selection acoustically: a two-channel map bug could keep cloud 880Hz.
   import array,cmath,math
   raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(video),'-map','0:a','-ac','1','-ar','8000','-f','f32le','-'])
   samples=array.array('f',raw)[:8000]
   def magnitude(hz):return abs(sum(x*cmath.exp(-2j*math.pi*hz*i/8000) for i,x in enumerate(samples)))
   self.assertGreater(magnitude(440),magnitude(880)*10)
 def test_missing_mix_preserves_downloaded_video(self):
  with tempfile.TemporaryDirectory() as tmp:
   source=Path(tmp)/'clip.mp4';source.write_bytes(b'original download')
   with self.assertRaisesRegex(RuntimeError,'原歌片段缺失'):
    finish_native_video(source,{'native_mix_path':str(Path(tmp)/'missing.wav')})
   self.assertEqual(source.read_bytes(),b'original download')
if __name__=='__main__':unittest.main()
