# -*- coding: utf-8 -*-
"""LLM 출력 스키마 2겹. 기준 문서는 `LLM.md` §3-4.

[1겹] LLM 출력      — vLLM `guided_json` 강제 대상. LLM 이 채운다
[2겹] 최종 응답     — (5) 검증·강등기가 변환·보강해 화면과 `tenant.decisions` 로

🔴 **폐쇄 목록을 이 파일에 박지 않는다.**
   `비목` enum 은 `_비목_어휘집.json` 의 `guided_json_enum` 을 **실행 시점에** 읽는다
   (`rule_base.md` §1-b — 기준 문서는 파일이다). 여기에 복사하면 용어 사전이 바뀌어도
   조용히 옛 목록으로 강제하고, `rules.비목` 조인이 끊긴다.
   같은 이유로 F 필드 경로는 `tenant` 실제 컬럼에서 만든다 (`llm_validate.py`).

실행:
    PYTHONIOENCODING=utf-8 python scripts/llm_schema.py          # 스키마 실물 출력
    PYTHONIOENCODING=utf-8 python scripts/llm_schema.py --slot 1 # ① 정규화 호출 자리만
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# 훅이 PYTHONIOENCODING=utf-8 을 강제하므로 보통 이미 utf-8 이다.
# 조건 없이 다시 감싸면 **import 될 때 앞의 래퍼가 GC 되며 버퍼가 닫힌다** —
# llm_validate 가 llm_schema 를 import 하는 순간 터졌다.
if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
어휘집_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"

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
    p = 경로 or 어휘집_경로
    v = json.loads(p.read_text(encoding="utf-8"))
    대기 = v.get("enum_검수대기") or []
    if 대기:
        print(f"⚠️ 어휘집 enum_검수대기 {len(대기)}종 — 정본 확정 전이다", file=sys.stderr)
    return list(v["guided_json_enum"])


# ════════════════════════════════════════════════════════════════════════════
# [1겹] guided_json — ④-b 판정 조립 호출 자리
# ════════════════════════════════════════════════════════════════════════════
def 체크코드_enum(dsn: str | None = None, 사업명: str | None = None) -> list[str]:
    """`corpus.check_items.code` 중 **이 사업에 해당하는 것만**. 해야할일 폐쇄 목록의 기준 문서.

    이 테이블의 존재 이유가 **안정 식별자**다(`02_frontend.sql`). 열어 두면 LLM 이
    "과업 범위 확정"/"계약 범위 명확화" 를 매번 다르게 뱉고, 재판정 때 사용자가
    체크해 둔 진행상황이 code 로 이어지지 않는다.

    🔴 **사업명으로 걸러야 한다.** `사업명 IS NULL` = 전 사업 공통(38행),
       그 밖은 해당 사업 전용(14행)이다 — `02_frontend.sql:26`.
       안 거르면 52개가 통째로 guided_json enum 에 들어가서, 예비창업패키지 판정에
       LLM 이 `기장대행한도_재도전`·`자격증응시료아님_초격차` 를 고를 수 있다.
       인덱스를 `(사업명, 비목, 구분)` 로 만들어 놓고 그 키로 조회하지 않던 구멍이었다
       (2026-09-01 실측 · 오너 지시).

    🔴 **사업명을 안 주면 공통 38개만 준다.** 전체를 주는 쪽을 기본값으로 두면
       호출자가 인자를 빠뜨린 순간 조용히 예전 버그로 돌아간다. 좁은 쪽이 안전하다 —
       남의 사업 항목을 제안하는 것보다 항목이 모자란 게 낫다.
    """
    from _lib import db
    with db.connect(dsn) as conn:
        return [r[0] for r in conn.execute(
            'SELECT code FROM corpus.check_items '
            'WHERE "사업명" IS NULL OR "사업명" = %s ORDER BY code', [사업명]).fetchall()]


def 판정_스키마(s번호들: list[str] | None = None,
             코드들: list[str] | None = None) -> dict[str, Any]:
    """§3-4 [1겹] 을 JSON Schema 로. vLLM `guided_json` 에 그대로 넣는다.

    s번호들 을 주면 `인용`·`근거조항` 을 **그 집합 안으로 강제**한다 —
    §3-4 "컨텍스트에 부여된 S번호 집합 안에서만". 디코딩 단계에서 막으면
    검증기가 폐기할 일이 애초에 줄어든다.
    주지 않으면 패턴만 건다 (조립기가 아직 S번호를 못 정한 단계용).
    """
    s번호 = ({"type": "string", "enum": list(s번호들)} if s번호들
             else {"type": "string", "pattern": S번호_PATTERN})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["판정", "요약", "해야할일", "인용", "전제"],
        "properties": {
            "판정": {"type": "string", "enum": list(판정_ENUM)},
            "요약": {"type": "string", "minLength": 1, "maxLength": 300},
            # 🔴 §3-4 원문은 `{항목, 설명}` 2필드다. `코드들` 을 주면 **`code` 를 추가**해
            #    `check_items.code` 폐쇄 목록으로 닫는다 — `02_frontend.sql` 이
            #    "code 는 guided_json enum 으로 그대로 들어간다" 고 요구하기 때문이다.
            #    두 사양이 어긋나 있어 인자로 갈랐다. 안 주면 §3-4 모양 그대로다.
            "해야할일": {
                "type": "array", "maxItems": 10,
                "items": ({
                    "type": "object", "additionalProperties": False,
                    "required": ["code", "항목", "설명"],
                    "properties": {"code": {"type": "string", "enum": list(코드들)},
                                   "항목": {"type": "string", "minLength": 1},
                                   "설명": {"type": "string"}},
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
                        "사실": {"type": "string", "minLength": 1},
                        "근거조항": s번호,
                        # F필드 경로의 **참조 목록**. 수식 금지 — 계산은 룰에서 코드가 한다
                        "매핑": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "미충족시": {"type": "string", "enum": list(미충족시_ENUM)},
                    },
                },
            },
        },
    }


# ════════════════════════════════════════════════════════════════════════════
# [1겹] guided_json — ① 정규화 호출 자리
# ════════════════════════════════════════════════════════════════════════════
def 정규화_스키마(비목목록: list[str] | None = None) -> dict[str, Any]:
    """자연어 → JSON. `비목` 이 용어 사전 enum 으로 닫히는 유일한 호출 자리이다.

    ⚠️ `비목` 은 인자로 받지 않으면 **실행 시점에 파일을 읽는다.** 하드코딩 금지.
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


