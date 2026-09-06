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
import os
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
용어사전_경로 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"
# 🔴 어휘집과 «다른 파일» 이다. 어휘집은 build_item_vocab.py 가 통째로 덮어쓴다
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
    """비목 이름 -> 정의 한 줄. 정규화(①) 프롬프트 «설명» 전용이다.

    🔴 `guided_json` enum 은 이 파일을 «보지 않는다». 폐쇄 목록의 기준 문서는
    여전히 `_비목_어휘집.json` 하나다 — 여기서 정의가 빠지거나 늘어도 모델이
    고를 수 있는 값은 안 변한다. 설명이 느는 것뿐이다.

    🔴 왜 `_비목_어휘집.json` 에 「정의」 키를 더하지 «않았나». 그 파일은
    `scripts/archive/indexing/build_item_vocab.py` 가 생성한다. 그 스크립트는 `doc` 를
    새로 짜서 `write_text` 로 통째로 덮어쓴다(build_item_vocab.py:388) — 손으로 더한
    키는 다음 재생성 때 «조용히» 사라진다. 그래서 별도 파일로 뺐다.

    파일이 없으면 빈 dict 를 돌려준다. 정의는 «있으면 좋은 것» 이지 없다고 정규화가
    죽어야 할 것이 아니다 — 호출자가 이름만으로 된 종전 프롬프트로 돌아간다.
    """
    p = 경로 or 비목정의_경로
    if not p.exists():
        print(f"⚠️ 비목 정의 파일이 없다: {p} — 이름만으로 프롬프트를 만든다", file=sys.stderr)
        return {}
    return dict(json.loads(p.read_text(encoding="utf-8"))["정의"])


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
    스키마: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["판정", "요약", "해야할일", "인용", "전제"],
        "properties": {
            "판정": {"type": "string", "enum": list(판정_ENUM)},
            # 🔴 2026-09-07(중앙 ai-33, 오너 지시) — minLength 1 이 «구멍» 이었다.
            #    run 197 실측: 12건 중 5건이 요약을 `"S02, S03"` 처럼 인용
            #    앵커만으로 채웠고, 오답 3건이 «전부» 그 5건 안에 있었다.
            #    요약을 안 쓰면 판정도 찍는다 — 문장을 강제한다(B0 에 문안 규칙).
            "요약": {"type": "string", "minLength": 20, "maxLength": 300},
            # 🔴 2026-09-06(레인 H, ai-8c 승인) — `코드들` 이 있으면 LLM 은 **code 하나만**
            #    고른다. `인용`(S번호만 LLM, 나머지는 코드)과 같은 원칙 — code 는 안정
            #    식별자이고(`체크코드_enum` 독스트링), 항목·설명은 `corpus.check_items`
            #    에서 코드가 채운다(`llm_validate.py::체크항목_본문()`). §3-4 원문은
            #    `{항목, 설명}` LLM 저작 2필드였는데, 그건 code 가 «없을 때»(아래 else,
            #    폴백)만 남긴다 — 폴백은 손대지 않는다(같이 고치면 무너진다).
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
                        # 🔴 2026-09-06(레인 H, ai-8c 승인) — minLength 1 -> 5.
                        #    실측(run 195 원출력 전제.사실 180개): 길이<=4 가 100건
                        #    (55.6%) «전부» 가비지였다 — S번호 패턴 76건("S03" 등)
                        #    + 맨 단어 24건("용도"·"증빙"·"인건비"). 예외 0건.
                        #    반대로 정상 사실도 7자·9자에 실재한다("사전승인 완료"
                        #    7자·"수량 과하지 않음" 9자) — 오너 초안 12는 이 둘을
                        #    «수집 손실» 시킨다. 5는 그 손실 없이 확인된 가비지를 막는다.
                        #    🔴 **부분 방어다.** 문법 제약은 지식을 만들지 않는다 —
                        #    실패의 «모양»을 바꿀 뿐이다. "시제품 제작"(6자) 류 명사구가
                        #    5~14자 구간에 이미 섞여 있어, 이후 5자 이상으로 늘어난
                        #    가비지가 나올 수 있다. 실제로 줄어드는지는 GPU 를 켜야
                        #    안다 — 이 세션은 그걸 확인하지 않았다(할 수 없었다).
                        "사실": {"type": "string", "minLength": 5},
                        "근거조항": s번호,
                        # F필드 경로의 **참조 목록**. 수식 금지 — 계산은 룰에서 코드가 한다
                        "매핑": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        "미충족시": {"type": "string", "enum": list(미충족시_ENUM)},
                    },
                },
            },
        },
    }
    return _순서_적용(스키마)


