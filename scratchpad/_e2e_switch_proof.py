# -*- coding: utf-8 -*-
"""SUDDOE_LLM=qwen 스위치를 «실제 orchestrate.판정()」 전체 경로로 증명한다.
①(정규화)·④(판정) 둘 다 llm_qwen 을 타는지 실측 — LLM 2회, 유료.
"""
import os
import sys
import json

sys.path.insert(0, "scripts")

KEY_PATH = sys.argv[1]
os.environ["DASHSCOPE_API_KEY"] = open(KEY_PATH, encoding="utf-8").read().strip()
os.environ["SUDDOE_LLM"] = "qwen"

from llm_qwen import 스위치_적용  # noqa: E402
backend = 스위치_적용()
print(f"백엔드: {backend}", file=sys.stderr)

import orchestrate  # noqa: E402
import normalize_run  # noqa: E402
import llm_qwen  # noqa: E402

assert normalize_run.llm_호출 is llm_qwen.llm_호출, "①이 qwen 이 아니다"
assert orchestrate.llm_호출 is llm_qwen.llm_호출, "④가 qwen 이 아니다"
print("스위치 identity 재확인: 통과", file=sys.stderr)

from _lib import db  # noqa: E402
with db.connect(autocommit=True) as conn:
    row = conn.execute(
        "SELECT gold_id, 질문, 사업명, 정답판정 FROM eval.golden_set WHERE gold_id=330"
    ).fetchone()
gold_id, 질문, 사업명, 정답판정 = row

r = orchestrate.판정(질문, 사업명=사업명, dry=False, 기록=False)
print(f"정답판정={정답판정} 모델출력판정={r.get('판정')} 모델={r.get('모델')}", file=sys.stderr)

out = {"gold_id": gold_id, "정답판정": 정답판정, "스위치_backend": backend, "판정결과": r}
with open("scratchpad/Q5_스위치on_실경로_통과.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2, default=str)
print("저장: scratchpad/Q5_스위치on_실경로_통과.json", file=sys.stderr)
