#!/bin/bash
# vLLM 기동. `GPU Guideline.md` §2 (4)(5).
#
# 🔴 볼륨 venv 를 쓴다 (`pod_setup.sh` 가 만든 것). 시스템 python 을 쓰면
#    팟마다 재설치이고, 그러다 버전이 갈려 두 번 깨졌다.
#
# 🔴 `--enable-prefix-caching` 은 B0(시스템 지시)가 고정이라 효과가 크다 —
#    `LLM.md` §3-7 이 블록 순서를 고정한 이유 중 하나다.
#
# 🔴 `--max-model-len` 을 16384 -> 24576 으로 올렸다 (2026-09-01 A · 드라이런 실측).
#    골든셋 77문항 프롬프트 길이: 중앙 8,640자 · **최장 25,111자**.
#    한국어 법령문은 Qwen3 토크나이저에서 대략 0.6~0.7 토큰/자라 최장 문항이
#    15,000~17,600 토큰이다 — 16384 로는 **가장 긴 문항이 잘린다.**
#    잘리면 B6(질문 + 출력 지시)가 맨 뒤라 **질문 자체가 날아간다.** 조용한 오답이 된다.
#    32B AWQ 는 가중치 ~20GB 라 48GB 카드에서 24k 컨텍스트 KV 캐시는 여유가 있다.
#    ⚠️ 그래도 판정 결과의 `finish_reason='length'` 를 반드시 확인할 것.
set -euo pipefail

VENV=/workspace/venv
export HF_HOME=/workspace/hf
export VLLM_USE_V1=1

[ -x "${VENV}/bin/vllm" ] || { echo "🔴 venv 가 없다. 먼저 pod_setup.sh 를 돌려라"; exit 1; }

exec "${VENV}/bin/vllm" serve Qwen/Qwen3-32B-AWQ \
  --quantization awq_marlin \
  --max-model-len 24576 \
  --gpu-memory-utilization 0.92 \
  --enable-prefix-caching \
  --disable-log-requests \
  --port 8000 --host 0.0.0.0
