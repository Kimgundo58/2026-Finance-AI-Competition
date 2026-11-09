#!/usr/bin/env bash
# 실제 LLM 제품 경로 점검. 키 값이나 응답 본문은 출력·저장하지 않는다.
set -eu
set -a
. <(tr -d '\r' < .env.llm)
set +a
export SUDDOE_QWEN_MODEL='qwen/qwen3.7-plus'
export SUDDOE_QWEN_FALLBACK_MODEL='qwen/qwen3.8-flash'
export SUDDOE_ONTOLOGY_PROGRAMS='모두의 창업 프로젝트'
export SUDDOE_DSN='postgresql://postgres:devpw@127.0.0.1:5462/suddoe'
export PGOPTIONS='-c default_transaction_read_only=on'
export PYTHONIOENCODING=utf-8 HF_HUB_OFFLINE=1
python -I -X utf8 codex_handoff/run_loop_check.py scripts/agentic_live_check.py
