# -*- coding: utf-8 -*-
"""L3 (주관기관 규정) 로드 — 검색하지 않고 통째로 읽는다.

`RAG.md` §4-1 · `Agent.md` §3-2 · `0831_최종구현.md` §4 (동결 인터페이스).

    l3_load.로드(cur, org_id, 사업명) -> [{"article_id","조번호","조제목","본문"}]
    l3_load.l3룰(cur, org_id, 비목)   -> dict | None

━━ 왜 검색하지 않는가 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
L3 조항은 30~80개다. 20,525청크짜리 L1·L2 풀과 같은 링에 올리면 밀려서 안 뽑힌다.
경쟁 자체를 없앤다 — `tenant.l3_articles` 는 벡터 컬럼이 아예 없는 별도 테이블이고,
판정 검색(`corpus.chunks`, `layer IN ('L1','L2')`)과 물리적으로 다른 경로다.
그래서 멀티테넌시 누수가 **프롬프트 규율이 아니라 스키마**로 막힌다.

━━ 2026-09-02 갱신 — 파서가 들어왔다 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`l3_parse.py` 가 실제 업로드(`server/routes_l3.py` 가 저장한 원본)를 읽어 이 모듈이
기대하는 모양대로 `l3_articles` 를 채운다. 이 모듈 자체는 안 바뀐다 — 그 약속대로다.
`seed_l3_fixture.py` 의 두 기관(합성 데이터)은 여전히 게이팅·RLS 검증 전용이며
실제 파싱 경로를 타지 않는다 — 섞이지 않게 `출처='테스트픽스처'` 로 계속 구분한다.

━━ 🔴 왜 `corpus.rules` 에서 L3 를 읽지 않는가 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
`Agent.md` §3-2 · `rule_base.md` §3-1 의 의사코드는 L3 오버레이를
`rules WHERE layer='L3' AND 기관ID=?` 로 그린다. 오늘 그 경로를 쓰지 않는 이유가 둘이다.

  1. `corpus.rules` 에는 RLS 가 없다 (실측 2026-08-31: `relrowsecurity=false`).
     기관별 데이터를 RLS 없는 공용 테이블에 넣으면 `TENANT_LEAK` 을 구조로 못 막는다.
  2. `seed_rules.py` 가 `TRUNCATE rules` 로 재적재한다. 남의 기관 L3 가 통째로 날아간다.

그래서 L3 룰은 `tenant.l3_articles`(RLS 축이 있고 org 별 격리가 걸린 테이블)에서
**조문을 읽어 그 자리에서 뽑는다.** 저장 위치를 옮기는 설계 변경은 오늘 범위가 아니다 —
`결과보고.md` E 섹션에 올렸다.

실행:  PYTHONIOENCODING=utf-8 python scripts/l3_load.py --org <org_id|기관명>
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import uuid
from decimal import Decimal
from typing import Any

if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db                                                   # noqa: E402

DSN = db.DSN


# ════════════════════════════════════════════════════════════════════════════
# 1. 장(章) 필터 — "사업비 집행 관련 장만"
# ════════════════════════════════════════════════════════════════════════════
# `RAG.md` §4-1 은 `장 IN (:사업비관련장)` 을 "인제스천 시점에 정하는 정적 선택" 이라 쓴다.
# 기관마다 장 이름이 다르므로 목록을 못 박지 않고 **키워드 규칙**으로 판정한다.
# 🔴 제외가 포함을 이긴다. "제5장 사업비 집행 및 복무" 같은 혼합 장이 나오면
#    빠뜨리는 쪽(=상위를 더 보는 쪽)이 아니라 넣는 쪽이 위험하므로... 는 아니다:
#    L3 를 덜 읽으면 상위를 보게 되고(need_upper=True) 그건 안전한 실패다.
#    반대로 인사·복무 조문이 컨텍스트에 섞이면 소형 모델이 엉뚱한 조를 인용한다.
#    그래서 **제외 우선**이 맞다.
_장_포함 = re.compile(
    r"총칙|목적|적용|정의|사업비|집행|비목|예산|정산|사후\s*관리|자산|회계|계약|구매"
)
_장_제외 = re.compile(
    r"인사|복무|보수|징계|출퇴근|휴가|시설|안전|보안|윤리|위원회\s*운영|보칙|부칙"
)


def 사업비관련장(장: str | None) -> bool:
    """이 장을 판정 컨텍스트에 넣는가.

    🔴 `장 IS NULL` 은 **넣는다.** 파서가 장을 못 잡은 것이지 무관하다는 증거가 아니다.
       빼면 "규정에 없다"(=미규정)로 잘못 갈려 상위만 보고 L3 제약을 놓친다.
    """
    if 장 is None or not 장.strip():
        return True
    if _장_제외.search(장):
        return False
    return bool(_장_포함.search(장))


# ════════════════════════════════════════════════════════════════════════════
# 2. 비목 어휘 — `corpus.item_vocab` 이 기준 문서, 상수는 대체 경로
# ════════════════════════════════════════════════════════════════════════════
# 🔴 DB 조회만 하면 안 된다. G 세션의 `TRUNCATE rules` 재적재 창(합류점 1) 처럼
#    참조 테이블이 잠깐 비는 순간이 있고, 그때 비목이 0종이면 L3 룰이 **전건 무음 None**
#    이 된다 — 판정은 계속 돌지만 L3 가 통째로 사라진 걸 아무도 모른다.
_비목_대체경로: dict[str, tuple[str, ...]] = {
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
    """{정본비목: (별칭...)}. `item_vocab` 이 없거나 비면 대체 경로를 쓰고 경고한다."""
    try:
        rows = cur.execute(
            "SELECT 비목, coalesce(별칭,'{}') , coalesce(하위항목,'{}') "
            "FROM corpus.item_vocab WHERE 계통 = '창업'").fetchall()
    except Exception:
        rows = []
    if not rows:
        print("   ⚠️ corpus.item_vocab 조회 실패/0행 — 폴백 어휘 10종을 쓴다", file=sys.stderr)
        return {k: v for k, v in _비목_대체경로.items()}
    vocab: dict[str, tuple[str, ...]] = {}
    for 비목, 별칭, 하위 in rows:
        vocab[비목] = tuple(list(별칭) + list(하위) + list(_비목_대체경로.get(비목, ())))
    for k, v in _비목_대체경로.items():          # 용어 사전에 없는 비목은 대체 경로로 메운다
        vocab.setdefault(k, v)
    return vocab


def 비목추정(조제목: str | None, 본문: str, vocab: dict[str, tuple[str, ...]],
           *, 제목만: bool = False) -> str | None:
    """조문 하나가 어느 비목의 규정인가.

    한국 규정문은 비목을 **조 제목**에 그대로 쓴다 — 제목이 1순위, 본문은 보조다.
    긴 표기부터 맞춰야 '기계장치' 가 '기계장치비' 를 먼저 삼키지 않는다.
    못 고르면 None. 억지로 고르지 않는다 — 틀린 비목은 없는 것보다 나쁘다.

    🔴 `제목만=True` 가 필요한 이유 — 본문 적중은 오탐이 잦다.
       "제21조(자산의 등록 및 관리) 취득가액 500만원을 초과하는 **기계장치**는…" 은
       자산관리 조문이지 기계장치 비목 규정이 아닌데 본문 적중으로는 구별이 안 된다.
       `l3룰()` 이 제목 패스를 먼저 돌려 이 오탐을 걸러낸다.
    """
    후보: list[tuple[int, str]] = []
    for 비목, 별칭들 in vocab.items():
        for 표기 in (비목, *별칭들):
            if not 표기:
                continue
            if 조제목 and 표기 in 조제목:
                후보.append((len(표기) + 100, 비목))   # 제목 적중에 가산점
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
# 🔴 이건 파서가 아니라 **분류기**다. 값을 지어내지 않는다.
#    분류 순서가 곧 안전 순서다: 참조만 → 불가 → 조건부 → 가능 → (포기).
#    '가능' 이 마지막인 이유 — 어느 패턴에도 안 걸린 조문을 '가능' 으로 떨어뜨리면
#    L3 단독 "가능" 이 만들어진다. 그게 이 프로젝트가 가장 피하려는 오답이다.
#    분류 실패는 None 을 돌려 **미규정과 같은 취급**(need_upper=True)으로 보낸다.

_불가 = re.compile(
    r"할\s*수\s*없|하지\s*못한다|아니\s*된다|불가(?!피)|금지|제외한다|"
    r"인정하지\s*(아니하|않)|집행하지\s*(아니하|않)|계상할\s*수\s*없")
_조건부 = re.compile(
    r"사전\s*(승인|심의|검토|보고|협의)|승인을\s*(받|얻)|이내|이하로|한도|"
    r"초과할\s*수\s*없|하여야\s*한다|받아야\s*한다|제출하여야|다만|경우에\s*한(하|정)")
_가능 = re.compile(r"할\s*수\s*있다|집행할\s*수\s*있다|인정한다|가능하다|계상할\s*수\s*있다")

# "…에 따른다 / 준용한다 / 의한다" 로만 끝나는 조문 = 게이팅 (2) 참조만
_참조서술 = re.compile(r"(따른다|준용한다|의한다|따라\s*집행한다|정하는\s*바에\s*따)")

# 상위 규범 인용.  「법령명」 제N조  /  지침 제N조  /  관리기준 제N조
_참조표기 = re.compile(
    r"(?:「(?P<법령>[^」]{2,60})」\s*)?"
    r"(?P<약칭>지침|운영요령|관리기준|세부관리기준|통합관리지침)?\s*"
    r"제\s*(?P<조>\d+)\s*조(?:\s*의\s*(?P<의>\d+))?")

_금액 = re.compile(r"(?P<수>[\d,]+(?:\.\d+)?)\s*(?P<단위>억원|천만원|백만원|만원|원)")
_비율 = re.compile(r"(?P<수>\d+(?:\.\d+)?)\s*(?:%|퍼센트|백분율)")

_단위배수 = {"억원": 100_000_000, "천만원": 10_000_000, "백만원": 1_000_000,
           "만원": 10_000, "원": 1}


def _문장들(본문: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=다)\.\s*|\n+", 본문) if s and s.strip()]


def _참조만인가(본문: str) -> bool:
    """모든 실질 문장이 '상위에 따른다' 뿐인가.

    한 문장이라도 자체 제약(불가/조건/가능)을 담고 있으면 참조만이 아니다 —
    그때는 L3 가 실제로 무언가를 정한 것이고, 게이팅 결론이 달라진다.
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


