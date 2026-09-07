#!/bin/bash
# RunPod 무인 기동 훅. 볼륨의 /workspace/pre_start.sh 사본을 템플릿 /start.sh 가 부른다.
# 팟 열 때:
#   --docker-args "bash -c 'cp /workspace/pre_start.sh /pre_start.sh && chmod +x /pre_start.sh && exec /start.sh'"
# /start.sh 는 이 스크립트의 리턴을 기다리므로 vLLM 은 배경으로 떼어 던지고 즉시 리턴한다.
# CRLF 면 set -euo pipefail 이 죽는다 — 볼륨에 올릴 때 sed -i 's/\r$//' 를 거친다.
# 로그: /workspace/logs/boot_<epoch>.log
setsid nohup bash -c 'bash /workspace/pod_setup.sh && bash /workspace/pod_serve.sh' \
  > /workspace/logs/boot_$(date +%s).log 2>&1 < /dev/null &
disown
