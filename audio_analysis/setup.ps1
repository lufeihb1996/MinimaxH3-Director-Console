$ErrorActionPreference = 'Stop'
$audioRoot = $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $audioRoot 'cache/uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $audioRoot 'python'
$env:TEMP = Join-Path $audioRoot 'tmp'
$env:TMP = $env:TEMP
$env:HF_HOME = Join-Path $audioRoot 'cache/huggingface'
$env:TORCH_HOME = Join-Path $audioRoot 'cache/torch'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
$audioPython = Join-Path $audioRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $audioPython)) {
 uv venv (Join-Path $audioRoot '.venv') --python 3.11 --managed-python
 if ($LASTEXITCODE) { throw 'Python environment creation failed' }
}
$readyMarker = Join-Path $audioRoot '.requirements-v1.ok'
if (!(Test-Path -LiteralPath $readyMarker)) {
 uv pip install --python $audioPython torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu126
 if ($LASTEXITCODE) { throw 'PyTorch installation failed' }
 uv pip install --python $audioPython -r (Join-Path $audioRoot 'requirements.txt')
 if ($LASTEXITCODE) { throw 'Audio dependencies installation failed' }
 & $audioPython -c "import torch,demucs,whisperx; print('audio imports OK')"
 if ($LASTEXITCODE) { throw 'Audio import test failed' }
 New-Item -ItemType File -Path $readyMarker -Force | Out-Null
}
& $audioPython (Join-Path $audioRoot 'download_models.py')
if ($LASTEXITCODE) { throw 'Model download failed' }
