# -*- coding: utf-8 -*-
"""(1) 입력 정규화 슬롯 + 공용 vLLM 클라이언트.

`Agent.md` §1 (1) · `LLM.md` §1 (1)·§3-5. 자연어 한 줄 → 판정 파이프라인이 먹을 JSON.

## 이 파일이 vLLM 클라이언트를 겸하는 이유
`orchestrate.py` 와 `judge_run.py` 가 같은 HTTP 호출을 쓴다. 둘 중 한쪽에 두면
다른 쪽이 import 하면서 순환한다 (judge_run 은 --live 에서 orchestrate 를 부른다).
슬롯 ① 은 파이프라인 맨 앞이라 아무것도 import 하지 않는다 — 여기가 순환이 없는 자리다.

## 🔴 `guided_json` 은 최상위다
`extra_body` 로 감싸는 건 파이썬 OpenAI SDK 문법이다. HTTP 를 직접 치면 서버가 그 키를
모르고 **에러 없이 버린다** — 모델이 필드 이름을 제멋대로 짓는다 (2026-08-31 실측:
스키마가 {판정,요약,해야할일,인용,전제} 인데 {결과,근거,이유} 가 나왔다). 무음 실패다.

## 🔴 스키마가 `llm_schema.정규화_스키마()` 와 다르다 — 의도된 것이다
`llm_schema.정규화_스키마()` 는 `{비목, 금액, 집행예정일, 거래처, 불확실}` 인데
`0831_최종구현.md` §4 의 동결 인터페이스가 요구하는 건 **품목·용도**다:

    rule_lookup.비목확정(cur, 품목, 사업명)
    rule_lookup.금지적중(cur, 품목, 용도, 사업명, 비목)

품목·용도가 없으면 B 를 부를 수 없다. `LLM.md` §3-5 의 슬롯 ① 실물도
`{품목, 금액, 금액_추정여부, 용도, 비목후보[], 누락필드}` 다 — 그쪽이 정본이라 보고
여기서 그 모양으로 만든다. `llm_schema.py` 는 오늘 아무 세션의 소유도 아니라 손대지 않았다.
(둘 중 하나를 지우는 정리는 내일 몫 — `결과보고.md` 에 남긴다)

## dry 모드
LLM 없이 규칙만으로 정규화한다. **배관 검증 전용**이고 판정 품질 측정에 쓰지 않는다.
GPU 를 열기 전에 77문항 프롬프트 조립이 끝까지 도는지 보는 것이 목적이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --q "맥북 250만원 디자이너용"
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --q "..." --dry
    PYTHONIOENCODING=utf-8 python scripts/normalize_run.py --golden --dry   # 77문항 일괄
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_schema import 비목_enum  # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
VLLM = os.environ.get("VLLM_URL", "http://localhost:8000")
MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-32B-AWQ")
MODEL_1 = os.environ.get("VLLM_MODEL_1", "")     # 슬롯 ① 을 다른 모델로 돌릴 때만


class LLM실패(Exception):
    """vLLM 호출·파싱 실패. 부르는 쪽은 이걸 잡아 **판단불가**로 닫는다 (`Agent.md` §8)."""


def llm_호출(프롬프트: str, 스키마: dict | None, *,
            모델: str | None = None,
            온도: float = 0.0,
            최대토큰: int = 1500,
            타임아웃: int = 180,
            재시도: int = 1) -> tuple[Any, dict]:
    """vLLM OpenAI 호환 엔드포인트. (파싱된 출력, 메타).

    온도 0 고정이 기본이다 — 재현성이 이 도메인의 **요건**이지 편의가 아니다
    (`Agent.md` §6). 투표(N=3~5)를 켤 때만 부르는 쪽이 온도를 올린다.

    스키마 위반은 1회 재시도한다 (`Agent.md` §8 "(4) 스키마 위반 → 1회 재시도").
    guided_json 이 걸려 있으면 사실상 안 일어나지만, 서버가 그 키를 무시했을 때
    (위 주석의 무음 실패) 여기서 걸린다.
    """
    본문 = {"model": 모델 or MODEL,
            "messages": [{"role": "user", "content": 프롬프트}],
            "temperature": 온도,
            "max_tokens": 최대토큰}
    if 스키마 is not None:
        본문["guided_json"] = 스키마          # 🔴 최상위. extra_body 로 감싸지 않는다
    data = json.dumps(본문, ensure_ascii=False).encode()

    마지막 = None
    for 회차 in range(재시도 + 1):
        t = time.time()
        try:
            # 🔴 User-Agent 를 명시한다. RunPod 의 `*.proxy.runpod.net` 앞단 Cloudflare 가
            #    `Python-urllib/3.x` 를 봇으로 보고 **403 error code 1010** 으로 끊는다
            #    (2026-09-01 실측 — 같은 요청이 curl 로는 통과했다).
            #    로컬 vLLM 직결에서는 무해하고, 프록시 경유에서만 필요하다.
            req = urllib.request.Request(
                f"{VLLM}/v1/chat/completions", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "suddoe-judge/1.0"})
            with urllib.request.urlopen(req, timeout=타임아웃) as r:
                d = json.loads(r.read().decode())
            내용 = d["choices"][0]["message"]["content"]
            메타 = {"지연ms": int((time.time() - t) * 1000),
                   "토큰": d.get("usage", {}),
                   "종료이유": d["choices"][0].get("finish_reason"),
                   "모델": 본문["model"], "재시도": 회차}
            if 스키마 is None:
                return 내용, 메타
            try:
                return json.loads(내용), 메타
            except json.JSONDecodeError as e:
                마지막 = f"JSON 파싱 실패: {e} · 앞 200자 {내용[:200]!r}"
        except urllib.error.HTTPError as e:
            마지막 = f"HTTP {e.code}: {e.read()[:300]!r}"
        except Exception as e:                                  # 타임아웃·연결 끊김 포함
            마지막 = f"{type(e).__name__}: {str(e)[:200]}"
    raise LLM실패(마지막 or "알 수 없는 실패")


# ════════════════════════════════════════════════════════════════════════════
# 슬롯 ① 스키마
# ════════════════════════════════════════════════════════════════════════════
def 슬롯1_스키마(비목목록: list[str] | None = None) -> dict:
    """`LLM.md` §3-5 (1) 정규화 출력. `비목후보.비목` 만 어휘집 enum 으로 닫는다.

    품목·용도는 자유 문자열이다 — 닫을 수 있는 폐쇄 목록이 없고, B 의 `비목확정()` 이
    `item_alias` 정확조회 → 벡터 유사도로 받아내는 구조라 원문 표기가 오히려 재료다.
    """
    비목 = 비목목록 if 비목목록 is not None else 비목_enum()
    return {
        "type": "object", "additionalProperties": False,
        "required": ["품목", "금액", "금액_추정여부", "용도", "비목후보", "누락필드"],
        "properties": {
            "품목": {"type": "string", "minLength": 1, "maxLength": 60},
            # 금액이 없으면 되묻지 않고 null 로 둔다. 화면 4 에서 확인받는다 (§3-5)
            "금액": {"type": ["number", "null"], "minimum": 0},
            "금액_추정여부": {"type": "boolean"},
            "용도": {"type": "string", "maxLength": 200},
            "비목후보": {
                "type": "array", "minItems": 0, "maxItems": 3,
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["비목", "신뢰도"],
                          "properties": {"비목": {"type": "string", "enum": 비목},
                                         "신뢰도": {"type": "number",
                                                  "minimum": 0, "maximum": 1}}},
            },
            "누락필드": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        },
    }


# ── 프롬프트. B0 과 달리 캐시 프리픽스가 아니므로 비목 목록을 안에 넣어도 된다 ──
_지시 = """다음 문장은 창업지원금으로 무언가를 사거나 지출하려는 사람의 질문이다.
판정에 필요한 사실만 뽑아 JSON 으로 정규화하라.

