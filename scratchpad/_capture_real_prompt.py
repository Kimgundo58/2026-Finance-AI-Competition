# -*- coding: utf-8 -*-
"""dry=True 로 (4) 조립까지만 돌려 실제 B0~B6 프롬프트를 캡처한다.
LLM 호출 없음 (비용 0). orchestrate.py 는 수정하지 않는다 — 런타임 몽키패치만.
"""
import sys, json
sys.path.insert(0, "scripts")
import orchestrate  # noqa: E402
from _lib import db  # noqa: E402

captured = {}
_orig_조립 = orchestrate.조립


def _wrap(*a, **kw):
    result = _orig_조립(*a, **kw)
    captured["프롬프트"], captured["s맵"], captured["사슬"] = result
    return result


orchestrate.조립 = _wrap

with db.connect(autocommit=True) as conn:
    row = conn.execute(
        "SELECT gold_id, 질문, 사업명, 정답판정 FROM eval.golden_set "
        "WHERE verified=true ORDER BY gold_id LIMIT 1"
    ).fetchone()
gold_id, 질문, 사업명, 정답판정 = row
print(f"gold_id={gold_id} 사업명={사업명} 정답판정={정답판정}", file=sys.stderr)

r = orchestrate.판정(질문, 사업명=사업명, dry=True, 기록=False)

프롬프트 = captured.get("프롬프트", "")
print(f"프롬프트 길이: {len(프롬프트)}자", file=sys.stderr)
print(f"s맵 크기: {len(captured.get('s맵') or {})}", file=sys.stderr)

out = {"gold_id": gold_id, "질문": 질문, "사업명": 사업명, "정답판정": 정답판정,
       "프롬프트": 프롬프트, "s맵": captured.get("s맵")}
with open("scratchpad/_real_prompt_captured.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False)
print("저장: scratchpad/_real_prompt_captured.json", file=sys.stderr)
