# -*- coding: utf-8 -*-
"""LLM 출력 JSON Schema 와 최종 응답 데이터클래스.

normalize_run(정규화 호출)과 orchestrate(판정 호출)가 여기서 스키마를 만들어 llm_qwen 의
response_format 에 넣고, llm_validate 가 인용·전제·최종응답 데이터클래스로 결과를 채운다.
공개: 판정_ENUM, 미충족시_ENUM, 비목_enum(), 체크코드_enum(사업명, *, conn), 판정_스키마(문장ID들, 체크코드), 할일다듬기_스키마(번호들),
      정규화_스키마(비목목록), 인용, 전제, 최종응답. 읽는 테이블: corpus.check_items.
비목 enum 은 2026_Finance_DATA_FOR_RAG/_비목_어휘집.json 을 실행 시점에 읽는다. 코드에 복사하지 않는다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from _lib import db

_log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
비목어휘집_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"

# 판정 3종(2026-11-11 사용자 결정: 옛 조건부·판단불가 → 확인 필요). enum 순서가 바뀌면 스키마 문자열이 바뀌어 출력이 달라질 수 있으므로 순서도 고정한다.
판정_ENUM: tuple[str, ...] = ("가능", "확인 필요", "불가")

# 전제가 깨졌을 때의 결론. 판정과 같은 3종이다.
미충족시_ENUM: tuple[str, ...] = ("가능", "확인 필요", "불가")

# 확인 필요를 풀 수 있는 쪽(2026-11-12 사용자 결정). 주관기관 = 규정 문장만으로 정해지지 않아 기관 해석 · 승인이 필요
확인대상_ENUM: tuple[str, ...] = ("사용자", "주관기관")

문장ID_PATTERN = r"^S\d{2,3}$"     # assemble_context 가 B1→B2→B3 통합 연번으로 부여한다


def 비목_enum(경로: Path | None = None) -> list[str]:
    """`_비목_어휘집.json` 의 비목_enum. 비목 허용 목록(enum)의 기준값이다."""
    p = 경로 or 비목어휘집_경로
    v = json.loads(p.read_text(encoding="utf-8"))
    대기 = v.get("enum_검수대기") or []
    if 대기:
        _log.warning("어휘집 enum_검수대기 %d종: 기준값 확정 전이다", len(대기))
    return list(v["비목_enum"])


def 체크코드_enum(사업명: str | None = None, *, conn=None) -> list[str]:
    """`corpus.check_items.code` 중 이 사업에 해당하는 것. 해야할일 code 의 허용 목록이다.

    `사업명 IS NULL` 은 전 사업 공통, 그 밖은 해당 사업 전용이다. 사업명을 주지 않으면 공통 항목만
    돌려준다. 다른 사업의 항목을 제안하는 것보다 항목이 모자란 쪽이 안전하다.
    code 는 재판정해도 사용자의 체크 진행상황을 유지하는 고정 식별자라 LLM 이 문구를 새로 쓰지 못하게
    이 목록으로 제한한다. `conn` 을 주지 않으면 커넥션을 새로 연다.
    """
    with db.borrow(conn) as c:
        return [r[0] for r in c.execute(
            'SELECT code FROM corpus.check_items '
            'WHERE "사업명" IS NULL OR "사업명" = %s ORDER BY code', [사업명]).fetchall()]


def 판정_스키마(문장ID들: list[str] | None = None,
             체크코드: list[str] | None = None, 조건ID들: list[str] | None = None) -> dict[str, Any]:
    """판정 호출의 출력 스키마.

    `문장ID들` 을 주면 `인용`·`전제.근거조항` 을 그 집합으로 제한한다. 디코딩 단계에서 막으면 검증에서
    버릴 인용이 줄어든다. 주지 않으면 문장 ID 패턴만 건다.
    `해야할일` 은 `체크코드`(check_items code) 중에서 code 만 고른다. 항목·설명은 llm_validate 가 DB 원문으로
    채운다. `체크코드` 가 비면 `해야할일` 을 빈 배열로 고정한다(maxItems 0). 빈 enum 은 strict 스키마에서
    거부될 수 있다.
    `조건ID들`(B4 조건 줄 번호 C1…)을 주면 `조건판단` 을 맨 앞에 더한다(2026-11-13 사용자): 번호마다 {조건, 종류, 근거, 이유, 판단}.
    코드가 검사하고(ontology_gate.조건판단_검사) 상한만 정한다(합치기). 주지 않으면 조건판단 칸이 없다.
    칸 순서는 근거 → 결론이다(2026-11-13 사용자 승인): 조건판단 → 전제 → 인용 → 판정 → 확인대상 → 요약 → 해야할일.
    strict 출력은 이 순서대로 생성되므로 결론이 앞서 쓴 근거를 따른다. 읽는 쪽은 모두 키로 읽는다.
    """
    문장ID_스키마 = ({"type": "string", "enum": list(문장ID들)} if 문장ID들
                 else {"type": "string", "pattern": 문장ID_PATTERN})
    속성: dict[str, Any] = {}
    if 조건ID들:
        속성["조건판단"] = _조건판단_스키마(조건ID들)
    속성["전제"] = {
        "type": "array", "maxItems": 10,
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["사실", "근거조항", "매핑", "미충족시"],
            "properties": {
                # 짧은 값은 대부분 문장 ID 나 단어 하나뿐인 무의미한 출력이라 최소 길이를 둔다
                "사실": {"type": "string", "minLength": 5},
                "근거조항": 문장ID_스키마,
                # 프로필 필드 경로의 목록. 계산식은 받지 않는다. 계산은 코드(룰)가 한다
                "매핑": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                "미충족시": {"type": "string", "enum": list(미충족시_ENUM)},
            },
        },
    }
    # 인용은 문장 ID 만 받는다. 원문은 LLM 이 쓰지 않고 llm_validate 가 DB 에서 치환한다
    속성["인용"] = {"type": "array", "minItems": 0, "maxItems": 20, "items": 문장ID_스키마}
    속성["판정"] = {"type": "string", "enum": list(판정_ENUM)}
    # 확인 필요를 누가 풀 수 있는지. 주관기관이면 문의 초안을 붙인다(orchestrate._확인대상_반영 → inquiry.근거부족).
    # 판정 값은 바꾸지 않는다. 화면에는 내지 않는다
    속성["확인대상"] = {"type": "string", "enum": list(확인대상_ENUM)}
    # 요약을 문장 ID 나열만으로 채우는 출력을 막으려고 최소 길이를 둔다(문안 규칙은 시스템 프롬프트(B0)에 있다)
    속성["요약"] = {"type": "string", "minLength": 20, "maxLength": 300}
    속성["해야할일"] = {
        "type": "array", "maxItems": 10 if 체크코드 else 0,
        "items": {
            "type": "object", "additionalProperties": False,
            "required": ["code"],
            "properties": {"code": ({"type": "string", "enum": list(체크코드)} if 체크코드
                                    else {"type": "string"})},
        },
    }
    return {"type": "object", "additionalProperties": False, "required": list(속성), "properties": 속성}


def _조건판단_스키마(조건ID들: list[str]) -> dict[str, Any]:
    """조건판단 칸. 한 줄 안에서도 근거 · 이유를 판단보다 먼저 쓴다(조건 → 종류 → 근거 → 이유 → 판단)."""
    from ontology_gate import 조건근거_ENUM, 조건종류_ENUM, 조건판단_ENUM
    return {
        "type": "array", "maxItems": len(조건ID들),
        "items": {"type": "object", "additionalProperties": False,
                  "required": ["조건", "종류", "근거", "이유", "판단"],
                  "properties": {"조건": {"type": "string", "enum": list(조건ID들)},
                                 "종류": {"type": "string", "enum": list(조건종류_ENUM)},
                                 "근거": {"type": "string", "enum": list(조건근거_ENUM)},
                                 "이유": {"type": "string", "maxLength": 200},
                                 "판단": {"type": "string", "enum": list(조건판단_ENUM)}}}}


def 할일다듬기_스키마(번호들: list[str]) -> dict[str, Any]:
    """할 일 다듬기 호출(orchestrate._할일다듬기_호출)의 출력. 번호(H1…)마다 이번 지출에 맞게 줄인 문장과 이유 한 줄. 코드가 다시 검사한다."""
    return {
        "type": "object", "additionalProperties": False, "required": ["할일다듬기"],
        "properties": {"할일다듬기": {
            "type": "array", "maxItems": len(번호들),
            "items": {"type": "object", "additionalProperties": False, "required": ["번호", "문장", "설명"],
                      "properties": {"번호": {"type": "string", "enum": list(번호들)},
                                     "문장": {"type": "string", "maxLength": 150}, "설명": {"type": "string", "maxLength": 100}}},
        }},
    }


def 정규화_스키마(비목목록: list[str] | None = None) -> dict[str, Any]:
    """정규화 호출의 출력 스키마. `비목후보.비목` 만 비목 enum 으로 제한한다.

    품목·용도는 자유 문자열이다. rule_lookup.비목후보_조회 가 item_alias 정확 조회와 벡터 유사도로 원문
    표기를 받아 처리하므로 원문 표기를 그대로 두는 편이 낫다.
    `비목목록` 을 주지 않으면 어휘집 파일의 enum 을 쓴다.
    """
    비목 = 비목목록 if 비목목록 is not None else 비목_enum()
    return {
        "type": "object", "additionalProperties": False,
        "required": ["품목", "금액", "금액_추정여부", "용도", "비목후보", "누락필드"],
        "properties": {
            "품목": {"type": "string", "minLength": 1, "maxLength": 60},
            # 금액이 없으면 되묻지 않고 null 로 둔다. 사용자가 확인 화면에서 채운다
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


def 질의변환_스키마(표시문들: list[str]) -> dict[str, Any]:
    """온톨로지 경로의 질의 변환 출력(docs/4_검색/4-4_검색_재설계.md). 분류는 고르지 않는다(③ item_classify, 재설계 11-11).
    `표시문들` 은 옛 호출과 맞추려고 받기만 한다."""
    수 = {"type": ["number", "null"], "minimum": 0}
    return {
        "type": "object", "additionalProperties": False,
        "required": ["품목", "금액", "금액_추정여부", "용도", "질의문", "수치", "거래처유형", "사용일자", "누락필드"],
        "properties": {
            "품목": {"type": "string", "minLength": 1, "maxLength": 60},
            "금액": 수,
            "금액_추정여부": {"type": "boolean"},
            "용도": {"type": "string", "maxLength": 200},
            "질의문": {"type": "string", "maxLength": 200},
            "수치": {"type": "object", "additionalProperties": False,
                   "required": ["인원", "일수", "박수", "시간", "계약총액", "부가세구분"],
                   "properties": {"인원": 수, "일수": 수, "박수": 수, "시간": 수, "계약총액": 수,
                                  "부가세구분": {"type": "string", "enum": ["포함", "공급가액", "모름"]}}},
            "거래처유형": {"type": ["string", "null"], "maxLength": 40},
            # 입력에 연월일이 다 적힌 지출 날짜만(5-14 D 예비 길). 코드가 날짜로 읽히는지 다시 검사하고, 화면 입력이 있으면 그것이 먼저다
            "사용일자": {"type": ["string", "null"], "maxLength": 10},
            "누락필드": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        },
    }


# 최종 응답. 필드마다 LLM 이 채우는지 코드가 채우는지를 주석으로 구분한다.
@dataclass
class 인용:
    """문장ID 는 LLM 이 고르고, 나머지는 llm_validate 가 문장맵과 DB 로 채운다."""
    문장ID: str                                 # LLM
    doc_id: Optional[str] = None                # 코드: 문장맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    항호: Optional[str] = None                  # 코드: 문장맵 값이 기준값
    원문: Optional[str] = None                  # 코드: DB 원문 치환. LLM 이 생성하지 않는다
    원문범위: Optional[str] = None              # 코드: '항' | '조전체' | '청크' 등
    version: Optional[str] = None               # 코드: documents.version
    extraction: Optional[str] = None            # 코드: 신뢰등급 산정 입력


@dataclass
class 전제:
    """`사실`·`매핑`·`미충족시` 는 LLM 이, 근거조항의 문서 정보(doc_id 이하)는 `인용` 과 같은 방식으로 코드가 채운다.

    `인용` 과 달리 DB 조회가 비어도 전제를 버리지 않는다. `사실` 자체는 유효한 정보다. 이때 doc_id 이하는
    None 으로 남고 화면은 값이 없는 필드를 표시하지 않는다.
    """
    사실: str                                   # LLM
    근거조항: Optional[str]                     # LLM (문장 ID). 없으면 검증에서 버린다
    매핑: list[str] = field(default_factory=list)   # LLM: 프로필 필드 경로 목록
    미충족시: str = "불가"                      # LLM
    미매핑: bool = False                        # 코드: 프로필 스키마에 없는 경로가 섞였는가
    doc_id: Optional[str] = None                # 코드: 문장맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    원문: Optional[str] = None                  # 코드: DB 원문 치환. LLM 이 생성하지 않는다
    원문범위: Optional[str] = None              # 코드


@dataclass
class 최종응답:
    """화면과 `tenant.decisions` 로 나가는 형태."""
    # LLM 이 채운 것(검증·하향 후)
    판정: str
    요약: str
    해야할일: list[dict[str, str]] = field(default_factory=list)
    인용목록: list[인용] = field(default_factory=list)
    전제목록: list[전제] = field(default_factory=list)
    # 코드가 채우는 것. LLM 이 스스로 주장한 값을 쓰지 않는다
    신뢰등급: Optional[Literal["A", "B"]] = None   # 인용 문서의 extraction 등 실제 속성으로 정한다
    규정버전: Optional[str] = None                 # 인용 문서의 documents.version
    참조관계: list[dict] = field(default_factory=list)   # refs 레코드 그대로(근거 상세 화면용, 현재 미연결)
    검증사유: list[str] = field(default_factory=list)    # 검증 단계의 감사 로그
    미매핑전제: list[str] = field(default_factory=list)  # 프로필 스키마에 없는 전제 매핑 경로

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
