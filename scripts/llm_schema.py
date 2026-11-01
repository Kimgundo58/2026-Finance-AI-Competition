# -*- coding: utf-8 -*-
"""LLM 출력 JSON Schema 와 최종 응답 데이터클래스.

normalize_run(정규화 호출)과 orchestrate(판정 호출)가 여기서 스키마를 만들어 llm_qwen 의
response_format 에 넣고, llm_validate 가 인용·전제·최종응답 데이터클래스로 결과를 채운다.
공개: 판정_ENUM, 미충족시_ENUM, 비목_enum(), 체크코드_enum(사업명, *, conn), 판정_스키마(s번호들, 코드들),
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
용어사전_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"

# 판정 4종. enum 순서가 바뀌면 스키마 문자열이 바뀌어 출력이 달라질 수 있으므로 순서도 고정한다.
판정_ENUM: tuple[str, ...] = ("가능", "조건부", "불가", "판단불가")

# 전제가 깨졌을 때의 결론. 전제는 "이 사실이 아니면 이렇게 된다" 를 말하므로 판단불가는 들어가지 않는다.
미충족시_ENUM: tuple[str, ...] = ("가능", "조건부", "불가")

S번호_PATTERN = r"^S\d{2,3}$"      # assemble_context 가 B1→B2→B3 통합 연번으로 부여한다


def 비목_enum(경로: Path | None = None) -> list[str]:
    """`_비목_어휘집.json` 의 guided_json_enum. 비목 허용 목록(enum)의 기준값이다."""
    p = 경로 or 용어사전_경로
    v = json.loads(p.read_text(encoding="utf-8"))
    대기 = v.get("enum_검수대기") or []
    if 대기:
        _log.warning("어휘집 enum_검수대기 %d종: 기준값 확정 전이다", len(대기))
    return list(v["guided_json_enum"])


def 체크코드_enum(사업명: str | None = None, *, conn=None) -> list[str]:
    """`corpus.check_items.code` 중 이 사업에 해당하는 것. 해야할일 code 의 허용 목록이다.

    `사업명 IS NULL` 은 전 사업 공통, 그 밖은 해당 사업 전용이다. 사업명을 주지 않으면 공통 항목만
    돌려준다. 다른 사업의 항목을 제안하는 것보다 항목이 모자란 쪽이 안전하다.
    code 는 재판정 사이에 사용자의 체크 진행상황을 잇는 안정 식별자라 LLM 이 문구를 새로 쓰지 못하게
    이 목록으로 제한한다. `conn` 을 주지 않으면 커넥션을 새로 연다.
    """
    with db.borrow(conn) as c:
        return [r[0] for r in c.execute(
            'SELECT code FROM corpus.check_items '
            'WHERE "사업명" IS NULL OR "사업명" = %s ORDER BY code', [사업명]).fetchall()]


def 판정_스키마(s번호들: list[str] | None = None,
             코드들: list[str] | None = None) -> dict[str, Any]:
    """판정 호출의 출력 스키마.

    `s번호들` 을 주면 `인용`·`전제.근거조항` 을 그 집합으로 제한한다. 디코딩 단계에서 막으면 검증에서
    버릴 인용이 줄어든다. 주지 않으면 S번호 패턴만 건다.
    `해야할일` 은 `코드들`(check_items code) 중에서 code 만 고른다. 항목·설명은 llm_validate 가 DB 원문으로
    채운다. `코드들` 이 비면 enum 이 비어 고를 수 있는 code 가 없다.
    """
    s번호 = ({"type": "string", "enum": list(s번호들)} if s번호들
             else {"type": "string", "pattern": S번호_PATTERN})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["판정", "요약", "해야할일", "인용", "전제"],
        "properties": {
            "판정": {"type": "string", "enum": list(판정_ENUM)},
            # 요약을 S번호 나열만으로 채우는 출력을 막으려고 최소 길이를 둔다(문안 규칙은 B0 에 있다)
            "요약": {"type": "string", "minLength": 20, "maxLength": 300},
            "해야할일": {
                "type": "array", "maxItems": 10,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["code"],
                    "properties": {"code": {"type": "string", "enum": list(코드들 or [])}},
                },
            },
            # 인용은 S번호만 받는다. 원문은 LLM 이 쓰지 않고 llm_validate 가 DB 에서 치환한다
            "인용": {"type": "array", "minItems": 0, "maxItems": 20, "items": s번호},
            "전제": {
                "type": "array", "maxItems": 10,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["사실", "근거조항", "매핑", "미충족시"],
                    "properties": {
                        # 짧은 값은 대부분 S번호나 단어 하나뿐인 무의미한 출력이라 최소 길이를 둔다
                        "사실": {"type": "string", "minLength": 5},
                        "근거조항": s번호,
                        # F 필드 경로의 목록. 계산식은 받지 않는다. 계산은 코드(룰)가 한다
                        "매핑": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "미충족시": {"type": "string", "enum": list(미충족시_ENUM)},
                    },
                },
            },
        },
    }


def 정규화_스키마(비목목록: list[str] | None = None) -> dict[str, Any]:
    """정규화 호출의 출력 스키마. `비목후보.비목` 만 비목 enum 으로 제한한다.

    품목·용도는 자유 문자열이다. rule_lookup.비목확정 이 item_alias 정확 조회와 벡터 유사도로 원문
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


# 최종 응답. 필드마다 LLM 이 채우는지 코드가 채우는지를 주석으로 구분한다.
@dataclass
class 인용:
    """S번호는 LLM 이 고르고, 나머지는 llm_validate 가 s맵과 DB 로 채운다."""
    s번호: str                                  # LLM
    doc_id: Optional[str] = None                # 코드: s맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    항호: Optional[str] = None                  # 코드: s맵 값이 기준값
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
    근거조항: Optional[str]                     # LLM (S번호). 없으면 검증에서 버린다
    매핑: list[str] = field(default_factory=list)   # LLM: F 필드 경로 목록
    미충족시: str = "불가"                      # LLM
    미매핑: bool = False                        # 코드: F 스키마에 없는 경로가 섞였는가
    doc_id: Optional[str] = None                # 코드: s맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    원문: Optional[str] = None                  # 코드: DB 원문 치환. LLM 이 생성하지 않는다
    원문범위: Optional[str] = None              # 코드


@dataclass
class 최종응답:
    """화면과 `tenant.decisions` 로 나가는 형태."""
    # LLM 이 채운 것(검증·강등 후)
    판정: str
    요약: str
    해야할일: list[dict[str, str]] = field(default_factory=list)
    인용목록: list[인용] = field(default_factory=list)
    전제목록: list[전제] = field(default_factory=list)
    # 코드가 채우는 것. LLM 이 스스로 주장한 값을 쓰지 않는다
    신뢰등급: Optional[Literal["A", "B"]] = None   # 인용 문서의 extraction 등 실제 속성으로 정한다
    버전스탬프: Optional[str] = None               # 인용 문서의 documents.version
    참조사슬: list[dict] = field(default_factory=list)   # refs 레코드 그대로(근거 상세 화면용, 현재 미연결)
    강등사유: list[str] = field(default_factory=list)    # 검증 단계의 감사 로그
    미매핑전제: list[str] = field(default_factory=list)  # F 스키마에 없는 전제 매핑 경로

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