def _한도추출(본문: str) -> tuple[str | None, Decimal | None, str | None]:
    """금액/비율 한도. **확실할 때만** 채우고 아니면 전부 None.

    "1인 1일 30만원" 처럼 대상 수식이 붙은 것만 잡는다. 본문에 떠도는 숫자
    ("제38조", "2년 이내")를 한도로 오인하면 금액 비교가 통째로 틀린다.
    """
    m = re.search(r"(?P<대상>1인\s*1일|1인당|1인|월|연간|건당|총액의)?\s*"
                  r"(?P<수>[\d,]+(?:\.\d+)?)\s*(?P<단위>억원|천만원|백만원|만원)"
                  r"\s*(?P<서술>이내|이하|를\s*초과할\s*수\s*없|까지)", 본문)
    if m:
        값 = Decimal(m.group("수").replace(",", "")) * _단위배수[m.group("단위")]
        대상 = (m.group("대상") or "").replace(" ", "")
        단위 = {"1인1일": "원/인·일", "1인당": "원/인", "1인": "원/인",
               "월": "원/월", "연간": "원/년", "건당": "원/건"}.get(대상, "원")
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
    elif _불가.search(본문):
        허용 = "불가"
    elif _조건부.search(본문):
        허용 = "조건부"
    elif _가능.search(본문):
        허용 = "가능"
    else:
        return None                      # 🔴 '가능' 으로 떨어뜨리지 않는다

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
        # 🔴 2026-09-06 — 배열화. `corpus.rules.사전승인_조건` 이 TEXT -> TEXT[] 로 바뀌면서
        #    (rule_lookup.py `_조건_리스트화()` 참조) L3 쪽도 리스트 계약으로 맞춘다.
        #    지금은 늘 0개 또는 1개(첫 매칭 문장만 쓰므로) — 내용은 그대로다.
        "사전승인_조건": [조건] if 조건 else [],
        "한도_유형": 유형, "한도_값": 값, "한도_단위": 단위,
        "증빙": sorted(set(증빙)),
    }


