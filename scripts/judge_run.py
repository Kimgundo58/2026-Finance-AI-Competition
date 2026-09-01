# -*- coding: utf-8 -*-
"""판정 실행기 — 조립기 -> vLLM(guided_json) -> 검증기 -> jsonl.

세 조각을 잇는다. 각 조각은 다른 파일이 소유한다:
    scripts/assemble_context.py   B0~B6 조립 + S번호 부여        (중앙)
    scripts/llm_schema.py         guided_json 스키마             (LLM 스키마 세션)
    scripts/llm_validate.py       검증·강등                      (LLM 스키마 세션)
    scripts/score_judgment.py     채점                           (중앙)

🔴 **`guided_json` 에 S번호 enum 을 넣는 것이 이 파일의 요점이다.**
   디코딩 단계에서 막으면 환각 인용이 애초에 생기지 않는다 — 검증기가 폐기하는 것보다 낫다.
   검증기는 그래도 돌린다 (2겹 방어, `LLM.md` §3-4).

## 격리 모드 (D6)
`--isolated` 는 검색을 건너뛰고 골든셋 `정답근거` 의 조문 전문을 넣는다.
**판정층만의 성능**을 재는 용도다 — 검색 hit@5 52.9% 에 가려진 판정력을 분리한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/judge_run.py --isolated --limit 5
    PYTHONIOENCODING=utf-8 python scripts/judge_run.py --isolated --out 결과.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assemble_context import 조립, 격리_근거          # noqa: E402
from llm_schema import 판정_스키마                     # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
VLLM = os.environ.get("VLLM_URL", "http://localhost:8000")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B-AWQ")


def 호출(프롬프트: str, 스키마: dict, *, 온도: float = 0.0, 타임아웃: int = 180) -> tuple[dict, dict]:
    """vLLM OpenAI 호환 엔드포인트. (파싱된 출력, 메타)."""
    본문 = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": 프롬프트}],
        "temperature": 온도,          # 🔴 0 고정 — 재현성이 이 도메인의 요건이다
        # 🔴 3000 이다 (2026-09-01 격상). 1500 으로 돌린 실전 1차(run_id=189)에서
        #    판단불가 5건이 **전부 잘림**이었다 — 모델의 판단이 아니라 §8 실패 경로인데
        #    한 숫자로 세면 «근거 없으면 답하지 않는다» 가 작동한 것처럼 읽힌다.
        "max_tokens": 3000,
        # 🔴 vLLM 의 구조화 출력. **최상위에 넣는다.**
        #    `extra_body` 로 감싸는 건 파이썬 OpenAI SDK 문법이고, HTTP 를 직접 칠 때는
        #    서버가 그 키를 모른다 — 조용히 무시되고 모델이 필드 이름을 제멋대로 짓는다.
        #    2026-08-31 실측: extra_body 로 보냈더니 {"결과","근거","이유"} 가 나왔다
        #    (스키마는 {"판정","요약","해야할일","인용","전제"}). 에러가 아니라 무음 실패다.
        #    vLLM 0.11 은 guided_json · structured_outputs · response_format(json_schema)
        #    셋 다 받는다. 가장 널리 쓰이는 guided_json 을 쓴다.
        "guided_json": 스키마,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{VLLM}/v1/chat/completions", data=본문,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=타임아웃) as r:
        d = json.loads(r.read().decode())
    내용 = d["choices"][0]["message"]["content"]
    메타 = {"지연ms": int((time.time() - t) * 1000),
            "토큰": d.get("usage", {}),
            "종료이유": d["choices"][0].get("finish_reason")}
    return json.loads(내용), 메타


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--isolated", action="store_true", help="판정층 격리 (D6)")
    ap.add_argument("--limit", type=int, help="앞에서 N문항만")
    ap.add_argument("--gold-id", type=int, action="append", help="특정 문항만 (반복 가능)")
    ap.add_argument("--out", default="scripts/_work/_judge_result.jsonl")
    ap.add_argument("--no-validate", action="store_true", help="검증기를 건너뛴다 (원출력 확인용)")
    a = ap.parse_args()

    if not a.isolated:
        sys.exit("지금은 --isolated 만 지원한다. 실전 경로는 검색 연결(D7) 이후다.")

    검증 = None
    if not a.no_validate:
        from llm_validate import 검증 as _v
        검증 = _v

    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        q = "SELECT gold_id, 세트, 질문, 사업명, 정답판정 FROM eval.golden_set"
        if a.gold_id:
            cur.execute(q + " WHERE gold_id = ANY(%s) ORDER BY gold_id", (a.gold_id,))
        else:
            cur.execute(q + " ORDER BY gold_id")
        문항 = cur.fetchall()
        if a.limit:
            문항 = 문항[:a.limit]

        결과, 건너뜀 = [], []
        for gid, 세트, 질문, 사업, 정답 in 문항:
            근거 = 격리_근거(cur, gid)
            if not 근거:
                건너뜀.append(gid)          # 역추적 실패 7건 — 조용히 빼지 않고 보고한다
                continue
            프롬프트, s맵, _사슬 = 조립(cur, 질문, {"사업명": 사업}, 격리근거=근거)
            스키마 = 판정_스키마(s번호들=list(s맵))
            try:
                출력, 메타 = 호출(프롬프트, 스키마)
            except urllib.error.HTTPError as e:
                print(f"🔴 gold_id={gid} HTTP {e.code}: {e.read()[:300]!r}")
                continue
            except Exception as e:
                print(f"🔴 gold_id={gid} {type(e).__name__}: {str(e)[:200]}")
                continue

            사유 = []
            if 검증:
                출력, 사유 = 검증(출력, s맵, dsn=DSN)
            표시 = "✅" if 출력.get("판정") == 정답 else ("🔴" if 정답 in ("불가", "조건부")
                                                     and 출력.get("판정") == "가능" else "  ")
            print(f"{표시} gold_id={gid:3} [{세트}] 정답={정답:5} 예측={출력.get('판정'):5} "
                  f"인용={출력.get('인용')} {메타['지연ms']:5}ms" +
                  (f"  강등:{사유}" if 사유 else ""))
            결과.append({"gold_id": gid, **출력,
                         "s맵": {k: list(v) for k, v in s맵.items()},
                         "강등사유": 사유, "메타": 메타})

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with Path(a.out).open("w", encoding="utf-8") as f:
        for r in 결과:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n{len(결과)}건 -> {a.out}")
    if 건너뜀:
        print(f"⚠️ 정답 근거 조문을 못 찾아 건너뜀 {len(건너뜀)}건: {건너뜀}")
    print(f"채점:  PYTHONIOENCODING=utf-8 python scripts/score_judgment.py --in {a.out}")


if __name__ == "__main__":
    main()
