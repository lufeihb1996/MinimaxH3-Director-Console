import os,sys,json,time,gc,argparse,subprocess,re
from pathlib import Path
BASE=Path((os.environ.get("ANIME_STUDIO_ANALYSIS_HOME") or "").strip() or (Path(__file__).resolve().parents[1] / "audio_analysis"))
for key,value in {"HF_HOME":BASE/"cache/huggingface","HF_HUB_CACHE":BASE/"cache/huggingface/hub","TORCH_HOME":BASE/"cache/torch","NLTK_DATA":BASE/"cache/nltk","TEMP":BASE/"tmp","TMP":BASE/"tmp","MPLCONFIGDIR":BASE/"cache/matplotlib"}.items():
 os.environ[key]=str(value);Path(value).mkdir(parents=True,exist_ok=True)
os.environ["MPLBACKEND"]="Agg"
os.environ["HF_XET_HIGH_PERFORMANCE"]="1"
os.environ["HF_HUB_DISABLE_XET"]="1"
os.environ["OMP_NUM_THREADS"]="4"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"]="1"
if os.name=="nt":
 for path in [Path(sys.prefix)/"Lib/site-packages/torch/lib",Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.6/bin")]:
  if path.exists():os.environ["PATH"]=str(path)+os.pathsep+os.environ["PATH"]
import torch
# Demucs checkpoints carry their architecture and require trusted legacy loading.
_original_load=torch.load
def compatible_load(*args,**kwargs):
 kwargs.setdefault("weights_only",False);return _original_load(*args,**kwargs)
torch.load=compatible_load

def release():
 gc.collect()
 if torch.cuda.is_available():torch.cuda.empty_cache()
def progress(value,message):
 print("@@PROGRESS "+json.dumps({"progress":value,"message":message},ensure_ascii=False),flush=True)

def _detect_lang_from_lyrics(known_lines):
 txt="\n".join(known_lines)
 if re.search(r"[\u3040-\u30ff]",txt):return "ja"
 if re.search(r"[\u4e00-\u9fff]",txt):return "zh"
 return "en"

def _detect_speech_bounds(samples,sr=16000):
 import numpy as np
 total=len(samples)/float(sr)
 if len(samples)<sr//10:return 0.0,total
 frame=int(sr*0.02)
 n_frames=len(samples)//frame
 if n_frames<=0:return 0.0,total
 frames=samples[:n_frames*frame].reshape(n_frames,frame)
 rms=np.sqrt(np.mean(frames**2,axis=1))
 peak=float(np.max(rms)) if len(rms) else 0.0
 thr=max(0.005,min(0.02,peak*0.06))
 active=np.where(rms>=thr)[0]
 if len(active)==0:return 0.0,total
 st=max(0.0,round(float(active[0]*0.02)-0.08,4))
 en=min(total,round(float((active[-1]+1)*0.02)+0.12,4))
 return st,en

def run(audio,out,device,resume=False,lyrics_file=None,dry_vocals=False):
 started=time.time();out.mkdir(parents=True,exist_ok=True)
 known=[]
 if lyrics_file and Path(lyrics_file).is_file():
  known=[x.strip() for x in Path(lyrics_file).read_text(encoding="utf-8").splitlines() if x.strip() and not re.fullmatch(r"\[[^\]]+\]",x.strip())]
 import whisperx, numpy as np, soundfile as sf
 if resume and (out/"transcript_raw.json").is_file() and (out/"vocals.wav").is_file():
  samples=whisperx.load_audio(str(out/"vocals.wav"))
  result=json.loads((out/"transcript_raw.json").read_text(encoding="utf-8"))
  print("Resuming saved transcript alignment",flush=True)
 else:
  if dry_vocals:
   progress(15,"配音纯干声直通（跳过 Demucs 分离）")
   wav,sr=sf.read(str(audio),dtype="float32",always_2d=True)
   if wav.shape[1]==1:wav=np.repeat(wav,2,axis=1)
   elif wav.shape[1]>2:wav=wav[:,:2]
   sf.write(str(out/"vocals.wav"),wav,sr,subtype="PCM_16")
   sf.write(str(out/"no_vocals.wav"),np.zeros_like(wav),sr,subtype="PCM_16")
   del wav;release()
  else:
   progress(5,"本地 Demucs 分离人声与伴奏")
   from demucs.pretrained import get_model
   from demucs.apply import apply_model
   import torchaudio
   model=get_model("htdemucs");model.to(device)
   wav,sr=sf.read(str(audio),dtype="float32",always_2d=True)
   wav=torch.from_numpy(wav.T.copy())
   if wav.shape[0]==1:wav=wav.repeat(2,1)
   if wav.shape[0]>2:wav=wav[:2]
   if sr!=model.samplerate:wav=torchaudio.functional.resample(wav,sr,model.samplerate)
   reference=wav.mean(0);mean=reference.mean();std=reference.std().clamp_min(1e-8)
   normalized=(wav-mean)/std
   with torch.inference_mode():sources=apply_model(model,normalized[None],device=device,split=True,segment=7.8,overlap=0.1,shifts=0,progress=True,num_workers=0)[0].cpu()
   sources=sources*std+mean
   index=model.sources.index("vocals");vocals=sources[index];backing=sources.sum(0)-vocals
   sf.write(str(out/"vocals.wav"),vocals.T.numpy(),model.samplerate,subtype="PCM_16")
   sf.write(str(out/"no_vocals.wav"),backing.T.numpy(),model.samplerate,subtype="PCM_16")
   del model,sources,vocals,backing,wav,normalized;release()
  samples=whisperx.load_audio(str(out/"vocals.wav"))
  if known:
   progress(40,"已知台词/歌词直通（跳过 Whisper 全量转写）")
   lang=_detect_lang_from_lyrics(known)
   st_b,en_b=_detect_speech_bounds(samples,16000)
   result={"language":lang,"segments":[{"start":st_b,"end":en_b,"text":" ".join(known)}]}
   (out/"transcript_raw.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
   print("Fast-path known lyrics language:",lang,"bounds:",st_b,en_b,flush=True)
  else:
   progress(35,"本地 Whisper small 识别语言与歌词")
   model_path=(BASE/"whisper_model_path.txt").read_text(encoding="utf-8").strip()
   model=whisperx.load_model(model_path,device,compute_type="int8",vad_method="silero",asr_options={"beam_size":3})
   result=model.transcribe(samples,batch_size=4)
   del model;release()
   (out/"transcript_raw.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
   print("Detected language:",result["language"],flush=True)
 progress(60,"本地 Wav2Vec2 极速对齐原歌词")
 targets=[{"start":max(0.0,min(x["start"] for x in result["segments"])),"end":min(len(samples)/16000,max(x["end"] for x in result["segments"])+0.1),"text":" ".join(known)}] if known else result["segments"]
 align_dev=device if (device=="cuda" and torch.cuda.is_available()) else "cpu"
 try:
  align,metadata=whisperx.load_align_model(language_code=result["language"],device=align_dev)
  aligned=whisperx.align(targets,align,metadata,samples,align_dev,return_char_alignments=False)
 except Exception as e:
  print("CUDA align fallback to CPU:",e,flush=True)
  release()
  align,metadata=whisperx.load_align_model(language_code=result["language"],device="cpu")
  aligned=whisperx.align(targets,align,metadata,samples,"cpu",return_char_alignments=False)
 lines=[];warnings=[]
 if known:
  import unicodedata
  def _is_punct(s):
   return not s or all(unicodedata.category(c)[0] in ("P","Z","S") for c in str(s))
  words=aligned["segments"][0].get("words",[]);cursor=0
  total_dur=round(len(samples)/16000,4)
  n_w=len(words)
  for idx_w,w in enumerate(words):
   if _is_punct(w.get("word","")):continue
   if w.get("start") is None or w.get("end") is None:
    prev_end=0.0
    for p_i in range(idx_w-1,-1,-1):
     if words[p_i].get("end") is not None:prev_end=float(words[p_i]["end"]);break
    next_start=total_dur
    gap_cnt=1
    for n_i in range(idx_w+1,n_w):
     if words[n_i].get("start") is not None:next_start=float(words[n_i]["start"]);break
     if not _is_punct(words[n_i].get("word","")):gap_cnt+=1
    step=max(0.02,(max(prev_end,next_start)-prev_end)/max(1,gap_cnt))
    w["start"]=round(prev_end,4)
    w["end"]=round(min(total_dur,prev_end+step),4)
  for text in known:
   count=len(text.split()) if result["language"] not in ["zh","ja"] else len([c for c in text if not c.isspace()])
   group=words[cursor:cursor+count];cursor+=count
   timed=[w for w in group if w.get("start") is not None and w.get("end") is not None and not _is_punct(w.get("word",""))]
   if not timed:
    timed=[w for w in group if w.get("start") is not None and w.get("end") is not None]
   if not timed:
    warnings.append("未对齐歌词："+text);continue
   st=float(timed[0]["start"]);en=float(min(len(samples)/16000,timed[-1]["end"]))
   last_st=float(timed[-1]["start"])
   if en-last_st>0.35:
    si=int((last_st+0.12)*16000);ei=min(len(samples),int(en*16000))
    for k in range(si,ei,320):
     win=samples[k:min(ei,k+800)]
     if len(win)>0 and float(np.sqrt(np.mean(win**2)))<0.012:
      en=round(k/16000,4);break
   clean_cnt=max(1,len([c for c in text if not _is_punct(c) and not c.isspace()]))
   lines.append({"index":len(lines)+1,"text":text,"start":round(st,4),"end":round(en,4),"aligned_ratio":round(len(timed)/clean_cnt,3),"words":group})
   if len(timed)<clean_cnt*.8:warnings.append("部分词未对齐："+text)
 else:
  lines=[{"index":i+1,"text":x["text"],"start":x["start"],"end":x["end"]} for i,x in enumerate(aligned["segments"])]
 if not lines:raise RuntimeError("没有可用的歌词对齐结果")

 del align;release()
 progress(85,"本地 librosa 分析节拍与起音")
 import librosa
 music,sample_rate=librosa.load(str(audio),sr=22050,mono=True)
 tempo,beat_frames=librosa.beat.beat_track(y=music,sr=sample_rate)
 onset_frames=librosa.onset.onset_detect(y=music,sr=sample_rate)
 duration=len(samples)/16000
 slots=[];cursor=0.0
 for line in lines:
  if line["start"]>cursor+.12:slots.append({"kind":"instrumental","text":"","start":cursor,"end":line["start"]})
  slots.append({"kind":"lyric","text":line["text"],"start":line["start"],"end":line["end"]});cursor=max(cursor,line["end"])
 if cursor<duration-.03:slots.append({"kind":"instrumental","text":"","start":cursor,"end":duration})
 payload={"language":result["language"],"duration":duration,"lines":lines,"slots":slots,"segments":aligned["segments"],"warnings":warnings,"beats":librosa.frames_to_time(beat_frames,sr=sample_rate).tolist(),"onsets":librosa.frames_to_time(onset_frames,sr=sample_rate).tolist(),"tempo":float(tempo.item() if hasattr(tempo,"item") else tempo),"elapsed_seconds":round(time.time()-started,2),"device":device,"model":"fast_align" if known else "small","original_audio":str(audio),"alignment_source":"provided_lyrics" if known else "asr"}

 (out/"timeline.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
 def stamp(t):
  n=round(t*1000);return f"{n//3600000:02}:{n//60000%60:02}:{n//1000%60:02},{n%1000:03}"
 srt=[]
 for i,seg in enumerate(lines,1):srt.append(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{seg['text'].strip()}\n")
 (out/"lyrics.srt").write_text("\n".join(srt),encoding="utf-8")
 print("DONE",str(out),"elapsed",payload["elapsed_seconds"],flush=True)
if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("audio");parser.add_argument("--output",default=str(BASE/"results/test"));parser.add_argument("--device",choices=["cpu","cuda"],default="cuda" if torch.cuda.is_available() else "cpu");parser.add_argument("--lyrics-file");parser.add_argument("--resume",action="store_true");parser.add_argument("--dry-vocals",action="store_true");args=parser.parse_args()
 run(Path(args.audio),Path(args.output),args.device,args.resume,args.lyrics_file,args.dry_vocals)
