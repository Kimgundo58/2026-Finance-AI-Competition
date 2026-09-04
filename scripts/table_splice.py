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

🔴 **파이프 마크다운 표를 쓴다. ASCII 박스표(┌─┬─┐) 는 안 쓴다.** `stage2_chunk.py`
가 박스표를 감지해 청킹·임베딩에서 뺀다(「표를 그대로 임베딩하면 벡터가 표
서식에 끌려간다」, `stage2_chunk.py:53` · `RE_박스표`). 우리는 정확히 반대
(표 내용을 인용 가능한 청크로 살린다)를 원하므로 그 감지망에 안 걸리는 형식을
쓴다 — CLAUDE.md 가 「별표 ASCII 박스표 변환은 3-3_파싱_규칙.md 가 기준」이라
적어 뒀지만 실측(2026-09-04) 그 문서엔 해당 절이 없다(낡은 참조 — 별도 보고).

🔴 **매칭 실패(그 라벨의 표가 `_tables.json` 에 없음)는 원문을 그대로 둔다.**
   이 패치는 «더 나은 걸 아는 경우에만 갈아끼운다» 다 — 모르면 손 안 댄다.
   그래서 이 문서에 없는 사업(예비창업 등)·L3 업로드(User doc_id 가 UUID 라 애초에
   `_tables.json` 에 없다)는 전부 조용히 통과한다.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TABLES_PATH = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

_캐시: list[dict] | None = None


def _표_전체() -> list[dict]:
    global _캐시
    if _캐시 is None:
        if not TABLES_PATH.exists():
            _캐시 = []
        else:
            _캐시 = json.loads(TABLES_PATH.read_text(encoding="utf-8")).get("tables", [])
    return _캐시


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
    `_tables.json` 에 표가 있는 라벨만 그 표로 갈아끼운다.

    `doc_id` 가 없으면(L3 업로드 등 `_tables.json` 대상이 아닌 경로) 손 안 대고
    그대로 돌려준다 — 이 함수는 «갈아끼울 근거가 있을 때만» 개입한다.
    """
    if not doc_id or not 붙임들:
        return 붙임들
    바뀜 = []
    for 라벨, 원문 in 붙임들:
        표본문 = 직렬화(doc_id, 라벨)
        바뀜.append((라벨, 표본문) if 표본문 else (라벨, 원문))
    return 바뀜
