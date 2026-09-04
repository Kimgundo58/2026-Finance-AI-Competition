#!/bin/bash
# RunPod 완전 무인 훅 — 이 파일 자체는 실행되지 않는다. `/workspace/pre_start.sh`
# 로 볼륨에 복사된 사본을 `docker-args` 가 부른다(2026-09-04 실물 검증 완료).
#
# 🔴 배선 방법(팟 열 때):
#   --docker-args "bash -c 'cp /workspace/pre_start.sh /pre_start.sh && chmod +x /pre_start.sh && exec /start.sh'"
#   RunPod 템플릿 기본 CMD(`/start.sh`)가 nginx 뒤·SSH 셋업 전에 `/pre_start.sh` 가
#   있으면 자동으로 `bash` 로 실행해 주는 공식 훅이다(`execute_script`, 템플릿
#   `runpod-torch-v280` 실측). `docker-args` 로 CMD 를 통째로 갈아치우지 않고 이
#   훅만 쓰는 이유 — CMD 를 통째로 바꾸면 `/start.sh` 가 안 돌아 SSH·nginx·jupyter
#   가 전부 죽는다(2026-09-04 첫 시도 `3bawa89c20txjf` 가 그렇게 실패했다).
#
# 🔴 절대 블로킹하면 안 된다. `execute_script` 는 이 스크립트의 리턴을 **기다린다**
#   — `pod_serve.sh` 는 마지막 줄이 `exec vllm ...` 라 리턴하지 않는다. 그대로 두면
#   SSH·jupyter 가 영원히 안 뜬다. 그래서 `setsid nohup ... & disown` 으로 완전히
#   떼어 배경으로 던지고, 이 스크립트 자신은 즉시 리턴한다.
#
# 🔴 `pod_setup.sh` 는 멱등이다(venv 있으면 건너뜀) — 재기동마다 다시 돌아도
#   안전하고 빠르다(수 초). `pod_serve.sh` 는 `VLLM_CACHE_ROOT=/workspace/vllm_cache`
#   덕에 torch.compile 캐시를 볼륨에서 재사용한다 — 실측 109초(최초) → 29~31초
#   (재사용, 2회 재현). 캐시는 **팟이 아니라 볼륨·모델설정 단위**로 유효해서
#   이 볼륨에 붙는 어떤 팟이든 이득을 받는다.
#
# 🔴 CRLF 금지 — Windows 에서 옮기면 `set -euo pipefail` 이
#   `set: pipefail: invalid option name` 으로 죽는다(2026-09-04 첫 실측, 원인은
#   scp 가 그대로 옮긴 CRLF). 볼륨에 올릴 때 반드시:
#     sed -i 's/\r$//' /workspace/pre_start.sh && chmod +x /workspace/pre_start.sh
#
# 산출 로그: /workspace/logs/boot_<epoch>.log — 재기동마다 새 파일이라 「이번에도
# 자동으로 돌았나」를 파일 존재 자체로 확인할 수 있다(2026-09-04 2회 재기동 확증).
setsid nohup bash -c 'bash /workspace/pod_setup.sh && bash /workspace/pod_serve.sh' \
  > /workspace/logs/boot_$(date +%s).log 2>&1 < /dev/null &
disown