# ════════════════════════════════════════════════════════════════════════════
# [2겹] 최종 응답 — 누가 채우는지를 타입으로 가른다
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class 인용:
    """S번호는 LLM 이, 나머지는 **전부 코드가** 채운다 (§3-4 [2겹] 표).

    치환 주체는 (6) 검증·강등기다 — 조립기(3)가 아니다. 조립기가 하면 s맵에 원문을
    통째로 실어 넘겨야 해서 계약이 무거워진다. 2026-08-31 중앙세션과 확정.
    """
    s번호: str                                  # LLM
    doc_id: Optional[str] = None                # 코드 — s맵 → DB
    조번호: Optional[str] = None                # 코드
    조제목: Optional[str] = None                # 코드
    항호: Optional[str] = None                  # 코드 (s맵 값이 기준 문서)
    원문: Optional[str] = None                  # 코드 — S번호 → DB 원문 치환. **생성 금지**
    원문범위: Optional[str] = None              # 코드 — '항' | '조전체' | '청크'. 아래 주석
    version: Optional[str] = None               # 코드 — documents.version
    extraction: Optional[str] = None            # 코드 — 신뢰등급 산정 입력


@dataclass
class 전제:
    사실: str                                   # LLM
    근거조항: Optional[str]                     # LLM (S번호). 없으면 (5)에서 폐기
    매핑: list[str] = field(default_factory=list)   # LLM — F필드 경로 참조 목록
    미충족시: str = "불가"                      # LLM
    미매핑: bool = False                        # 코드 — F 스키마에 없는 경로가 섞였나


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
