# -*- coding: utf-8 -*-
"""[참고N]/[붙임N] 표 섹션 본문을 `_tables.json`(pdfplumber 셀 단위 추출)로 갈아끼운다.

오염 신호가 있는 조만 표로 바꾸고, 정상인 조는 원문을 그대로 둔다. 표가 원문
구간을 놓치면 그 구간을 축자 그대로 이어 붙여 인용 손실을 막는다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from _lib import text_coverage

ROOT = Path(__file__).resolve().parent.parent
TABLES_PATH = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

# 세로 한 글자 줄 — 표 좌측 라벨(비목명·「기\n준」·「유\n의\n사\n항」)이 본문 줄 사이에
# 조각조각 섞인 흔적.
RE_라벨오염 = re.compile(r"\n[가-힣]\n")
# 조에 한 번이라도 있으면 그 조가 잘못 읽혔다는 신호로 본다(정상 문장엔 나올 이유가 없다).
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
    오염된 라벨만 `_tables.json` 표로 갈아끼운다.

    `doc_id` 가 없으면(L3 업로드 등 `_tables.json` 대상이 아닌 경로) 손 안 대고
    그대로 돌려준다. 오염이 없는 라벨은 표가 있어도 원문을 유지한다.
    """
    if not doc_id or not 붙임들:
        return 붙임들
    바뀜 = []
    for 라벨, 원문 in 붙임들:
        if not 오염됐나(원문):
            바뀜.append((라벨, 원문))
            continue
        표본문 = 직렬화(doc_id, 라벨)
        if 표본문:
            표본문 = _손실_보강(원문, 표본문)
        바뀜.append((라벨, 표본문) if 표본문 else (라벨, 원문))
    return 바뀜


def _손실_보강(원문: str, 표본문: str) -> str:
    """표본문이 원문 구간을 통째로 놓쳤으면 그 구간을 축자 그대로 이어 붙인다.

    연속 구간 축자 복구(`누락_구간`)를 쓴다 — 어절 다중집합 재조립은 이미 다른
    자리에서 소비된 흔한 어절이 필요한 자리에서 빠져 문자 단위로 어긋난다.
    """
    구간 = text_coverage.누락_구간(원문, 표본문)
    if not 구간:
        return 표본문
    return 표본문 + "\n\n" + "\n".join(구간)