# ════════════════════════════════════════════════════════════════════════════
# 4. 상위 참조 해소 — 게이팅 (2) 의 seed_refs
# ════════════════════════════════════════════════════════════════════════════
_약칭_문서 = {
    "지침": "통합관리지침", "통합관리지침": "통합관리지침",
    "운영요령": "운영요령",
    "관리기준": "세부관리기준", "세부관리기준": "세부관리기준",
}


def _상위문서해소(cur, 법령: str | None, 약칭: str | None) -> str | None:
    """참조 표기 → `corpus.documents.doc_id`. 못 찾으면 None(=끊긴 참조)."""
    키 = None
    if 법령:
        키 = 법령
    elif 약칭:
        키 = _약칭_문서.get(약칭)
    if not 키:
        return None
    # 🔴 doc_id 는 `L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223` 처럼
    #    공백이 아니라 **밑줄**로 이어져 있다. 인용 표기는 「중소기업창업 지원사업
    #    통합관리지침」 처럼 공백이다. 양쪽에서 구분자를 다 지우고 맞춰야 붙는다 —
    #    공백만 지우면 전건 끊긴 참조가 되어 seed_refs 가 통째로 비고,
    #    게이팅 (2) 가 상위를 못 짚는다 (무음 실패라 지표에도 안 잡힌다).
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


