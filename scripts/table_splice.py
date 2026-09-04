# -*- coding: utf-8 -*-
"""[참고N]/[붙임N] 표 섹션 본문을 `_tables.json`(Stage 0-T, 셀 단위 정확 추출)로 갈아끼운다.

**진단은 `scratchpad/Q5_참고3_진단_0904.md`.** 요약만 남긴다.

문제 — 이 파일이 생기기 전까지: [참고N] 표 섹션의 「조」 본문은 `pdftext.extract()`
원문 그대로였다. `pdftext.page_gutter()` 는 페이지당 거터 «하나» 로만 자르는데,
표는 3~4 컬럼(비목·세목·세세목·집행기준)이라 한 번 잘라도 컬럼이 안에 남는다 —
`pdfplumber` 가 y좌표로 줄을 묶으며 컬럼끼리 섞인다(참고3 실측: 「기\n준」·
「유\n의\n사\n항」). 이건 `pdftext.py` 의 버그가 아니다 — 그 함수는 애초에 2단
«프로즈» 를 풀게 설계됐지 다중 컬럼 표를 풀도록 설계된 적이 없다.

해법 — 같은 문서를 `scripts/extract_tables.py`(Stage 0-T, `pdfplumber.extract_tables
(strategy='lines')` — 텍스트가 아니라 **셀** 단위)가 이미 옳게 읽어 `_tables.json`
에 갖고 있다(4문서 전수 확인: 창업중심대학2025 37개·초격차10차 41개·TIPS3차 53개·
창업도약2025 31개). 여기서 하는 일은 그 «이미 검증된» 표를 라벨(`_attach_label()`
산출, 예: "참고3")로 매칭해 마크다운 표로 직렬화하고, 해당 조 본문을 통째로
바꾸는 것뿐이다 — pdftext 나 조 분해 로직은 안 건드린다.

🔴 **2026-09-04 정정(중앙 지시, ai-c5 W3 대조) — 「오염된 조만」 갈아끼운다.**
   처음엔 그 라벨의 표가 `_tables.json` 에 있으면 무조건 갈아끼웠는데, ai-c5 팀이
   참고2 인용 11건을 `corpus.doc_articles` 와 대조해 **전부 verbatim 일치**를 확인했다
   — 참고2 는 애초에 안 깨졌다(참고3 만 깨졌다). 무조건 교체하면 **멀쩡한 참고2 원문을
   마크다운 표로 바꿔 오히려 verbatim 인용을 깨뜨릴 뻔했다.** 그래서 이제
   **오염 신호(`RE_라벨오염`, 세로 한 글자 줄 3+회)가 실제로 있는 조만** 표로 갈아끼운다.
   오염이 없으면(참고2 처럼 정상) 원문을 그대로 둔다 — `table_splice` 의 원 철학
   («더 나은 걸 아는 경우에만 갈아끼운다»)과 정확히 같은 결이다.

🔴 **파이프 마크다운 표를 쓴다. ASCII 박스표(┌─┬─┐) 는 안 쓴다.** `stage2_chunk.py`
가 박스표를 감지해 청킹·임베딩에서 뺀다(「표를 그대로 임베딩하면 벡터가 표
서식에 끌려간다」, `stage2_chunk.py:53` · `RE_박스표`). 우리는 정확히 반대
(표 내용을 인용 가능한 청크로 살린다)를 원하므로 그 감지망에 안 걸리는 형식을
쓴다 — CLAUDE.md 가 「별표 ASCII 박스표 변환은 3-3_파싱_규칙.md 가 기준」이라
적어 뒀지만 실측(2026-09-04) 그 문서엔 해당 절이 없다(낡은 참조 — 별도 보고).

🔴 **매칭 실패(그 라벨의 표가 `_tables.json` 에 없음)나 오염 없음은 원문을 그대로
   둔다.** 모르면(또는 이미 멀쩡하면) 손 안 댄다 — L3 업로드(doc_id 가 UUID 라
   애초에 `_tables.json` 에 없다)도 이 규칙 하나로 조용히 통과한다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES_PATH = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

# 세로 한 글자 줄 — 표 좌측 라벨(비목명·「기\n준」·「유\n의\n사\n항」)이 본문 줄 사이에
# 조각조각 섞인 흔적. 중앙 스캔 쿼리와 같은 정규식(`\n[가-힣]\n`).
RE_라벨오염 = re.compile(r"\n[가-힣]\n")
# 🔴 2026-09-04 — 처음엔 3(중앙의 «문서 단위» 스캔 임계를 그대로 물려썼다). TIPS 실측으로
#    정정: 오염이 [별첨1]·[붙임3]·[붙임5] 세 라벨에 «한 건씩» 흩어져 있었다 — 임계 3 이면
#    셋 다 문턱을 못 넘어 하나도 안 고쳐진다. 여기는 «조 하나에 3번 몰려야 오염» 이 아니라
#    «조에 한 번이라도 있으면 그 조가 잘못 읽혔다는 신호» 다(정상 문장은 세로 한 글자
#    줄이 나올 이유가 없다) — 그래서 1로 낮춘다. 문서 단위 스캔(central 의 3+)은
#    "문서가 이 문제를 겪고 있나" 를 거르는 임계고, 이건 "이 조를 갈아끼울까" 를 정하는
#    임계다 — 같을 이유가 없다.
오염_임계 = 1

_캐시: list[dict] | None = None


def _표_전체() -> list[dict]:
    global _캐시
    if _캐시 is None:
        if not TABLES_PATH.exists():
            _캐시 = []
        else:
            _캐시 = json.loads(TABLES_PATH.read_text(encoding="utf-8")).get("tables", [])
    return _캐시


def 오염됐나(본문: str) -> bool:
    """이 조 본문에 세로라벨 오염 신호가 임계 이상 있는가."""
    return len(RE_라벨오염.findall(본문 or "")) >= 오염_임계


def _마크다운_표(행: list[list]) -> str:
    """셀 배열 → 파이프 마크다운 표. None·개행·`|` 는 셀 안에서 정리한다."""
    def 셀(v) -> str:
        return (v or "").replace("\n", " ").replace("|", "/").strip()

    if not 행:
        return ""
    정리 = [[셀(c) for c in r] for r in 행]
    열수 = max(len(r) for r in 정리)
    정리 = [r + [""] * (열수 - len(r)) for r in 정리]
    머리 = "| " + " | ".join(정리[0]) + " |"
    구분 = "| " + " | ".join(["---"] * 열수) + " |"
    본문줄 = ["| " + " | ".join(r) + " |" for r in 정리[1:]]
    return "\n".join([머리, 구분, *본문줄])


def 라벨의_표들(doc_id: str, 라벨: str) -> list[dict]:
    """이 문서·라벨(예: "참고3")에 속하는 표를 페이지 순으로. 없으면 빈 리스트.

    `doc_id` 는 `extract_tables.py` 와 같은 관용구(`Path.stem`)를 쓴다 —
    `stage0_run.py`/`stage0_ingest.py` 호출부가 그대로 넘긴다.
    """
    후보 = [t for t in _표_전체() if t.get("doc_id") == doc_id and t.get("섹션") == 라벨]
    return sorted(후보, key=lambda t: (t.get("페이지") or 0, t.get("페이지_끝") or 0))


def 직렬화(doc_id: str, 라벨: str) -> str | None:
    """`_tables.json` 에 이 라벨의 표가 있으면 마크다운으로 이어 붙여 돌려준다.

    없으면 `None` — 호출부는 이때 «원문을 그대로 둔다» (갈아끼우지 않는다).
    """
    표들 = 라벨의_표들(doc_id, 라벨)
    if not 표들:
        return None
    조각 = [_마크다운_표(t["행"]) for t in 표들 if t.get("행")]
    조각 = [c for c in 조각 if c]
    if not 조각:
        return None
    return "\n\n".join(조각)


def 붙임_교체(doc_id: str | None, 붙임들: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """`stage0_articles._cut_sections()` 가 낸 (라벨, 원문) 목록을 받아,
    **오염된 라벨만** `_tables.json` 표로 갈아끼운다.

    `doc_id` 가 없으면(L3 업로드 등 `_tables.json` 대상이 아닌 경로) 손 안 대고
    그대로 돌려준다. 오염이 없는 라벨(예: 참고2)은 표가 있어도 원문을 유지한다 —
    verbatim 인용이 이미 맞는 걸 갈아끼워 깨뜨리지 않기 위해서다(2026-09-04 정정).
    """
    if not doc_id or not 붙임들:
        return 붙임들
    바뀜 = []
    for 라벨, 원문 in 붙임들:
        if not 오염됐나(원문):
            바뀜.append((라벨, 원문))
            continue
        표본문 = 직렬화(doc_id, 라벨)
        바뀜.append((라벨, 표본문) if 표본문 else (라벨, 원문))
    return 바뀜
