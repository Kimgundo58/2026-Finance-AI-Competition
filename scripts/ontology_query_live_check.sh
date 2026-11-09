#!/usr/bin/env bash
# 실제 모델 점검은 별도 승인 후에만 실행한다. 키·질의·응답 원문은 출력하지 않는다.
set -eu
if [ "${1:-}" != "--run" ]; then
  printf '%s\n' '실제 모델 호출은 별도 승인 후 --run을 지정한다.'
  exit 2
fi
set -a
. <(tr -d '\r' < .env.llm)
set +a
export SUDDOE_QWEN_MODEL='qwen/qwen3.7-plus'
export SUDDOE_QWEN_FALLBACK_MODEL='qwen/qwen3.8-flash'
export SUDDOE_ONTOLOGY_PROGRAMS='모두의 창업 프로젝트'
export SUDDOE_DSN='postgresql://postgres:devpw@127.0.0.1:5462/suddoe'
export PGOPTIONS='-c default_transaction_read_only=on'
export PYTHONIOENCODING=utf-8 HF_HUB_OFFLINE=1
python -I -X utf8 codex_handoff/run_loop_check.py scripts/ontology_query_live_check.py --run 2>/dev/null
