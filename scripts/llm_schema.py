# -*- coding: utf-8 -*-
"""LLM 출력 스키마 2겹. 기준 문서는 `LLM.md` §3-4.

[1겹] LLM 출력      — vLLM `guided_json` 강제 대상. LLM 이 채운다
[2겹] 최종 응답     — 검증·강등기가 변환·보강해 화면과 `tenant.decisions` 로

폐쇄 목록을 이 파일에 박지 않는다 — `비목` enum 은 `_비목_어휘집.json` 을 실행
시점에 읽는다. 여기 복사하면 용어 사전이 바뀌어도 옛 목록으로 강제된다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/llm_schema.py          # 스키마 실물 출력
    PYTHONIOENCODING=utf-8 python scripts/llm_schema.py --slot 1 # 정규화 호출 자리만
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# 훅이 PYTHONIOENCODING=utf-8 을 강제하므로 보통 이미 utf-8 이다. 조건 없이 다시
# 감싸면 import 시 앞의 래퍼가 GC 되며 버퍼가 닫힌다 — 조건부로만 감싼다.
if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
용어사전_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"
# 어휘집과 다른 파일이다 — 어휘집은 build_item_vocab.py 가 통째로 덮어쓴다
비목정의_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_정의.json"

# ════════════════════════════════════════════════════════════════════════════
# 폐쇄 목록
# ════════════════════════════════════════════════════════════════════════════
# 4-way. `LLM.md` §3-4. 순서까지 고정한다 — enum 순서가 바뀌면 프롬프트 캐시가 깨진다.
판정_ENUM: tuple[str, ...] = ("가능", "조건부", "불가", "판단불가")

# 전제가 깨졌을 때 어디로 떨어지는가. 판정과 같은 축이되 '판단불가' 는 전제의 결과가
# 될 수 없다 — 전제는 "이 사실이 아니면 이렇게 된다" 라서 결론이 있어야 한다.
미충족시_ENUM: tuple[str, ...] = ("가능", "조건부", "불가")

S번호_PATTERN = r"^S\d{2,3}$"      # 조립기가 B1→B2→B3 통합 연번으로 부여 (§3-7)


def 비목_enum(경로: Path | None = None) -> list[str]:
    """`_비목_어휘집.json` 의 guided_json_enum. 비목 폐쇄 목록의 유일한 기준 문서."""
    p = 경로 or 용어사전_경로
    v = json.loads(p.read_text(encoding="utf-8"))
    대기 = v.get("enum_검수대기") or []
    if 대기:
        print(f"⚠️ 어휘집 enum_검수대기 {len(대기)}종 — 정본 확정 전이다", file=sys.stderr)
    return list(v["guided_json_enum"])


def 비목_정의(경로: Path | None = None) -> dict[str, str]:
    """비목 이름 -> 정의 한 줄. 정규화 프롬프트의 설명 전용이다.

    `guided_json` enum 은 이 파일을 보지 않는다 — 폐쇄 목록의 기준 문서는 여전히
    `_비목_어휘집.json` 하나다. 별도 파일로 둔 이유는 어휘집이
    `build_item_vocab.py` 가 통째로 덮어써서 손으로 더한 키가 사라지기 때문이다.

    파일이 없으면 빈 dict 를 돌려준다 — 정의가 없어도 정규화가 죽지 않는다.
    """
    p = 경로 or 비목정의_경로
    if not p.exists():
        print(f"⚠️ 비목 정의 파일이 없다: {p} — 이름만으로 프롬프트를 만든다", file=sys.stderr)
        return {}
    return dict(json.loads(p.read_text(encoding="utf-8"))["정의"])


# ── [1겹] guided_json — ④-b 판정 조립 호출 자리 ──
def 체크코드_enum(dsn: str | None = None, 사업명: str | None = None) -> list[str]:
    """`corpus.check_items.code` 중 이 사업에 해당하는 것만. 해야할일 폐쇄 목록의 기준 문서.

    이 테이블의 존재 이유는 안정 식별자다 — 열어 두면 LLM 이 매번 다른 문구를
    뱉어 재판정 때 체크 진행상황이 이어지지 않는다.

    사업명으로 걸러야 한다. `사업명 IS NULL` 은 전 사업 공통, 그 밖은 해당 사업
    전용이다 — 안 거르면 다른 사업의 항목까지 후보에 섞인다.

    사업명을 안 주면 공통 항목만 준다. 좁은 쪽이 기본값이다 — 남의 사업 항목을
    제안하는 것보다 항목이 모자란 게 낫다.
    """
    from _lib import db
    with db.connect(dsn) as conn:
        return [r[0] for r in conn.execute(
            'SELECT code FROM corpus.check_items '
            'WHERE "사업명" IS NULL OR "사업명" = %s ORDER BY code', [사업명]).fetchall()]


def 판정_스키마(s번호들: list[str] | None = None,
             코드들: list[str] | None = None) -> dict[str, Any]:
    """§3-4 [1겹] 을 JSON Schema 로. vLLM `guided_json` 에 그대로 넣는다.

    s번호들 을 주면 `인용`·`근거조항` 을 그 집합 안으로 강제한다 — 디코딩 단계에서
    막으면 검증기가 폐기할 일이 줄어든다. 주지 않으면 패턴만 건다(조립기가 아직
    S번호를 못 정한 단계용).
    """
    s번호 = ({"type": "string", "enum": list(s번호들)} if s번호들
             else {"type": "string", "pattern": S번호_PATTERN})
    스키마: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["판정", "요약", "해야할일", "인용", "전제"],
        "properties": {
            "판정": {"type": "string", "enum": list(판정_ENUM)},
            # minLength 1 은 구멍이었다 — 요약을 S번호 인용만으로 채우는 경우가
            # 있어 문장을 강제한다(B0 에 문안 규칙).
            "요약": {"type": "string", "minLength": 20, "maxLength": 300},
            # `코드들` 이 있으면 LLM 은 code 하나만 고른다 — `인용`과 같은 원칙으로
            # code 는 안정 식별자이고 항목·설명은 코드가 채운다. `코드들` 이 없을
            # 때만 {항목, 설명} 을 LLM 이 직접 쓰는 폴백으로 남긴다.
            "해야할일": {
                "type": "array", "maxItems": 10,
                "items": ({
                    "type": "object", "additionalProperties": False,
                    "required": ["code"],
                    "properties": {"code": {"type": "string", "enum": list(코드들)}},
                } if 코드들 else {
                    "type": "object", "additionalProperties": False,
                    "required": ["항목", "설명"],
                    "properties": {"항목": {"type": "string", "minLength": 1},
                                   "설명": {"type": "string"}},
                }),
            },
            # 인용은 S번호만이다 (§3-6 인젝션 방어 2겹) — 원문을 LLM 이 쓰지 않는다
            "인용": {"type": "array", "minItems": 0, "maxItems": 20, "items": s번호},
            "전제": {
                "type": "array", "maxItems": 10,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["사실", "근거조항", "매핑", "미충족시"],
                    "properties": {
                        # minLength 5 — 짧은 값은 대부분 S번호 패턴이나 단일 단어
                        # 가비지였다. 문법 제약이라 완전한 방어는 아니다.
                        "사실": {"type": "string", "minLength": 5},
                        "근거조항": s번호,
                        # F필드 경로의 참조 목록. 수식 금지 — 계산은 룰에서 코드가 한다
                        "매핑": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "미충족시": {"type": "string", "enum": list(미충족시_ENUM)},
                    },
                },
            },
        },
    }
    return _순서_적용(스키마)


# ── A13 스키마 «필드 순서» 변형 — 문구가 아니라 디코딩 순서 축 ──
# strict json_schema / guided_json 은 스키마에 적힌 순서대로 토큰을 뱉는다. 기본
# 순서는 `판정` 이 맨 앞이라, 모델은 근거(`인용`·`전제`)를 쓰기 전에 판정부터
# 확정한다. S1 은 근거를 먼저 쓰게 해 이 순서 효과를 검증하는 변형이다.
# 채택 기준(assemble_context 의 A12)을 그대로 적용하고, A12(문구)와 같이 바꾸지
# 않는다 — 같이 바꾸면 무엇이 효과인지 못 가른다.
_순서_변형들: dict[str, str] = {
    "S0": "기준선 — 판정·요약·해야할일·인용·전제 (지금 운영 순서)",
    "S1": "근거우선 — 인용·전제 를 판정 «앞» 으로. 근거를 먼저 쓰게 해 thinking 을 대신한다",
}
_S1_순서 = ["인용", "전제", "판정", "요약", "해야할일"]


def _순서_적용(스키마: dict[str, Any]) -> dict[str, Any]:
    """`SUDDOE_SCHEMA_ORDER`(S0|S1, 기본 S0)에 맞춰 필드 순서만 바꾼다.

    기본값은 입력 객체를 그대로 돌려준다 — 값이 무엇이든 키 집합·제약은 안 바뀌고
    순서만 바뀐다.
    """
    # 환경변수 이름은 ASCII 여야 한다 — bash 는 비ASCII 이름을 식별자로 못 읽는다.
    순서 = os.environ.get("SUDDOE_SCHEMA_ORDER", "S0")
    if 순서 == "S0":
        return 스키마
    if 순서 != "S1":
        raise ValueError(f"SUDDOE_SCHEMA_ORDER={순서!r} — 'S0' 또는 'S1' 만 허용")
    props = 스키마["properties"]
    if set(_S1_순서) != set(props):
        raise ValueError(f"S1 순서표가 스키마와 안 맞는다: {sorted(props)}")
    새 = dict(스키마)
    새["properties"] = {k: props[k] for k in _S1_순서}
    새["required"] = list(_S1_순서)          # required 순서도 같이 맞춘다
    return 새


# ── [1겹] guided_json — ① 정규화 호출 자리 ──
def 정규화_스키마(비목목록: list[str] | None = None) -> dict[str, Any]:
    """자연어 → JSON. `비목` 이 용어 사전 enum 으로 닫히는 유일한 호출 자리이다.

    `비목` 은 인자로 받지 않으면 실행 시점에 파일을 읽는다. 하드코딩 금지.
    """
    비목 = 비목목록 if 비목목록 is not None else 비목_enum()
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["비목", "금액", "집행예정일", "거래처", "불확실"],
        "properties": {
            # 확신이 없으면 비목을 찍지 말고 불확실 에 담는다 — 기본값은 판단불가다
            "비목": {"type": ["string", "null"], "enum": 비목 + [None]},
            "금액": {"type": ["number", "null"], "minimum": 0},
            "집행예정일": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "거래처": {"type": ["string", "null"]},
            "불확실": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        },
    }


# ── [2겹] 최종 응답 — 누가 채우는지를 타입으로 가른다 ──
@dataclass
class 인용:
    """S번호는 LLM 이, 나머지는 전부 코드가 채운다 (§3-4 [2겹] 표).

    치환 주체는 검증·강등기다 — 조립기가 하면 s맵에 원문을 통째로 실어 넘겨야
    해서 계약이 무거워진다.
    """
    s번호: str                                  # LLM
    doc_id: Optional[str] = None                # 코드 — s맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    항호: Optional[str] = None                  # 코드 (s맵 값이 기준 문서)
    원문: Optional[str] = None                  # 코드 — S번호 → DB 원문 치환. 생성 금지
    원문범위: Optional[str] = None              # 코드 — '항' | '조전체' | '청크'. 아래 주석
    version: Optional[str] = None               # 코드 — documents.version
    extraction: Optional[str] = None            # 코드 — 신뢰등급 산정 입력


@dataclass
class 전제:
    """`사실`·`매핑`·`미충족시`는 LLM 이, 근거조항의 문서 해석(doc_id 이하)은
    `인용`과 같은 원칙으로 코드가 채운다.

    `인용`과 달리 DB 조회가 비어도 전제 자체를 폐기하지 않는다 — `사실` 은 그
    자체로 유효한 정보다. 이때는 doc_id 이하가 전부 None 으로 남고, 화면 쪽이
    "필드가 없으면 그 문장을 통째로 뺀다" 원칙으로 흡수한다.
    """
    사실: str                                   # LLM
    근거조항: Optional[str]                     # LLM (S번호). 없으면 (5)에서 폐기
    매핑: list[str] = field(default_factory=list)   # LLM — F필드 경로 참조 목록
    미충족시: str = "불가"                      # LLM
    미매핑: bool = False                        # 코드 — F 스키마에 없는 경로가 섞였나
    # ── 코드가 채운다 (S번호 → DB 치환. `인용`과 같은 패턴·같은 이유) ────
    doc_id: Optional[str] = None                # 코드 — s맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    원문: Optional[str] = None                  # 코드 — S번호 → DB 원문 치환. 생성 금지
    원문범위: Optional[str] = None              # 코드 — '항' | '조전체' | '청크'


@dataclass
class 최종응답:
    """화면과 `tenant.decisions` 로 나가는 형태."""
    # ── LLM 이 채운 것 (검증·강등 후) ──────────────────────────────────
    판정: str
    요약: str
    해야할일: list[dict[str, str]] = field(default_factory=list)
    인용목록: list[인용] = field(default_factory=list)
    전제목록: list[전제] = field(default_factory=list)
    # ── 코드가 채우는 것. LLM 자칭 금지 (§3-4) ─────────────────────────
    신뢰등급: Optional[Literal["A", "B"]] = None   # 인용 청크의 extraction 등 실제 속성으로
    버전스탬프: Optional[str] = None               # documents.version — "제14차, 2025.12.23 기준"
    참조사슬: list[dict] = field(default_factory=list)   # refs 레코드 그대로 (화면 7)
    강등사유: list[str] = field(default_factory=list)    # (5) 가 남기는 감사 로그
    미매핑전제: list[str] = field(default_factory=list)  # tenant.unmapped_premise 로깅 대상

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["1", "4"], default="4",
                    help="1=정규화 · 4=판정 조립 (기본)")
    ap.add_argument("--s", nargs="*", help="S번호 집합을 주면 인용을 그 안으로 닫는다")
    ap.add_argument("--codes", action="store_true",
                    help="check_items.code 로 해야할일을 닫는다 (DB 조회)")
    ap.add_argument("--program", help="사업명. 이걸 줘야 그 사업 전용 code 가 붙는다 "
                                      "(안 주면 전 사업 공통 code 만)")
    a = ap.parse_args()
    s = 정규화_스키마() if a.slot == "1" else 판정_스키마(
        a.s or None, 체크코드_enum(사업명=a.program) if a.codes else None)
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
