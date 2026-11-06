#!/usr/bin/env bash
# Linux 服务器上下载 FastEmbed 的 ONNX 模型。
# 顺序：先试 hf-mirror，失败再切 ModelScope；模型与 Windows 脚本一致：
#   Qdrant/bge-small-zh-v1.5（包含 model_optimized.onnx）
#
# 用法：
#   bash scripts/fetch_embedding_model.sh
#   bash scripts/fetch_embedding_model.sh /opt/models/bge-small-zh-v1.5
set -euo pipefail

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    PY=python
fi
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "错误：找不到 python3/python" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/models/bge-small-zh-v1.5}"
mkdir -p "$DEST"

need_files() {
    for name in model_optimized.onnx config.json tokenizer.json \
        tokenizer_config.json special_tokens_map.json vocab.txt; do
        [ -f "$DEST/$name" ] || return 1
    done
    return 0
}

if need_files; then
    echo "模型已存在：$DEST"
    exit 0
fi

echo "第 1/2 步：通过 hf-mirror.com 下载 Qdrant/bge-small-zh-v1.5 ..."
"$PY" -m pip show huggingface_hub >/dev/null 2>&1 || \
    "$PY" -m pip install --user -U huggingface_hub
if HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_ETAG_TIMEOUT=15 HF_HUB_DOWNLOAD_TIMEOUT=30 \
    "$PY" - "$DEST" <<'PY'
from huggingface_hub import snapshot_download
import sys
kwargs = {"repo_id": "Qdrant/bge-small-zh-v1.5", "local_dir": sys.argv[1]}
try:
    snapshot_download(local_dir_use_symlinks=False, **kwargs)
except TypeError:
    snapshot_download(**kwargs)
PY
then
    if need_files; then
        echo "第 1 步成功：$DEST"
        exit 0
    fi
fi

echo "第 1 步未完成，改用 ModelScope ..."
"$PY" -m pip show modelscope >/dev/null 2>&1 || \
    "$PY" -m pip install --user -U modelscope
CACHE="$DEST.modelscope"
"$PY" - "$CACHE" "$DEST" <<'PY'
from modelscope import snapshot_download
import shutil, sys
p = snapshot_download('Qdrant/bge-small-zh-v1.5', cache_dir=sys.argv[1])
shutil.copytree(p, sys.argv[2], dirs_exist_ok=True)
PY

if need_files; then
    echo "模型已就绪：$DEST"
    exit 0
fi

echo "错误：没有拿到完整的 ONNX 模型。" >&2
echo "请把 EMBEDDING_MODEL_HOST_PATH 指向有效模型目录，或改用 EMBEDDING_PROVIDER=openai_compat/hash。" >&2
exit 1