규칙
1. 품목    무엇을 사는가. 상품명·서비스명 그대로. 문장을 요약하지 마라
2. 금액    숫자만(원 단위). 문장에 없으면 null 이고 금액_추정여부=false
           "약", "정도" 처럼 어림수면 그 값을 넣고 금액_추정여부=true
3. 용도    무엇에 쓰는가. 문장에 근거가 없으면 빈 문자열. 지어내지 마라
4. 비목후보 아래 목록 안에서만 고른다. 확신이 없으면 **비우거나 둘을 나란히** 둔다.
           억지로 하나를 고르지 마라 — 갈리면 코드가 두 경로를 모두 판정한다
5. 누락필드 판정에 필요한데 문장에 없는 것의 이름 (예: "금액", "용도", "집행시기")

판정을 하지 마라. 가능·불가를 여기서 말하지 않는다. 사실만 뽑는다.

비목 목록: {비목}

질문: {질문}"""


# ── dry 규칙 정규화 — 배관 검증 전용 ────────────────────────────────────────
_RE_금액 = re.compile(
    r"(?:(\d[\d,]*)\s*(억|천만|백만|만)?\s*원)|(?:(\d[\d,]*)\s*(억|천만|백만|만))")
_배수 = {"억": 100_000_000, "천만": 10_000_000, "백만": 1_000_000, "만": 10_000, None: 1}
_불용 = re.compile(r"(구매|구입|결제|지출|사도|써도|사용|가능|되나요|될까요|하려|합니다|"
                   r"인데|인가요|해도|되는지|괜찮|처리|집행)")


def 규칙_정규화(질문: str) -> dict:
    """LLM 없이 뽑는다. **`dry=True` 전용** — 판정 품질 측정에 쓰면 안 된다.

    이걸로 잰 숫자는 정규화 품질이 아니라 배관이 뚫렸는지만 말한다.
    """
    금액, 추정 = None, False
    m = _RE_금액.search(질문.replace(" ", ""))
    if m:
        수 = (m.group(1) or m.group(3) or "").replace(",", "")
        단위 = m.group(2) or m.group(4)
        if 수:
            금액 = int(수) * _배수.get(단위, 1)
            추정 = bool(re.search(r"(약|정도|가량|쯤)", 질문))
    # 품목: 첫 명사구 근사 — 조사·서술어를 떼고 앞 3어절
    머리 = re.split(r"[?？.\n]", 질문.strip())[0]
    어절 = [w for w in 머리.split() if not _불용.search(w)]
    품목 = " ".join(어절[:3])[:60] or 머리[:60]
    return {"품목": 품목, "금액": 금액, "금액_추정여부": 추정,
            "용도": "", "비목후보": [], "누락필드": [] if 금액 else ["금액"],
            "_출처": "규칙(dry)"}


def 정규화(질문: str, *, dry: bool = False, 비목목록: list[str] | None = None,
         타임아웃: int = 60) -> tuple[dict, dict]:
    """(정규화 JSON, 메타). 실패는 예외로 올린다 — 부르는 쪽이 판단불가로 닫는다."""
    if dry:
        return 규칙_정규화(질문), {"지연ms": 0, "모델": "규칙(dry)", "토큰": {}}
    스키마 = 슬롯1_스키마(비목목록)
    프롬프트 = _지시.format(비목=", ".join(스키마["properties"]["비목후보"]["items"]
                                       ["properties"]["비목"]["enum"]), 질문=질문)
    출력, 메타 = llm_호출(프롬프트, 스키마, 모델=MODEL_1 or None,
                       최대토큰=400, 타임아웃=타임아웃)
    # guided_json 이 걸려 있어도 서버가 무시했을 때를 대비해 최소 형태만 확인한다
    if not isinstance(출력, dict) or "품목" not in 출력:
        raise LLM실패(f"슬롯① 출력이 스키마 밖: {str(출력)[:200]}")
    출력.setdefault("비목후보", [])
    출력.setdefault("누락필드", [])
    출력["_출처"] = "llm"
    return 출력, 메타


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", help="질문 한 줄")
    ap.add_argument("--golden", action="store_true", help="골든셋 전량")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 규칙으로")
    a = ap.parse_args()

    if a.golden:
        import psycopg
        with psycopg.connect(DSN) as conn:
            rows = conn.execute("SELECT gold_id, 질문 FROM eval.golden_set "
                                "ORDER BY gold_id").fetchall()
        for gid, q in rows:
            out, _ = 정규화(q, dry=a.dry)
            print(f"{gid:3} {json.dumps(out, ensure_ascii=False)}")
        return
    if not a.q:
        ap.error("--q 또는 --golden")
    out, 메타 = 정규화(a.q, dry=a.dry)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n{메타}")


if __name__ == "__main__":
    main()
