import os
from pathlib import Path
BASE=Path(__file__).resolve().parent
os.environ["HF_HOME"]=str(BASE/"cache/huggingface")
os.environ["HF_HUB_CACHE"]=str(BASE/"cache/huggingface/hub")
os.environ["HF_XET_CACHE"]=str(BASE/"cache/huggingface/xet")
os.environ["HF_XET_HIGH_PERFORMANCE"]="1"
os.environ["TORCH_HOME"]=str(BASE/"cache/torch")
from concurrent.futures import ThreadPoolExecutor
import hashlib,httpx
from huggingface_hub import snapshot_download

def direct(url,hash_prefix):
 target=BASE/"cache/torch/hub/checkpoints"/url.rsplit("/",1)[-1]
 target.parent.mkdir(parents=True,exist_ok=True)
 if target.exists() and (not hash_prefix or hashlib.sha256(target.read_bytes()).hexdigest().startswith(hash_prefix)):
  print("CACHED",target.name,flush=True);return
 partial=target.with_suffix(target.suffix+".partial")
 with httpx.stream("GET",url,follow_redirects=True,timeout=180) as r:
  r.raise_for_status()
  with partial.open("wb") as f:
   for chunk in r.iter_bytes(1024*1024):f.write(chunk)
 if hash_prefix and not hashlib.sha256(partial.read_bytes()).hexdigest().startswith(hash_prefix):raise RuntimeError("Checksum failed: "+target.name)
 partial.replace(target);print("READY",target.name,target.stat().st_size,flush=True)
def whisper():
 result=snapshot_download("Systran/faster-whisper-small",allow_patterns=["model.bin","config.json","tokenizer.json","vocabulary.*","preprocessor_config.json"],max_workers=3)
 (BASE/"whisper_model_path.txt").write_text(result,encoding="utf-8")
 print("READY Whisper small",flush=True)
def tokenizer():
 base=BASE/"cache/nltk/tokenizers"
 if (base/"punkt_tab/english/collocations.tab").exists():return
 import io,zipfile
 response=httpx.get("https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip",timeout=60)
 response.raise_for_status()
 with zipfile.ZipFile(io.BytesIO(response.content)) as z:
  for name in z.namelist():
   if name.startswith("punkt_tab/english/") and not name.endswith("/"):
    target=(base/name).resolve()
    if not target.is_relative_to(base.resolve()):raise RuntimeError("Unexpected tokenizer path")
    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(z.read(name))
 print("READY English tokenizer",flush=True)
with ThreadPoolExecutor(max_workers=3) as pool:
 futures=[pool.submit(tokenizer),pool.submit(whisper),pool.submit(direct,"https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th","8726e21a"),pool.submit(direct,"https://download.pytorch.org/torchaudio/models/wav2vec2_fairseq_base_ls960_asr_ls960.pth","488fd4f16de84438")]
 for f in futures:f.result()
