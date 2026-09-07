#!/bin/bash
# vLLM 기동 (RunPod 팟). pod_setup.sh 가 만든 볼륨 venv 를 쓴다.
# --reasoning-parser qwen3: thinking 종료 후부터 guided_json 문법을 적용한다(없으면 첫 토큰부터 걸려 공백만 뱉는다).
#   vllm 0.9 미만이면 인자가 거부된다 — 그때는 REASONING_PARSER=deepseek_r1.
# --max-model-len 40960: 프롬프트 최장 약 31k 토큰 + 출력 3k. 32768 로 내리면 질문 블록(B6)이 잘린다.
set -euo pipefail

VENV=/workspace/venv
export HF_HOME=/workspace/hf
export VLLM_USE_V1=1
# torch.compile 캐시를 볼륨에 둬 재기동마다 재사용한다.
export VLLM_CACHE_ROOT=/workspace/vllm_cache

MAX_MODEL_LEN="${MAX_MODEL_LEN:-40960}"
REASONING_PARSER="${REASONING_PARSER-qwen3}"   # `-` 다(`:-` 면 빈 값으로 파서를 못 뺀다)

[ -x "${VENV}/bin/vllm" ] || { echo "venv 가 없다. 먼저 pod_setup.sh 를 돌려라"; exit 1; }

ARGS=(serve Qwen/Qwen3-32B-AWQ
  --quantization awq_marlin
  --max-model-len "${MAX_MODEL_LEN}"
  --gpu-memory-utilization 0.92
  --enable-prefix-caching
  --disable-log-requests
  --port 8000 --host 0.0.0.0)
if [ -n "${REASONING_PARSER}" ]; then ARGS+=(--reasoning-parser "${REASONING_PARSER}"); fi

echo "기동: max-model-len=${MAX_MODEL_LEN} · reasoning-parser=${REASONING_PARSER:-<없음>}"
exec "${VENV}/bin/vllm" "${ARGS[@]}"