def _shifted_재매칭(cur, doc_id: str, 약칭: str | None, 조번호숫자: str) -> str | None:
    """🔴 조번호 참조는 구판일 수 있다 — 조 제목으로 재매칭한다 (CLAUDE.md 확정 원칙).

    L3 가 인용한 「지침 제N조」의 N이 옛 개정판 번호면, 현행판에서 조번호가 옮겨가
    직접 조회가 dangling 이 된다. `build_refs.py` 가 L1↔L1 참조에 쓰는 것과 **같은
    기준표**(`지침_조제목`)를 재사용한다 — 새로 만들지 않는다. 「지침」 계열이 아니면
    (기준표가 통합관리지침 전용이라) None.
    """
    if 약칭 not in ("지침", "통합관리지침"):
        return None
    try:
        from build_refs import 지침_조제목
    except ImportError:
        return None
    for 판, tbl in 지침_조제목.items():
        if 판 == "제14차" or 조번호숫자 not in tbl:
            continue
        제목 = tbl[조번호숫자]
        현행 = cur.execute(
            "SELECT 조번호 FROM corpus.doc_articles "
            " WHERE doc_id=%s AND 조제목=%s AND NOT coalesce(삭제,false) LIMIT 1",
            (doc_id, 제목)).fetchone()
        if 현행:
            return 현행[0]
    return None


def 상위참조(cur, 본문: str) -> list[dict[str, Any]]:
    """조문이 인용한 상위 규범을 (해소 여부와 함께) 뽑는다.

    🔴 조 번호가 지정된 참조만 낸다. `CLAUDE.md` — 조 없는 인용을 문서 통째로 펴면
       근로기준법 하나가 6,026청크를 끌고 온다. `_참조표기` 가 `제N조` 를 강제한다.
    """
    out: list[dict[str, Any]] = []
    본 = set()
    for m in _참조표기.finditer(본문):
        조번호숫자 = str(int(m.group("조")))
        조 = f"제{조번호숫자}조" + (f"의{int(m.group('의'))}" if m.group("의") else "")
        법령, 약칭 = m.group("법령"), m.group("약칭")
        if not 법령 and not 약칭:
            continue                       # "제3조" 처럼 자기 규정 내부 참조 — 상위 아님
        표기 = (f"「{법령}」 " if 법령 else "") + (약칭 or "") + f" {조}"
        표기 = re.sub(r"\s+", " ", 표기).strip()
        if 표기 in 본:
            continue
        본.add(표기)
        doc_id = _상위문서해소(cur, 법령, 약칭)
        해소, 보정 = False, None
        if doc_id:
            해소 = bool(cur.execute(
                "SELECT 1 FROM corpus.doc_articles "
                " WHERE doc_id=%s AND 조번호=%s AND NOT coalesce(삭제,false)",
                (doc_id, 조)).fetchone())
            if not 해소 and not m.group("의"):     # 「의N」 붙은 조는 기준표 대상이 아니다
                재매칭 = _shifted_재매칭(cur, doc_id, 약칭, 조번호숫자)
                if 재매칭:
                    해소, 보정 = True, f"{조} 로 표기됨 → {재매칭} 로 재매칭(구판 조번호)"
                    조 = 재매칭
        out.append({"표기": 표기, "doc_id": doc_id if 해소 else None,
                    "조번호": 조, "해소": 해소, "보정": 보정})
    return out


# ════════════════════════════════════════════════════════════════════════════
# 5. 동결 인터페이스
# ════════════════════════════════════════════════════════════════════════════
def _org정규화(org_id) -> str | None:
    """org_id 를 UUID 로 확정한다. 아니면 None(= 게스트 · L3 없음).

    🔴 이게 없으면 잘못된 org_id 가 **판정 전체를 무너뜨린다.** `org_id` 는
       `WHERE a.org_id = %s` 로 UUID 컬럼에 바로 들어가는데, 'guest' 같은 문자열이
       오면 Postgres 가 `invalid input syntax for type uuid` 를 던지고
       **그 트랜잭션이 통째로 abort 된다.** 그 뒤 A 의 오케스트레이터가 같은 커서로
       하는 모든 조회가 `current transaction is aborted` 로 줄줄이 실패한다 —
       L3 하나 없는 것이 검색·룰·인용 검증까지 다 죽이는 연쇄 실패가 된다.
       계약 §2-3 은 "모든 실패의 기본값은 판단불가" 다. 잘못된 org_id 는
       "L3 가 없다"(= 상위를 본다) 로 닫히는 게 맞지, 판정을 죽이는 게 아니다.
    """
    if org_id is None:
        return None
    s = str(org_id).strip()
    if not s:
        return None
    try:
        return str(uuid.UUID(s))
    except (ValueError, AttributeError, TypeError):
        # 무음으로 삼키지 않는다 — 게스트(None)와 잘못된 값은 다른 사건이다
        print(f"   ⚠️ org_id 가 UUID 가 아니다 — L3 없이 진행한다: {org_id!r}",
              file=sys.stderr)
        return None


