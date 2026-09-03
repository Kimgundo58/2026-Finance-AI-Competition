#!/bin/bash
# vLLM 기동. `GPU Guideline.md` §2 (4)(5).
#
# 🔴 볼륨 venv 를 쓴다 (`pod_setup.sh` 가 만든 것). 시스템 python 을 쓰면
#    팟마다 재설치이고, 그러다 버전이 갈려 두 번 깨졌다.
#
# 🔴 `--enable-prefix-caching` 은 B0(시스템 지시)가 고정이라 효과가 크다 —
#    `LLM.md` §3-7 이 블록 순서를 고정한 이유 중 하나다.
#
# 🔴 `--reasoning-parser qwen3` (2026-09-03 P4 추가).
#    Qwen3 는 thinking 이 기본이고 우리는 두 호출(①정규화·④조립) 다 `guided_json` 을 쓴다.
#    파서가 없으면 문법이 **첫 토큰부터** 걸려 `<think>` 를 못 열고, 모델이 공백만
#    뱉으며 토큰을 소진한다(ai-98 실측). 파서를 주면 **추론 종료 후 출력부터** 문법을
#    적용한다 — `docs/6_LLM/6-1_호출_설계.md:33`·`docs/부록/GPU_운영_전문.md:271` 이
#    이미 이 구성으로 적혀 있는데 **이 스크립트만 뒤처져 있었다.**
#    ⚠️ run 191(2026-09-02 기준선)은 **이 인자 없이** 떴다. 새 run 과 run 191 은
#       서빙 조건이 한 축 다르다 — 두 run 의 수치를 그냥 빼지 말 것.
#    ⚠️ `qwen3` 파서는 vllm 0.9 부터다. `pod_setup.sh` 의 cu124 분기(0.8.5.post1)에서는
#       인자가 거부돼 **부팅이 즉사**한다(조용히 넘어가지 않는다 — 그게 낫다).
#       그 경우에만 `REASONING_PARSER=deepseek_r1` 로 내려라(같은 `<think>` 규약).
#
# 🔴 `--max-model-len` 은 40960 이다 (2026-09-02 run 191 실측 · ai-43 승인).
#    이 파일은 24576 에 머물러 있었는데 **run 191 은 40960 으로 떴다** — 즉 지금까지
#    실제 기동은 이 스크립트를 안 거치고 손으로 했다는 뜻이다. 값을 여기로 끌어온다.
#    근거: 골든셋 93문항 프롬프트 최장 **44,926자**(P4 dry 실측, 중앙 15,221 · p90 34,247).
#    한국어 법령문은 Qwen3 토크나이저에서 대략 0.6~0.7 토큰/자 → 최장 ~31,400 토큰.
#    24576·32768 로는 가장 긴 문항이 잘리고, 잘리면 B6(질문 + 출력 지시)가 맨 뒤라
#    **질문 자체가 날아간다.** 조용한 오답이 된다. 32768 로 내리지 말 것.
#    실측 부팅 로그(A40 48GB): `Available KV cache memory: 21.28 GiB` ·
#    `GPU KV cache size: 87,136 tokens` · `Maximum concurrency: 2.13x` — 여유 있다.
#    ⚠️ 그래도 판정 결과의 `finish_reason='length'` 를 반드시 확인할 것.
set -euo pipefail

VENV=/workspace/venv
export HF_HOME=/workspace/hf
export VLLM_USE_V1=1

MAX_MODEL_LEN="${MAX_MODEL_LEN:-40960}"
REASONING_PARSER="${REASONING_PARSER-qwen3}"   # 🔴 `:-` 가 아니라 `-` 다. `:-` 면 REASONING_PARSER="" 로도 파서를 못 뺀다

[ -x "${VENV}/bin/vllm" ] || { echo "🔴 venv 가 없다. 먼저 pod_setup.sh 를 돌려라"; exit 1; }

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