# ════════════════════════════════════════════════════════════════════════════
# A13 스키마 «필드 순서» 변형 — 문구가 아니라 «디코딩 순서» 축
# ════════════════════════════════════════════════════════════════════════════
# 🔴 2026-09-07(ai-33) — strict json_schema / guided_json 은 **스키마에 적힌 순서대로**
#    토큰을 뱉는다. 기본 순서는 `판정` 이 «맨 앞» 이라, 모델은 어떤 조항이 걸리는지
#    (`인용`·`전제`) 쓰기 **전에** 판정을 확정한다.
#      · GPU(vLLM, thinking ON) 에서는 문제가 덜하다 — 근거를 사고블록에서 이미 훑고 온다.
#      · Qwen API(thinking 없음) 에서는 **근거를 한 글자도 안 쓴 상태의 첫 토큰이 판정**이다.
#    Qwen 기준선 실측(320문항): 정답=가능 24%(4/17) · 정답=판단불가 0%(0/8) ·
#    오답 20건 중 17건이 「조건부」 — 근거 없이 4지선다를 찍으면 가운데가 나온다.
#
#    ⚠️ 이건 **가설이다.** 채택 기준(assemble_context 의 A12 3개)을 그대로 적용한다.
#    A12(문구) 와 «같이» 바꾸지 않는다 — 같이 바꾸면 무엇이 효과인지 못 가른다.
_순서_변형들: dict[str, str] = {
    "S0": "기준선 — 판정·요약·해야할일·인용·전제 (지금 운영 순서)",
    "S1": "근거우선 — 인용·전제 를 판정 «앞» 으로. 근거를 먼저 쓰게 해 thinking 을 대신한다",
}
_S1_순서 = ["인용", "전제", "판정", "요약", "해야할일"]


def _순서_적용(스키마: dict[str, Any]) -> dict[str, Any]:
    """`SUDDOE_스키마순서`(S0|S1, 기본 S0)에 맞춰 필드 순서만 바꾼다.

    🔴 **기본값은 바이트 단위로 예전과 같다** — 미설정이면 입력 객체를 그대로 돌려준다
    (`is` 로 증명 가능). 값이 무엇이든 «키 집합·제약은 하나도 안 바뀐다» — 순서만이다.
    """
    순서 = os.environ.get("SUDDOE_스키마순서", "S0")
    if 순서 == "S0":
        return 스키마
    if 순서 != "S1":
        raise ValueError(f"SUDDOE_스키마순서={순서!r} — 'S0' 또는 'S1' 만 허용")
    props = 스키마["properties"]
    if set(_S1_순서) != set(props):
        raise ValueError(f"S1 순서표가 스키마와 안 맞는다: {sorted(props)}")
    새 = dict(스키마)
    새["properties"] = {k: props[k] for k in _S1_순서}
    새["required"] = list(_S1_순서)          # required 순서도 같이 맞춘다
    return 새


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
    """`사실`·`매핑`·`미충족시`는 LLM 이, 근거조항의 문서 해석은 `인용`과 같은
    원칙으로 코드가 채운다 (2026-09-06 레인 H).

    🔴 `근거조항`(S번호)이 `인용`과 «같은 s맵 체계»를 쓰는데도 문서명·조번호·원문으로
    안 풀려서, 화면(`server/inquiry.py`)에 "지침은 「S03」 이 정하고 있습니다" 처럼
    내부 번호가 그대로 나갈 뻔했다. `인용` 과 같은 필드를 같은 방식으로 더한다 —
    채우는 자리도 `인용`과 같다: 조립기(3)가 아니라 (6) 검증·강등기다
    (`llm_validate.py::검증()` 의 `s번호_메타()` 조회를 그대로 재사용한다).

    🔴 `인용`과 달리 DB 조회가 비어도(`CITE_DB_MISSING`처럼) 전제 자체를 폐기하지
    않는다 — `사실` 은 그 자체로 유효한 정보이고(사용자가 확인해야 할 것),
    근거 문서를 못 찾은 것은 «인용문 완성 실패» 이지 «사실 자체의 무효» 가 아니다.
    이때는 doc_id 이하가 전부 None 으로 남고, 화면 쪽(`inquiry.py`)이 이미
    "필드가 없으면 그 문장을 통째로 뺀다" 원칙을 갖고 있어 안전하게 흡수된다.
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
    원문: Optional[str] = None                  # 코드 — S번호 → DB 원문 치환. **생성 금지**
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