def _org_컨텍스트(cur, org_id: str) -> None:
    """RLS 축을 세션에 심는다.

    🔴 지금 앱은 테이블 소유자(`postgres`)로 붙어서 **RLS 가 우회된다**
       (`FORCE ROW LEVEL SECURITY` 미설정 · `seed_l3_fixture.py --verify` 가 실측).
       그래서 아래 `WHERE org_id = %s` 가 오늘의 실질 1차 방어다.
       이 SET 은 저권한 롤로 바꿨을 때 정책이 곧바로 먹도록 미리 깔아두는 것 —
       지금은 무해하고, 롤이 바뀌는 날 코드를 다시 안 고쳐도 된다.
    """
    cur.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))


def 로드(cur, org_id: str, 사업명: str | None = None,
       비목: str | None = None) -> list[dict[str, Any]]:
    """현재 기관의 L3 조문을 **검색 없이 통째로** 돌려준다 (`RAG.md` §4-1).

    사업비 관련 장만. `status='active'` 문서만 — `superseded` 를 같이 실으면
    구판 조문이 컨텍스트에 섞여 개정 전 한도를 인용한다.

    ━━ 비목 재정렬 (2026-09-07) — `SUDDOE_L3_비목순` = off | on (기본 off) ━━━━━
    `비목` 을 주고 스위치가 켜져 있으면, 그 비목에 걸리는 조를 **앞으로 옮긴다.**
    🔴 **떨어뜨리지 않는다.** 순서만 바꾼다. 레인 Q(ai-db) 실측이 근거다:
         데모 org 207조 중 비목추정 성공 123 / 실패 84
         그런데 **매치 123개가 «전부 본문 전용»** 이다 — 제목매치 0건.
         원인: 이 org 는 `조제목` 이 207/207 전부 NULL 이다(원본이 세부관리기준이
         아니라 「사업비사용안내문.hwp」·「변경신청서양식.hwp」라 조·항 구조가 없다).
       즉 「제목 적중만 믿고 좁힌다」로 가면 이 org 의 룰 123개가 «통째로 사라진다».
       org 마다 조제목 커버리지가 0~100% 로 갈린다는 사실 자체가, 게이트 없는
       필터링이 왜 위험한지의 근거다.
    🔴 기본값이 off 인 이유 — 이건 B1 블록의 «순서» 를 바꾸므로 판정이 달라질 수
       있다. 코퍼스 동기화 후 기준선을 뜬 «다음에» 이것 하나만 켜서 재야 무엇 때문에
       숫자가 변했는지 가를 수 있다(A12 채택 기준과 같은 규율).
    """
    org_id = _org정규화(org_id)
    if org_id is None:
        return []
    _org_컨텍스트(cur, org_id)
    rows = cur.execute(
        "SELECT a.article_id, a.조번호, a.조제목, a.본문, a.장, a.조번호_int "
        "  FROM tenant.l3_articles a "
        "  JOIN tenant.l3_documents d ON d.doc_id = a.doc_id "
        " WHERE a.org_id = %s AND d.org_id = %s AND d.status = 'active' "
        " ORDER BY a.조번호_int NULLS LAST, a.조번호",
        (org_id, org_id)).fetchall()
    조문 = [{"article_id": aid, "조번호": 조, "조제목": 제목, "본문": 본문}
           for aid, 조, 제목, 본문, 장, _ in rows if 사업비관련장(장)]
    return _비목순_재정렬(cur, 조문, 비목)


def _비목순_재정렬(cur, 조문: list[dict[str, Any]],
               비목: str | None) -> list[dict[str, Any]]:
    """비목에 걸리는 조를 앞으로. **한 건도 안 버린다** (위 docstring 참조).

    안정 정렬이라 그룹 «안의» 순서(조번호 순)는 그대로다. 스위치가 꺼져 있거나
    비목이 없으면 입력 리스트를 «그대로» 돌려준다 — 예전과 바이트 단위로 같다.
    """
    if os.environ.get("SUDDOE_L3_비목순", "off") != "on" or not 비목 or not 조문:
        return 조문
    try:
        vocab = 비목어휘(cur)
    except Exception:                                    # noqa: BLE001
        return 조문            # 어휘를 못 읽으면 재정렬을 포기한다. 판정은 안 죽인다
    걸림 = [a for a in 조문
           if 비목추정(a.get("조제목"), a.get("본문") or "", vocab) == 비목]
    if not 걸림:
        return 조문
    _id = {id(a) for a in 걸림}
    return 걸림 + [a for a in 조문 if id(a) not in _id]


