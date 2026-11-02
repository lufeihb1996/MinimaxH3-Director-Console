import os,sys,json,time,gc,argparse,subprocess
from pathlib import Path
BASE=Path(__file__).resolve().parent
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
def run(audio,out,device,resume=False):
 started=time.time();out.mkdir(parents=True,exist_ok=True)
 if resume and (out/"transcript_raw.json").is_file() and (out/"vocals.wav").is_file():
  import whisperx
  samples=whisperx.load_audio(str(out/"vocals.wav"))
  result=json.loads((out/"transcript_raw.json").read_text(encoding="utf-8"))
  print("Resuming saved transcript alignment",flush=True)
 else:
  print("STEP 1/3: Demucs vocals separation",flush=True)
  from demucs.pretrained import get_model
  from demucs.apply import apply_model
  import torchaudio
  import numpy as np
  import soundfile as sf
  model=get_model("htdemucs");model.to(device)
  wav,sr=sf.read(str(audio),dtype="float32",always_2d=True)
  wav=torch.from_numpy(wav.T.copy())
  if wav.shape[0]==1:wav=wav.repeat(2,1)
  if wav.shape[0]>2:wav=wav[:2]
  if sr!=model.samplerate:wav=torchaudio.functional.resample(wav,sr,model.samplerate)
  reference=wav.mean(0);mean=reference.mean();std=reference.std().clamp_min(1e-8)
  normalized=(wav-mean)/std
  with torch.inference_mode():sources=apply_model(model,normalized[None],device=device,split=True,segment=5,overlap=0.1,shifts=0,progress=True,num_workers=0)[0].cpu()
  sources=sources*std+mean
  index=model.sources.index("vocals");vocals=sources[index];backing=sources.sum(0)-vocals
  sf.write(str(out/"vocals.wav"),vocals.T.numpy(),model.samplerate,subtype="PCM_16")
  sf.write(str(out/"no_vocals.wav"),backing.T.numpy(),model.samplerate,subtype="PCM_16")
  del model,sources,vocals,backing,wav,normalized;release()
  print("STEP 2/3: Whisper small recognition",flush=True)
  import whisperx
  model_path=(BASE/"whisper_model_path.txt").read_text(encoding="utf-8").strip()
  samples=whisperx.load_audio(str(out/"vocals.wav"))
  model=whisperx.load_model(model_path,device,compute_type="int8",vad_method="silero",asr_options={"beam_size":3})
  result=model.transcribe(samples,batch_size=1)
  del model;release()
  (out/"transcript_raw.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
  print("Detected language:",result["language"],flush=True)
 print("STEP 3/3: align words",flush=True)
 align,metadata=whisperx.load_align_model(language_code=result["language"],device="cpu")
 aligned=whisperx.align(result["segments"],align,metadata,samples,"cpu",return_char_alignments=False)
 del align;release()
 payload={"language":result["language"],"duration":len(samples)/16000,"segments":aligned["segments"],"elapsed_seconds":round(time.time()-started,2),"device":device,"model":"small","original_audio":str(audio)}
 (out/"timeline.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
 def stamp(t):
  n=round(t*1000);return f"{n//3600000:02}:{n//60000%60:02}:{n//1000%60:02},{n%1000:03}"
 srt=[]
 for i,seg in enumerate(aligned["segments"],1):srt.append(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n{seg['text'].strip()}\n")
 (out/"lyrics.srt").write_text("\n".join(srt),encoding="utf-8")
 print("DONE",str(out),"elapsed",payload["elapsed_seconds"],flush=True)
if __name__=="__main__":
 parser=argparse.ArgumentParser();parser.add_argument("audio");parser.add_argument("--output",default=str(BASE/"results/test"));parser.add_argument("--device",choices=["cpu","cuda"],default="cuda" if torch.cuda.is_available() else "cpu");parser.add_argument("--resume",action="store_true");args=parser.parse_args()
 run(Path(args.audio),Path(args.output),args.device,args.resume)
