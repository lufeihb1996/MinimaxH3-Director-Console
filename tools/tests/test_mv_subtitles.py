from pathlib import Path
import importlib.util,tempfile,json,subprocess,time
root=Path(__file__).parent
module_path=root/'backend/services/mv_subtitles.py'
if not module_path.exists(): module_path=root.parents[1]/'backend/services/mv_subtitles.py'
spec=importlib.util.spec_from_file_location('mv_subtitles',module_path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
with tempfile.TemporaryDirectory(prefix='subtitle_test_') as d:
 p=Path(d);timeline=p/'timeline.json';src=p/'source.mp4';out=p/'final.mp4'
 timeline.write_text(json.dumps({'duration':1.2,'lines':[{'start':.1,'end':.9,'text':'Hello rap 你好'},{'start':1,'end':2,'text':'End'}]}),encoding='utf-8')
 assert '00:00:01,200' in m.lyric_srt(timeline)
 subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=640x360:r=25:d=1.2','-f','lavfi','-i','sine=frequency=440:duration=1.2','-c:v','libx264','-c:a','aac','-shortest',str(src)],check=True)
 started=time.monotonic();m.burn_mv_subtitles(src,timeline,out)
 info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(out)]))
 assert any(x['codec_type']=='audio' for x in info['streams']);assert src.is_file();assert out.with_suffix('.srt').is_file()
 def frame(f):return subprocess.check_output(['ffmpeg','-v','error','-ss','0.5','-i',str(f),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
 assert frame(src)!=frame(out)
 print('PASS: actual subtitles, audio, SRT, original preserved; seconds',round(time.monotonic()-started,2))
 timeline.write_text('{"lines":[]}');old=out.read_bytes()
 try:m.burn_mv_subtitles(src,timeline,out);raise AssertionError('expected failure')
 except RuntimeError:pass
 assert out.read_bytes()==old
 print('PASS: invalid timeline preserves previous final output')