def l3룰(cur, org_id: str, 비목: str) -> dict[str, Any] | None:
    """현재 기관 L3 에 이 비목 규정이 있는가.

    반환 dict 는 `rule_lookup.l3_게이팅()` 이 그대로 먹는다 (`Agent.md` §3-2):
      `참조만` True            → (2) 상위 필요 + seed_refs
      `허용` 이 불가/조건부     → (3) L3 로 닫힌다
      `허용` 이 가능           → (4) 🔴 상위 확인 **강제**
    None 은 (1) 미규정 = 상위 필요.

    🔴 조문은 있는데 분류가 안 되는 경우도 None 이다. '가능' 으로 추정하지 않는다 —
       분류 실패를 '가능' 으로 흘리면 근거 없는 "가능" 이 생긴다. 진단은
       `미분류(cur, org_id)` 로 따로 본다. 무음으로 버리지 않는다.

    🔴 2026-09-06 — **여러 조가 같은 비목에 걸리면 전부 싣는다.** 예전엔 첫 매치에서
       바로 `return` 해서 나머지가 조용히 사라졌다(실측: 한국창업대학교 기계장치가
       제12조(제목 적중) 하나만 쓰고, 본문에만 있는 제21조(자산의 등록 및 관리 —
       "취득가액 500만원 초과 기계장치는 …자산관리대장에 등록")를 아예 못 봤다 —
       제목 패스가 매치를 «하나라도» 찾으면 본문 패스 자체를 안 돌렸기 때문이다).
       대조해 보니 **진짜 충돌은 0건** — 제12조="구매 가능여부", 제21조="구매 후
       등록의무" 처럼 서로 다른 사실을 말하고 있었다. 그래서 답은 "어느 게 우선이냐"가
       아니라 "둘 다 싣는다"다.
       🔴 **판정 우선순위는 그대로 둔다.** 제목 적중을 본문 적중보다 앞에 모아 그중
       첫 번째를 기준선으로 쓴다(`_룰조립()` 그대로, 단일 조 경로와 완전히 같은
       결과) — 이 부분이 안 바뀌어야 "제21조 같은 부수적 언급이 진짜 비목 조문을
       가리는" 원래 걱정도 그대로 막힌다. 2개 이상이면 `_룰조립_다중()` 이 나머지의
       근거·사전승인_조건·증빙만 위에 더한다(판정 자체는 안 바꾼다 — 그 함수 docstring
       참조).
    """
    org_id = _org정규화(org_id)
    if org_id is None or not 비목:
        return None
    _org_컨텍스트(cur, org_id)
    vocab = 비목어휘(cur)
    조문 = _조문들(cur, org_id)
    # 🔴 제목 패스 결과를 먼저 모으고, 본문 패스는 «제목 패스가 못 잡은 조문에 한해»
    #    이어서 모은다 — 둘 다 항상 돈다(예전엔 제목 패스가 하나라도 잡으면 본문
    #    패스 자체를 건너뛰어 제21조 같은 본문 전용 매치가 통째로 사라졌다).
    #    순서는 "제목 적중 먼저"를 지킨다 — matches[0]이 여전히 제목 적중이라
    #    판정 우선순위(아래 `_룰조립_다중` 참조)가 바뀌지 않는다.
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
    if len(matches) == 1:
        a, 조각 = matches[0]
        return _룰조립(cur, a, 비목, 조각)
    return _룰조립_다중(cur, 비목, matches)


def _조문들(cur, org_id: str) -> list[dict[str, Any]]:
    rows = cur.execute(
        "SELECT a.article_id, a.조번호, a.조제목, a.본문, a.장, "
        "       d.doc_id, d.원본파일명, d.출처, d.extraction, d.파싱품질 "
        "  FROM tenant.l3_articles a "
        "  JOIN tenant.l3_documents d ON d.doc_id = a.doc_id "
        " WHERE a.org_id = %s AND d.org_id = %s AND d.status='active' "
        " ORDER BY a.조번호_int NULLS LAST, a.조번호", (org_id, org_id)).fetchall()
    keys = ("article_id", "조번호", "조제목", "본문", "장",
            "doc_id", "원본파일명", "출처", "extraction", "파싱품질")
    return [dict(zip(keys, r)) for r in rows if 사업비관련장(r[4])]


def _룰조립(cur, a: dict, 비목: str, 조각: dict) -> dict[str, Any]:
    refs = 상위참조(cur, a["본문"])
    return {
        "layer": "L3",
        "org_id": None,                      # 호출자가 이미 안다. 넣으면 프롬프트로 샌다
        "비목": 비목,
        **조각,
        "금지예시": [], "허용예시": [],
        # 🔴 인용은 `tenant.l3_articles.article_id` 로 건다. `corpus.doc_articles` 가 아니다 —
        #    A 의 인용 검증이 L1·L2 는 chunk_id, L3 는 article_id 로 갈라 확인한다.
        "근거": [{"article_id": a["article_id"], "doc_id": str(a["doc_id"]),
                 "조번호": a["조번호"], "조제목": a["조제목"], "layer": "L3"}],
        "seed_refs": [r for r in refs if r["해소"]],
        "dangling": [r["표기"] for r in refs if not r["해소"]],
        # 🔴 기계가 뽑은 룰이다. 사람이 검수한 적 없다.
        #    `verified=false` 면 이 룰 단독으로 "가능" 판정이 안 나간다 (Agent.md §5).
        "verified": False,
        "검수자": None,
        "출처": a["출처"],
        "extraction": a["extraction"],
        "원본파일명": a["원본파일명"],
    }


