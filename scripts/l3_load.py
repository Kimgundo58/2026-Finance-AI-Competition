# -*- coding: utf-8 -*-
"""L3(주관기관 규정) 조문을 검색 없이 기관 단위로 전체를 읽고, 비목별 L3 룰을 뽑는다.

판정 파이프라인(orchestrate)이 프롬프트용 조문(조문_조회)과 rule_lookup 의 L3 룰(l3룰)을 얻을 때 부른다.
공개 함수: 조문_조회(cur, org_id, 비목=None) -> [{article_id, 조번호, 조제목, 본문}],
l3룰(cur, org_id, 비목) -> dict | None, 사업비관련장(장), 상위참조(cur, 본문). 읽는 테이블:
tenant.l3_articles·l3_documents(RLS), corpus.item_vocab·documents·doc_articles.
불변식: L3 는 검색하지 않는다(corpus.chunks 와 다른 테이블). 분류하지 못한 조문을 '가능' 으로 추정하지 않는다.

실행: PYTHONIOENCODING=utf-8 python scripts/l3_load.py --org <org_id>
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import uuid
from decimal import Decimal
from typing import Any

from _lib import db

_log = logging.getLogger(__name__)

# L3 조항은 기관당 수십 개라 L1·L2 와 같은 검색 후보 풀에 넣으면 순위에서 밀려 뽑히지 않는다.
# 그래서 벡터 컬럼이 없는 tenant.l3_articles 를 기관 단위로 전체를 읽는다. 판정 검색
# (corpus.chunks, layer IN ('L1','L2'))과 테이블이 달라 기관 간 유출을 스키마로 막는다.
# L3 룰을 corpus.rules 에서 읽지 않는 이유: corpus.rules 에는 RLS 가 없고 TRUNCATE 재적재가
# 기관 데이터를 지운다. tenant.l3_articles 는 RLS 정책으로 org 별로 격리된다.


# ════════════════════════════════════════════════════════════════════════════
# 1. 장(章) 필터 — 사업비 집행 관련 장만
# ════════════════════════════════════════════════════════════════════════════
# 기관마다 장 이름이 다르므로 목록을 고정하지 않고 키워드 규칙으로 판정한다.
# 제외가 포함보다 우선한다. L3 를 덜 읽으면 상위 규정을 보게 되고(need_upper=True)
# 그건 안전한 실패다. 반대로 인사·복무 조문이 컨텍스트에 섞이면 소형 모델이
# 엉뚱한 조를 인용한다.
_장_포함 = re.compile(
    r"총칙|목적|적용|정의|사업비|집행|비목|예산|정산|사후\s*관리|자산|회계|계약|구매"
)
_장_제외 = re.compile(
    r"인사|복무|보수|징계|출퇴근|휴가|시설|안전|보안|윤리|위원회\s*운영|보칙|부칙"
)


def 사업비관련장(장: str | None) -> bool:
    """이 장을 판정 컨텍스트에 넣는가.

    `장 IS NULL` 은 넣는다. 파서가 장을 못 잡은 것이지 무관하다는 증거가 아니다.
    빼면 미규정으로 잘못 분류되어 상위 규정만 보고 L3 제약을 놓친다.
    """
    if 장 is None or not 장.strip():
        return True
    if _장_제외.search(장):
        return False
    return bool(_장_포함.search(장))


# ════════════════════════════════════════════════════════════════════════════
# 2. 비목 어휘 — corpus.item_vocab 이 기준, 상수는 폴백
# ════════════════════════════════════════════════════════════════════════════
# DB 조회만 하면 안 된다. rules 재적재 중에는 참조 테이블이 잠깐 비고, 그때 비목이
# 0종이면 L3 룰이 전부 경고 없이 None 이 된다. 이 기본 별칭 표가 그 순간을 메운다.
_비목_기본별칭: dict[str, tuple[str, ...]] = {
    "재료비": ("재료 및 원료비", "원재료비", "재료"),
    "외주용역비": ("외주 용역비", "용역비", "외주"),
    "기계장치": ("기계장치비", "공구기구비", "비품비", "공구", "기구", "비품",
               "소프트웨어", "기계"),
    "특허권등무형자산취득비": ("특허권 등 무형자산 취득비", "무형자산취득비",
                            "무형자산", "특허"),
    "인건비": ("인 건 비",),
    "지급수수료": ("지급 수수료", "수수료", "멘토링비", "멘토링", "사무실임차료",
                "기술이전비", "시험·인증비"),
    "여비": ("출장비", "출장"),
    "교육훈련비": ("교육 훈련비", "교육훈련", "교육"),
    "광고선전비": ("광고 선전비", "마케팅비", "광고", "홍보"),
    "창업활동비": (),
}


def 비목어휘(cur) -> dict[str, tuple[str, ...]]:
    """{표준 비목명: (별칭...)}. item_vocab 이 없거나 비면 기본 별칭 표로 폴백하고 경고한다."""
    try:
        rows = cur.execute(
            "SELECT 비목, coalesce(별칭,'{}') , coalesce(하위항목,'{}') "
            "FROM corpus.item_vocab WHERE 비목체계 = '창업'").fetchall()
    except Exception:
        rows = []
    if not rows:
        _log.warning("corpus.item_vocab 조회 실패 또는 0행 — 기본 별칭 표로 폴백한다")
        return {k: v for k, v in _비목_기본별칭.items()}
    vocab: dict[str, tuple[str, ...]] = {}
    for 비목, 별칭, 하위 in rows:
        vocab[비목] = tuple(list(별칭) + list(하위) + list(_비목_기본별칭.get(비목, ())))
    for k, v in _비목_기본별칭.items():          # 비목 어휘집에 없는 비목은 기본 별칭으로 채운다
        vocab.setdefault(k, v)
    return vocab


def 비목추정(조제목: str | None, 본문: str, vocab: dict[str, tuple[str, ...]],
           *, 제목만: bool = False) -> str | None:
    """조문 하나가 어느 비목의 규정인가.

    한국 규정문은 비목을 조 제목에 그대로 쓴다. 제목이 1순위, 본문은 보조다.
    긴 표기부터 맞춰야 '기계장치' 가 '기계장치비' 를 먼저 가져가지 않는다.
    못 고르면 None. 억지로 고르지 않는다 — 틀린 비목은 없는 것보다 나쁘다.

    `제목만=True` 는 본문 일치의 오탐을 거른다. 본문에 비목명이 나와도 실제 주제는
    다를 수 있다(예: 자산관리). `l3룰()` 이 제목 매칭을 먼저 실행한다.
    """
    후보: list[tuple[int, str]] = []
    for 비목, 별칭들 in vocab.items():
        for 표기 in (비목, *별칭들):
            if not 표기:
                continue
            if 조제목 and 표기 in 조제목:
                후보.append((len(표기) + 100, 비목))   # 제목 일치에 가산점
            elif not 제목만 and 표기 in 본문[:200]:
                후보.append((len(표기), 비목))
    if not 후보:
        return None
    후보.sort(reverse=True)
    최고 = 후보[0][0]
    선택 = {비목 for 점수, 비목 in 후보 if 점수 == 최고}
    return 후보[0][1] if len(선택) == 1 else None   # 동점이면 포기


# ════════════════════════════════════════════════════════════════════════════
# 3. 조문 → 룰 추출
# ════════════════════════════════════════════════════════════════════════════
# 파서가 아니라 분류기다. 원문에 없는 값을 만들지 않는다.
# 분류 순서가 곧 안전 순서다: 참조만 → 불가 → 조건부 → 가능 → (포기).
# '가능' 이 마지막인 이유: 어느 패턴에도 안 걸린 조문을 '가능' 으로 분류하면
# L3 단독 '가능' 이 만들어진다. 분류 실패는 None 을 돌려 미규정과 같은 취급
# (need_upper=True)으로 보낸다.

_불가 = re.compile(
    r"할\s*수\s*없|하지\s*못한다|아니\s*된다|불가(?!피)|금지|제외한다|"
    r"인정하지\s*(아니하|않)|집행하지\s*(아니하|않)|계상할\s*수\s*없")
_조건부 = re.compile(
    r"사전\s*(승인|심의|검토|보고|협의)|승인을\s*(받|얻)|이내|이하로|한도|"
    r"초과할\s*수\s*없|하여야\s*한다|받아야\s*한다|제출하여야|다만|경우에\s*한(하|정)")
_가능 = re.compile(r"할\s*수\s*있다|집행할\s*수\s*있다|인정한다|가능하다|계상할\s*수\s*있다")

# "…에 따른다 / 준용한다 / 의한다" 로만 끝나는 조문 = rule_lookup.l3_상위확인 의 '참조만' 분기
_참조서술 = re.compile(r"(따른다|준용한다|의한다|따라\s*집행한다|정하는\s*바에\s*따)")

# 상위 규정 인용.  「법령명」 제N조  /  지침 제N조  /  관리기준 제N조
_참조표기 = re.compile(
    r"(?:「(?P<법령>[^」]{2,60})」\s*)?"
    r"(?P<약칭>지침|운영요령|관리기준|세부관리기준|통합관리지침)?\s*"
    r"제\s*(?P<조>\d+)\s*조(?:\s*의\s*(?P<의>\d+))?")

_비율 = re.compile(r"(?P<수>\d+(?:\.\d+)?)\s*(?:%|퍼센트|백분율)")

# 금액 한도 표현. "500만원 이내" · "500만원을 초과할 수 없다" · "500만원을 초과하여 집행할 수 없다" ·
# "500만원을 넘을 수 없다" · "500만원까지". 서술이 없는 숫자(예: "500만원의 외주")는 한도가 아니다.
_금액한도 = re.compile(
    r"(?P<대상>1인\s*1일|1인당|1인|1건당|1건|건당|1회|회당|월|연간|총액의)?\s*"
    r"(?P<수>[\d,]+(?:\.\d+)?)\s*(?P<단위>억원|천만원|백만원|만원)"
    r"\s*(?P<서술>이내|이하|까지|한도|의?\s*범위\s*(?:에서|내|안)|"
    r"[을를]?\s*(?:초과|넘)[^.\n]{0,20}?수\s*없|"
    r"[을를]?\s*(?:초과|넘)[^.\n]{0,20}?(?:아니\s*된다|못한다|불가))")

_단위배수 = {"억원": 100_000_000, "천만원": 10_000_000, "백만원": 1_000_000,
           "만원": 10_000, "원": 1}


def _문장들(본문: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=다)\.\s*|\n+", 본문) if s and s.strip()]


def _참조만인가(본문: str) -> bool:
    """모든 실질 문장이 '상위 규정에 따른다' 뿐인가.

    한 문장이라도 자체 제약(불가/조건/가능)을 담고 있으면 참조만이 아니다.
    그때는 L3 가 실제로 무언가를 정한 것이고, 상위 규정 확인 결론이 달라진다.
    """
    실질 = [s for s in _문장들(본문)
            if not re.match(r"^\s*제?\s*\d+\s*조", s) and len(s) > 6]
    if not 실질:
        return False
    참조문장 = 0
    for s in 실질:
        if _참조서술.search(s) and _참조표기.search(s):
            참조문장 += 1
        elif _불가.search(s) or _가능.search(s) or _조건부.search(s):
            return False        # 자체 제약이 있다 → 참조만 아님
    return 참조문장 > 0


def _한도문장뿐(본문: str) -> bool:
    """`_불가` 에 걸린 문장이 전부 금액 한도 문장인가.

    "외주용역비는 1건당 500만원을 초과하여 집행할 수 없다" 는 '할 수 없' 때문에 `_불가` 에 걸리지만
    비목 전체 금지가 아니라 한도다. 이걸 '불가' 로 두면 한도 안의 집행까지 전부 불가가 된다.
    한도 값을 실제로 뽑을 수 있을 때만 한도로 보고(그래야 코드가 금액을 비교한다), 한도 없는
    금지 문장이 하나라도 있으면 False 를 돌려 '불가' 를 유지한다.
    """
    # 문장 단위로 보면 "500만원을 초과할 수 없으며, 가족 업체와의 거래는 집행할 수 없다" 처럼
    # 한 문장에 한도와 금지가 같이 있을 때 금지를 놓친다. 그래서 위치로 본다: 모든 금지 표현이
    # 한도 표현 구간 안에 들어 있어야 한도 문장이다.
    한도구간 = [m.span() for m in _금액한도.finditer(본문)]
    불가들 = list(_불가.finditer(본문))
    return bool(불가들) and all(any(s <= m.start() and m.end() <= e for s, e in 한도구간)
                                for m in 불가들)


def _한도추출(본문: str) -> tuple[str | None, Decimal | None, str | None]:
    """금액/비율 한도. 확실할 때만 채우고 아니면 전부 None.

    "1인 1일 30만원" 처럼 대상 수식이 붙은 것만 잡는다. 본문의 다른 숫자
    ("제38조", "2년 이내")를 한도로 오인하면 금액 비교 전체가 틀린다.
    """
    m = _금액한도.search(본문)
    if m:
        값 = Decimal(m.group("수").replace(",", "")) * _단위배수[m.group("단위")]
        대상 = re.sub(r"\s+", "", m.group("대상") or "")
        # 분모 구분자는 rule_lookup._단위파싱 이 '/' 로만 가른다. '원/인·일' 로 쓰면 분모가
        # '인·일' 한 덩어리가 되어 비교하지 못한다.
        단위 = {"1인1일": "원/인/일", "1인당": "원/인", "1인": "원/인",
               "월": "원/월", "연간": "원/년", "건당": "원/건", "1건당": "원/건",
               "1건": "원/건", "1회": "원/건", "회당": "원/건"}.get(대상, "원")
        return "금액", 값, 단위
    m = _비율.search(본문)
    if m and re.search(r"이내|이하|초과할\s*수\s*없", 본문):
        return "비율", Decimal(m.group("수")), "%"
    return None, None, None


def _추출(본문: str) -> dict[str, Any] | None:
    """조문 본문 하나 → 룰 조각. 분류 못 하면 None."""
    참조만 = _참조만인가(본문)
    if 참조만:
        허용 = None
    elif _불가.search(본문) and not _한도문장뿐(본문):
        허용 = "불가"
    elif _금액한도.search(본문) or _조건부.search(본문):
        허용 = "조건부"
    elif _가능.search(본문):
        허용 = "가능"
    else:
        return None                      # '가능' 으로 분류하지 않는다

    유형, 값, 단위 = _한도추출(본문)
    사전승인 = bool(re.search(r"사전\s*(승인|심의|검토|보고)|승인을\s*(받|얻)", 본문))
    조건 = None
    if 사전승인:
        for s in _문장들(본문):
            if re.search(r"사전\s*(승인|심의|검토|보고)|승인을\s*(받|얻)", s):
                조건 = s if s.endswith(".") else s + "."
                break
    증빙 = re.findall(
        r"([가-힣A-Za-z0-9·()]*(?:계약서|영수증|명세서|확인서|증명서|보고서|계획서|"
        r"이수증|대장|명부|사본|세금계산서|견적서|검수조서))", 본문)
    return {
        "허용": 허용,
        "참조만": 참조만,
        "사전승인": 사전승인,
        # corpus.rules.사전승인_조건 이 TEXT[] 라 L3 도 리스트로 맞춘다(첫 매칭 문장 하나).
        "사전승인_조건": [조건] if 조건 else [],
        "한도_유형": 유형, "한도_값": 값, "한도_단위": 단위,
        "증빙": sorted(set(증빙)),
    }


# ════════════════════════════════════════════════════════════════════════════
# 4. 상위 참조 해결 — '참조만' 분기의 seed_refs
# ════════════════════════════════════════════════════════════════════════════
_약칭_문서 = {
    "지침": "통합관리지침", "통합관리지침": "통합관리지침",
    "운영요령": "운영요령",
    "관리기준": "세부관리기준", "세부관리기준": "세부관리기준",
}


def _상위문서_찾기(cur, 법령: str | None, 약칭: str | None) -> str | None:
    """참조 표기 → `corpus.documents.doc_id`. 못 찾으면 None(미해결 참조)."""
    키 = None
    if 법령:
        키 = 법령
    elif 약칭:
        키 = _약칭_문서.get(약칭)
    if not 키:
        return None
    # doc_id 는 밑줄로 이어지고 인용 표기는 공백이다. 양쪽에서 구분자를 다
    # 지우고 비교해야 일치한다 — 그렇지 않으면 전부 미해결 참조가 되어 seed_refs 가 빈다.
    핵심 = re.sub(r"[\s_·\-()]+", "", 키)
    for 패턴 in (f"%{핵심}%", f"%{핵심[:10]}%"):
        row = cur.execute(
            "SELECT doc_id FROM corpus.documents "
            " WHERE layer IN ('L1','L2') AND index_target "
            "   AND translate(doc_id, ' _·-()', '') ILIKE %s "
            " ORDER BY (status='active') DESC, 시행일 DESC NULLS LAST LIMIT 1",
            (패턴,)).fetchone()
        if row:
            return row[0]
    return None


def 상위참조(cur, 본문: str) -> list[dict[str, Any]]:
    """조문이 인용한 상위 규정을 해결 여부와 함께 뽑는다.

    반환: [{표기, doc_id(해결 시), 조번호, 해결}]. 조 번호가 지정된 참조만 낸다 — 조 없는 인용을
    문서 전체로 펼치면 근로기준법 같은 큰 문서 전체가 함께 조회된다. `_참조표기` 가 `제N조` 를 강제한다.
    """
    out: list[dict[str, Any]] = []
    처리한_표기 = set()
    for m in _참조표기.finditer(본문):
        조번호숫자 = str(int(m.group("조")))
        조 = f"제{조번호숫자}조" + (f"의{int(m.group('의'))}" if m.group("의") else "")
        법령, 약칭 = m.group("법령"), m.group("약칭")
        if not 법령 and not 약칭:
            continue                       # "제3조" 처럼 자기 규정 내부 참조 — 상위 규정 아님
        표기 = (f"「{법령}」 " if 법령 else "") + (약칭 or "") + f" {조}"
        표기 = re.sub(r"\s+", " ", 표기).strip()
        if 표기 in 처리한_표기:
            continue
        처리한_표기.add(표기)
        doc_id = _상위문서_찾기(cur, 법령, 약칭)
        해결 = False
        if doc_id:
            해결 = bool(cur.execute(
                "SELECT 1 FROM corpus.doc_articles "
                " WHERE doc_id=%s AND 조번호=%s AND NOT coalesce(삭제,false)",
                (doc_id, 조)).fetchone())
        out.append({"표기": 표기, "doc_id": doc_id if 해결 else None,
                    "조번호": 조, "해결": 해결})
    return out


# ════════════════════════════════════════════════════════════════════════════
# 5. 공개 함수 (판정 파이프라인이 호출)
# ════════════════════════════════════════════════════════════════════════════
def _org정규화(org_id) -> str | None:
    """org_id 를 UUID 문자열로 확정한다. 아니면 None(게스트, L3 없음).

    잘못된 문자열이 UUID 컬럼 비교에 들어가면 Postgres 트랜잭션이 abort 되어 같은 커서의
    이후 조회가 모두 실패한다. 잘못된 org_id 는 "L3 가 없다"(상위 규정을 본다)로 처리하고
    판정은 계속한다.
    """
    if org_id is None:
        return None
    s = str(org_id).strip()
    if not s:
        return None
    try:
        return str(uuid.UUID(s))
    except (ValueError, AttributeError, TypeError):
        # 게스트(None)와 잘못된 값은 다른 경우라 경고를 남긴다
        _log.warning("org_id 가 UUID 가 아니다 — L3 없이 진행한다: %r", org_id)
        return None


def _rls_org_설정(cur, org_id: str) -> None:
    """RLS 정책이 읽는 GUC app.org_id 를 트랜잭션 범위로 설정한다.

    앱은 RLS 가 적용되는 비특권 롤(suddoe_app)로 접속하므로 이 값이 없으면 0행이다.
    쿼리의 `WHERE org_id = %s` 는 RLS 와 별개로 두는 코드 쪽 격리 조건이다.
    """
    cur.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))


def _조문들(cur, org_id: str) -> list[dict[str, Any]]:
    """기관의 active L3 문서에서 사업비 관련 장의 조문을 조번호 순으로 읽는다.

    app.org_id GUC 의 유효 범위: 커넥션이 쉬는 중이면 여기서 연 BEGIN/COMMIT 이 끝날 때 사라진다.
    호출자가 이미 트랜잭션 안이면 SAVEPOINT 를 해제해도 남으므로 바깥 트랜잭션이 끝날 때까지 이 기관 값이다.
    """
    # set_config(..., true) 는 트랜잭션 범위다. autocommit 커넥션에서 따로 실행하면
    # 다음 SELECT 전에 GUC 가 사라져 RLS 정책이 0행을 돌려준다. 같은 트랜잭션
    # (autocommit 이면 BEGIN/COMMIT, 아니면 SAVEPOINT) 안에서 함께 실행한다.
    with cur.connection.transaction():
        _rls_org_설정(cur, org_id)
        rows = cur.execute(
            "SELECT a.article_id, a.조번호, a.조제목, a.본문, a.장, "
            "       d.doc_id, d.원본파일명, d.출처, d.extraction, d.파싱품질 "
            "  FROM tenant.l3_articles a "
            "  JOIN tenant.l3_documents d ON d.doc_id = a.doc_id "
            " WHERE a.org_id = %s AND d.org_id = %s AND d.status='active' "
            " ORDER BY a.조순번 NULLS LAST, a.조번호", (org_id, org_id)).fetchall()
    keys = ("article_id", "조번호", "조제목", "본문", "장",
            "doc_id", "원본파일명", "출처", "extraction", "파싱품질")
    return [dict(zip(keys, r)) for r in rows if 사업비관련장(r[4])]


def 조문_조회(cur, org_id: str, 비목: str | None = None) -> list[dict[str, Any]]:
    """현재 기관의 L3 조문을 검색 없이 전부 돌려준다. org_id 가 없거나 잘못되면 [].

    사업비 관련 장, status='active' 문서만 읽는다 — superseded 문서를 함께 넣으면
    구판 조문이 컨텍스트에 섞여 개정 전 한도를 인용한다. `비목` 은 호출 호환용
    인자로 지금은 결과에 영향을 주지 않는다.
    """
    org_id = _org정규화(org_id)
    if org_id is None:
        return []
    return [{k: a[k] for k in ("article_id", "조번호", "조제목", "본문")}
            for a in _조문들(cur, org_id)]


def l3룰(cur, org_id: str, 비목: str) -> dict[str, Any] | None:
    """현재 기관 L3 에 이 비목 규정이 있으면 룰 dict, 없으면 None.

    `rule_lookup.l3_상위확인()` 이 반환값을 그대로 읽는다:
      `참조만` True           → 상위 규정 필요 + seed_refs
      `허용` 이 불가/조건부    → L3 로 결정
      `허용` 이 가능          → 상위 규정 확인 강제
    None 은 미규정(상위 규정 필요)이다. 조문은 있는데 분류가 안 되는 경우도 None 이다 —
    '가능' 으로 추정하지 않는다. 진단은 `미분류(cur, org_id)` 로 따로 본다.

    여러 조가 같은 비목에 걸리면 모두 반영한다. 제목 일치를 본문 일치보다 앞에 모으고
    그중 첫 번째의 판정 값을 쓴다(`_룰병합()`).
    """
    org_id = _org정규화(org_id)
    if org_id is None or not 비목:
        return None
    vocab = 비목어휘(cur)
    조문 = _조문들(cur, org_id)
    # 제목 패스 결과를 먼저 모으고, 본문 패스는 제목 패스가 못 잡은 조문에 한해
    # 이어서 모은다. matches[0] 이 늘 제목 일치라 판정 우선순위가 바뀌지 않는다.
    matches: list[tuple[dict, dict]] = []
    seen: set[int] = set()
    for 제목만 in (True, False):
        for a in 조문:
            if a["article_id"] in seen:
                continue
            if 비목추정(a["조제목"], a["본문"], vocab, 제목만=제목만) != 비목:
                continue
            조각 = _추출(a["본문"])
            if 조각 is None:
                continue
            matches.append((a, 조각))
            seen.add(a["article_id"])
    if not matches:
        return None
    return _룰병합(cur, 비목, matches)


def _룰병합(cur, 비목: str, matches: list[tuple[dict, dict]]) -> dict[str, Any]:
    """매칭된 조문들을 L3 룰 하나로 합친다. 반환 형식은 `rule_lookup._l3_룰행변환()` 이 읽는다.

    판정 값(허용·참조만·사전승인·한도)은 첫 매치에서만 가져온다 — 여러 조에서 스칼라를
    새로 계산하면 그 자체가 새 판정 로직이 된다. 근거·seed_refs·미해결참조·사전승인_조건·
    증빙 같은 목록만 모든 매치에서 모은다.
    """
    첫조문, 첫조각 = matches[0]
    근거: list[dict] = []
    seed_refs: list[dict] = []
    미해결참조: list[str] = []
    사전승인_조건: list[str] = []
    증빙: list[str] = []
    for a, 조각 in matches:
        refs = 상위참조(cur, a["본문"])
        # 인용은 tenant.l3_articles.article_id 로 건다. 인용 검증이 L1·L2 는 chunk_id,
        # L3 는 article_id 로 나눠 확인한다.
        근거.append({"article_id": a["article_id"], "doc_id": str(a["doc_id"]),
                     "조번호": a["조번호"], "조제목": a["조제목"], "layer": "L3"})
        seed_refs.extend(r for r in refs if r["해결"])
        미해결참조.extend(r["표기"] for r in refs if not r["해결"])
        사전승인_조건.extend(조각.get("사전승인_조건") or [])
        증빙.extend(조각.get("증빙") or [])
    if len(matches) > 1:
        # 여러 조에서 모은 목록만 중복을 제거한다. 단일 매치는 상위참조 순서를 그대로 둔다.
        seed_refs = list({(r.get("doc_id"), r.get("조번호")): r for r in seed_refs}.values())
        미해결참조 = sorted(set(미해결참조))
        사전승인_조건 = list(dict.fromkeys(사전승인_조건))   # 순서 보존 중복 제거
    return {
        "layer": "L3",
        "org_id": None,                      # 호출자가 이미 안다. 넣으면 프롬프트에 노출된다
        "비목": 비목,
        **첫조각,
        "사전승인_조건": 사전승인_조건,
        "증빙": sorted(set(증빙)),
        "금지예시": [], "허용예시": [],
        "근거": 근거,
        "seed_refs": seed_refs,
        "미해결참조": 미해결참조,
        # 자동 추출한 룰이고 사람이 검수하지 않았다. verified=false 면 이 룰
        # 단독으로 '가능' 판정이 나가지 않는다.
        "verified": False,
        "검수자": None,
        "출처": 첫조문["출처"],
        "extraction": 첫조문["extraction"],
        "원본파일명": 첫조문["원본파일명"],
    }


# ════════════════════════════════════════════════════════════════════════════
# 6. 진단 (CLI) — 드러나지 않는 누락을 보이게 한다
# ════════════════════════════════════════════════════════════════════════════
def 미분류(cur, org_id: str) -> list[dict[str, Any]]:
    """비목은 잡혔는데 `_추출` 이 포기한 조문. 분류 규칙을 고칠 때 먼저 볼 목록이다."""
    if _org정규화(org_id) is None:
        return []
    vocab = 비목어휘(cur)
    out = []
    for a in _조문들(cur, org_id):
        비목 = 비목추정(a["조제목"], a["본문"], vocab)
        if 비목 and _추출(a["본문"]) is None:
            out.append({"조번호": a["조번호"], "조제목": a["조제목"], "비목": 비목})
    return out


def 문서요약(cur, org_id: str) -> list[dict[str, Any]]:
    if _org정규화(org_id) is None:
        return []
    rows = cur.execute(
        "SELECT doc_id, 원본파일명, version, status, 출처, extraction, 파싱품질, 미해결참조수 "
        "  FROM tenant.l3_documents WHERE org_id=%s ORDER BY 원본파일명", (org_id,)).fetchall()
    keys = ("doc_id", "원본파일명", "version", "status", "출처",
            "extraction", "파싱품질", "미해결참조수")
    return [dict(zip(keys, r)) for r in rows]


def main() -> None:
    if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="L3 조문 조회 점검. 문서 요약까지 보려면 SUDDOE_DSN 이 테이블 소유자 롤이어야 한다.")
    ap.add_argument("--org", required=True, help="org_id (UUID)")
    a = ap.parse_args()
    org_id = _org정규화(a.org)
    if org_id is None:
        sys.exit(f"org_id 가 UUID 가 아니다: {a.org}")

    with db.connect() as conn, conn.cursor() as cur:
        기관 = cur.execute("SELECT 기관명 FROM tenant.orgs WHERE org_id = %s",
                         (org_id,)).fetchone()
        if not 기관:
            sys.exit(f"기관을 찾지 못했다: {org_id}")
        print(f"기관 {기관[0]}  ({org_id})")
        for d in 문서요약(cur, org_id):
            print(f"   문서 {d['원본파일명']}  {d['version']}  {d['status']} "
                  f"· 출처={d['출처']} · {d['extraction']} · 미해결참조 {d['미해결참조수']}")

        조문 = 조문_조회(cur, org_id)
        전체 = cur.execute(
            "SELECT count(*) FROM tenant.l3_articles WHERE org_id=%s", (org_id,)).fetchone()[0]
        print(f"\n조회 {len(조문)}조 (전체 {전체}조 · 장 필터로 {전체-len(조문)}조 제외)")
        for c in 조문:
            print(f"   {c['조번호']:<8} {c['조제목'] or '':<24} {len(c['본문'])}자")

        print("\n비목별 l3룰")
        for 비목 in sorted(비목어휘(cur)):
            r = l3룰(cur, org_id, 비목)
            if r is None:
                print(f"   {비목:<24} —  (미규정 → need_upper=True)")
            else:
                한도 = (f" · 한도 {r['한도_값']}{r['한도_단위']}" if r["한도_값"] else "")
                분류 = "참조만" if r["참조만"] else r["허용"]
                # 근거 전부를 나열한다 — 근거[0] 만 찍으면 여러 조가 매칭된 경우가 안 보인다.
                조번호들 = ",".join(g["조번호"] for g in r["근거"])
                print(f"   {비목:<24} {분류}{한도} · {조번호들}"
                      + (f" · seed_refs {len(r['seed_refs'])}" if r["seed_refs"] else "")
                      + (f" · 미해결참조 {r['미해결참조']}" if r["미해결참조"] else ""))

        미 = 미분류(cur, org_id)
        print(f"\n미분류 조문: {len(미)}" + ("  (분류 규칙 개선 대상)" if 미 else ""))
        for m in 미:
            print("   ", m)


if __name__ == "__main__":
    main()
