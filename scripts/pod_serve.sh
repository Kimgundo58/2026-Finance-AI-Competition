#!/bin/bash
# vLLM 기동. `GPU Guideline.md` §2 (4)(5).
#
# 🔴 볼륨 venv 를 쓴다 (`pod_setup.sh` 가 만든 것). 시스템 python 을 쓰면
#    팟마다 재설치이고, 그러다 버전이 갈려 두 번 깨졌다.
#
# 🔴 `--enable-prefix-caching` 은 B0(시스템 지시)가 고정이라 효과가 크다 —
#    `LLM.md` §3-7 이 블록 순서를 고정한 이유 중 하나다.
set -euo pipefail

VENV=/workspace/venv
export HF_HOME=/workspace/hf
export VLLM_USE_V1=1

[ -x "${VENV}/bin/vllm" ] || { echo "🔴 venv 가 없다. 먼저 pod_setup.sh 를 돌려라"; exit 1; }

exec "${VENV}/bin/vllm" serve Qwen/Qwen3-32B-AWQ \
  --quantization awq_marlin \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.92 \
  --enable-prefix-caching \
  --disable-log-requests \
  --port 8000 --host 0.0.0.0