def _룰조립_다중(cur, 비목: str, matches: list[tuple[dict, dict]]) -> dict[str, Any]:
    """같은 비목에 조가 여럿 걸릴 때의 병합.

    🔴 **판정(허용·참조만·사전승인·한도)은 바꾸지 않는다.** 이 조사는 "정보가 조용히
    사라진다"는 결함이지 "판정이 틀리다"는 결함이 아니다 — 스칼라 값을 여러 조에서
    골라 새로 계산하면 그 자체가 새 판정 로직이 되어 버린다(실측해 보니 실제로
    기계장치·지급수수료 2곳에서 '조건부'가 '불가'로 바뀌었다 — 이건 고치는 게 아니라
    다른 문제를 만드는 것이다). 그래서 **첫 매치(`_룰조립()`)를 그대로 기준선으로
    쓰고**, 나머지 매치에서는 "조용히 사라지던" 목록형 부가정보(근거·사전승인_조건·
    증빙·seed_refs·dangling)만 더한다. 실측(한국창업대학교 기계장치)에서 걸린 두
    조는 "구매 가능여부"·"구매 후 등록의무"처럼 서로 다른 사실이었다 — 이런 부가
    정보를 잃지 않는 게 이 함수의 목적이지, 새 판정을 만드는 게 아니다.

    `_룰조립()` (단일 조)과 필드 모양이 같아야 `_l3정규화()`·호출부가 안 깨진다.
    """
    base = _룰조립(cur, matches[0][0], 비목, matches[0][1])
    if len(matches) == 1:
        return base

    근거 = list(base["근거"])
    seed_refs = list(base["seed_refs"])
    dangling = list(base["dangling"])
    사전승인_조건 = list(base["사전승인_조건"])
    증빙 = list(base["증빙"])

    for a, 조각 in matches[1:]:
        refs = 상위참조(cur, a["본문"])
        근거.append({"article_id": a["article_id"], "doc_id": str(a["doc_id"]),
                     "조번호": a["조번호"], "조제목": a["조제목"], "layer": "L3"})
        seed_refs.extend(r for r in refs if r["해소"])
        dangling.extend(r["표기"] for r in refs if not r["해소"])
        사전승인_조건.extend(조각.get("사전승인_조건") or [])
        증빙.extend(조각.get("증빙") or [])

    base["근거"] = 근거
    base["seed_refs"] = list({(r.get("doc_id"), r.get("조번호")): r for r in seed_refs}.values())
    base["dangling"] = sorted(set(dangling))
    base["사전승인_조건"] = list(dict.fromkeys(사전승인_조건))   # 순서 보존 중복제거
    base["증빙"] = sorted(set(증빙))
    return base


# ════════════════════════════════════════════════════════════════════════════
# 6. 진단 — 무음 결손을 보이게 한다
# ════════════════════════════════════════════════════════════════════════════
def 미분류(cur, org_id: str) -> list[dict[str, Any]]:
    """비목은 잡혔는데 `_추출` 이 포기한 조문. 파서 개선의 1순위 재료다."""
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
        "SELECT doc_id, 원본파일명, version, status, 출처, extraction, 파싱품질, dangling수 "
        "  FROM tenant.l3_documents WHERE org_id=%s ORDER BY 원본파일명", (org_id,)).fetchall()
    keys = ("doc_id", "원본파일명", "version", "status", "출처",
            "extraction", "파싱품질", "dangling수")
    return [dict(zip(keys, r)) for r in rows]


class 기관모호(ValueError):
    """부분 이름이 여러 기관에 걸린다. 후보를 담는다."""

    def __init__(self, 기관: str, 후보: list[tuple[str, str]]):
        self.기관, self.후보 = 기관, 후보
        super().__init__(f"'{기관}' 이 {len(후보)}곳에 걸린다")


