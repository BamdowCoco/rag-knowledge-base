# Fetch the FastEmbed ONNX model for rag-api / embedder.
# Order: hf-mirror first, then ModelScope. No non-ASCII text on purpose:
# Windows PowerShell 5.1 may misread UTF-8 scripts without BOM.
param(
    [string]$Dest = (Join-Path $PSScriptRoot '..\models\bge-small-zh-v1.5'),
    [switch]$Force
)

$ErrorActionPreference = 'Continue'
$destPath = [System.IO.Path]::GetFullPath($Dest)
$modelFile = Join-Path $destPath 'model_optimized.onnx'

function Test-ModelReady {
    param([string]$Dir)
    $required = @(
        'model_optimized.onnx',
        'config.json',
        'tokenizer.json',
        'tokenizer_config.json',
        'special_tokens_map.json',
        'vocab.txt'
    )
    foreach ($name in $required) {
        if (-not (Test-Path (Join-Path $Dir $name))) {
            return $false
        }
    }
    return $true
}

function Write-NextSteps {
    param([string]$Dir)
    $display = $Dir.Replace('\', '/')
    Write-Host ''
    Write-Host 'Model is ready:'
    Write-Host "  $Dir"
    Write-Host ''
    Write-Host 'Put these in .env (the path is relative to the repo root when possible):'
    Write-Host "  EMBEDDING_MODEL_HOST_PATH=$display"
    Write-Host '  EMBEDDING_MODEL_PATH=/models/bge-small-zh-v1.5'
    Write-Host '  HF_HUB_OFFLINE=1'
    Write-Host '  EMBEDDING_PROVIDER=fastembed'
    Write-Host ''
    Write-Host 'Then run:'
    Write-Host '  docker compose up -d --build rag-api embedder'
}

New-Item -ItemType Directory -Force -Path $destPath | Out-Null

if ((Test-ModelReady $destPath) -and (-not $Force)) {
    Write-Host "Model already present: $destPath"
    Write-NextSteps $destPath
    exit 0
}

# ---------------------------------------------------------------
# Attempt 1: HuggingFace mirror (hf-mirror.com) via huggingface_hub.
# The Python API honors HF_ENDPOINT, so we do not depend on the CLI
# being on PATH; the same endpoint works for both.
# ---------------------------------------------------------------
Write-Host 'Attempt 1/2: download Qdrant/bge-small-zh-v1.5 via hf-mirror.com ...'
$env:HF_ENDPOINT = 'https://hf-mirror.com'
$env:HF_HUB_ETAG_TIMEOUT = '15'
$env:HF_HUB_DOWNLOAD_TIMEOUT = '30'

python -m pip show huggingface_hub 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'huggingface_hub is not installed; installing it with pip --user ...'
    python -m pip install --user -U huggingface_hub
}

$hfPython = @'
from huggingface_hub import snapshot_download
import sys
dest = sys.argv[1]
kwargs = {"repo_id": "Qdrant/bge-small-zh-v1.5", "local_dir": dest}
try:
    snapshot_download(local_dir_use_symlinks=False, **kwargs)
except TypeError:
    snapshot_download(**kwargs)
print("HF_DOWNLOAD_OK")
'@

try {
    # Suppress progress bars on stderr; the marker/completion is on stdout.
    & python -c $hfPython $destPath 2>$null | Out-Null
    if (Test-ModelReady $destPath) {
        Write-Host 'Attempt 1 succeeded (hf-mirror).'
        Write-NextSteps $destPath
        exit 0
    }
    Write-Host 'Attempt 1 finished but the model files are incomplete.'
} catch {
    Write-Host "Attempt 1 failed: $($_.Exception.Message)"
}

# ---------------------------------------------------------------
# Attempt 2: ModelScope mirror (www.modelscope.cn).
# Same ONNX repo; verified reachable from this machine.
# ---------------------------------------------------------------
Write-Host ''
Write-Host 'Attempt 2/2: download Qdrant/bge-small-zh-v1.5 via ModelScope ...'

python -m pip show modelscope 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'modelscope is not installed; installing it with pip --user ...'
    python -m pip install --user -U modelscope
}

$msCache = $destPath + '.modelscope'
$msPython = @'
from modelscope import snapshot_download
import shutil, sys
p = snapshot_download('Qdrant/bge-small-zh-v1.5', cache_dir=sys.argv[1])
shutil.copytree(p, sys.argv[2], dirs_exist_ok=True)
print("MODELSCOPE_DOWNLOAD_OK")
'@

try {
    # ModelScope prints progress bars on stderr; suppress them, copy happens in Python.
    & python -c $msPython $msCache $destPath 2>$null | Out-Null
} catch {
    Write-Host "Attempt 2 failed: $($_.Exception.Message)"
}

if (Test-ModelReady $destPath) {
    Write-Host 'Attempt 2 succeeded (ModelScope).'
    Write-NextSteps $destPath
    exit 0
}

Write-Host ''
Write-Host 'ERROR: could not get a complete ONNX model from hf-mirror or ModelScope.'
Write-Host 'Options:'
Write-Host '  1) Check network / proxy, then re-run this script.'
Write-Host '  2) Run the ModelScope download manually and pass its snapshot dir with -Dest.'
Write-Host '  3) Keep EMBEDDING_PROVIDER=hash (lexical fallback) or use openai_compat API.'
exit 1