def org해소(cur, 기관: str) -> tuple[str, str] | None:
    """org_id 또는 기관명으로 기관을 찾는다 (CLI 편의).

    🔴 2026-09-02 — 이전에는 `ILIKE %…% LIMIT 1` 하나였다. `ORDER BY` 도 없었다.
    `tenant.orgs` 가 2행일 땐 아무 일도 안 났는데 **413행이 되자 조용히 틀리기
    시작했다** — 실측:
        '대학교'    160곳 매치 → 항상 옛 테스트픽스처 기관을 돌려줬다
        '산학협력단'  45곳 매치 → 〃
    에러도 경고도 없이 **엉뚱한 기관의 L3 규정이 로드된다.** skip 보다 나쁘다 —
    skip 은 「안 쟀다」는 흔적이라도 남기는데 이건 「쟀는데 틀렸다」를 흔적 없이 통과시킨다.

    그래서 세 단을 갈랐다:
      ① org_id 또는 기관명 **완전 일치** — 하나뿐이다. 그대로 돌려준다
      ② 부분 일치가 **딱 하나** — 그것이다
      ③ 부분 일치가 **여럿** — 🔴 고르지 않고 `기관모호` 로 던진다.
         「아마 이것」을 만들지 않는다. 사용자가 좁혀야 한다
    """
    row = cur.execute(
        "SELECT org_id, 기관명 FROM tenant.orgs "
        " WHERE org_id::text = %s OR 기관명 = %s",
        (기관, 기관)).fetchone()
    if row:
        return (str(row[0]), row[1])

    후보 = cur.execute(
        "SELECT org_id, 기관명 FROM tenant.orgs WHERE 기관명 ILIKE %s "
        ' ORDER BY "기관명" LIMIT 21', (f"%{기관}%",)).fetchall()
    if not 후보:
        return None
    if len(후보) == 1:
        return (str(후보[0][0]), 후보[0][1])
    raise 기관모호(기관, [(str(r[0]), r[1]) for r in 후보])


def main() -> None:
    ap = argparse.ArgumentParser(description="L3 로드 점검")
    ap.add_argument("--org", required=True, help="org_id 또는 기관명 일부")
    ap.add_argument("--사업명", default=None)
    a = ap.parse_args()

    with db.connect() as conn, conn.cursor() as cur:
        try:
            찾음 = org해소(cur, a.org)
        except 기관모호 as e:
            # 🔴 하나를 골라주지 않는다. 고르면 사용자는 «찾았다» 고 믿는다.
            print(f"🔴 '{e.기관}' 이 {len(e.후보)}곳에 걸린다 — 더 좁혀라")
            for oid, 이름 in e.후보[:20]:
                print(f"     {이름}   {oid}")
            if len(e.후보) > 20:
                print("     … (20곳까지만 보인다)")
            sys.exit(1)
        if not 찾음:
            print(f"🔴 기관을 못 찾았다: {a.org}")
            sys.exit(1)
        org_id, 기관명 = 찾음
        print(f"기관 {기관명}  ({org_id})")
        for d in 문서요약(cur, org_id):
            print(f"   문서 {d['원본파일명']}  {d['version']}  {d['status']} "
                  f"· 출처={d['출처']} · {d['extraction']} · dangling {d['dangling수']}")

        조문 = 로드(cur, org_id, a.사업명)
        전체 = cur.execute(
            "SELECT count(*) FROM tenant.l3_articles WHERE org_id=%s", (org_id,)).fetchone()[0]
        print(f"\n로드 {len(조문)}조 (전체 {전체}조 · 장 필터로 {전체-len(조문)}조 제외)")
        for c in 조문:
            print(f"   {c['조번호']:<8} {c['조제목'] or '':<24} {len(c['본문'])}자")

        print("\n비목별 l3룰")
        for 비목 in sorted(비목어휘(cur)):
            r = l3룰(cur, org_id, 비목)
            if r is None:
                print(f"   {비목:<24} —  (미규정 → need_upper=True)")
            else:
                한도 = (f" · 한도 {r['한도_값']}{r['한도_단위']}" if r["한도_값"] else "")
                갈래 = "참조만" if r["참조만"] else r["허용"]
                # 🔴 2026-09-06 — 근거[0] 만 찍으면 다중매치 수정이 눈에 안 보인다.
                #    전부 찍는다(콤마로 나열) — 조 개수만큼 값이 늘어난 걸 확인하려고
                #    이 CLI 를 켰을 텐데 화면이 예전처럼 하나만 보이면 고친 게 안 보인다.
                조번호들 = ",".join(g["조번호"] for g in r["근거"])
                print(f"   {비목:<24} {갈래}{한도} · {조번호들}"
                      + (f" · seed_refs {len(r['seed_refs'])}" if r["seed_refs"] else "")
                      + (f" · 🔴dangling {r['dangling']}" if r["dangling"] else ""))

        미 = 미분류(cur, org_id)
        print(f"\n미분류 조문: {len(미)}" + ("  ⚠️ 파서 개선 대상" if 미 else ""))
        for m in 미:
            print("   ", m)


if __name__ == "__main__":
    main()
