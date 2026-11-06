# -*- coding: utf-8 -*-
"""판정 호출의 LLM 출력을 검증하고 필요하면 판정을 하향해 최종 응답으로 바꾼다.

판정 파이프라인 6단계(orchestrate)가 부른다. 공개: 검증(llm출력, 문장맵, *, ...) -> (최종 응답 dict, 검증사유),
검증코드_전체, 문장ID_메타, 체크항목_본문, 프로필_경로집합. 쓰는 테이블: tenant.incidents(TENANT_LEAK 발생 시).
LLM 이 스스로 주장한 값은 믿지 않는다. 인용은 문장맵과 DB 원문으로, 신뢰등급은 documents.extraction 으로,
전제 매핑은 프로필 스키마 컬럼으로 검증하고, 실패한 것은 고치지 않고 버리거나 하향한다. 하향은 불가를 만들지 않는다.
실행: PYTHONIOENCODING=utf-8 python scripts/llm_validate.py --profile-paths
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from typing import Iterable, Optional

from _lib import db
from llm_schema import 판정_ENUM, 미충족시_ENUM, 인용, 전제, 최종응답
from ontology_norm import 별지_행_내용, 조번호_표시 as 쪽_표시

_log = logging.getLogger(__name__)

# 검증코드의 기준 목록. tenant.decisions 의 decisions_검증코드_check 와 같은 순서·같은 집합이어야 한다.
# 여기 없는 코드는 _검증기록 이 거부하고, CHECK 에 없는 코드는 INSERT 가 거부된다.
# 판정 하향 코드뿐 아니라 경고(DANGLING_WARN·DOMAIN_WARN·CITE_PARAGRAPH_MISMATCH)도 들어간다.
# PRECEDENCE_OVERRIDE 는 코드가 판정을 올리던 경로라 없앴다(2026-11-06 결정). 저장된 행과 CHECK 에 남아 있어 목록에서 빼지 않는다.
# ONTOLOGY_BAN · LIMIT_EXCEEDED · VAT_BASIS_UNKNOWN 은 온톨로지 경로의 코드 판정이 붙인다(scripts/ontology_gate.py).
# TASK_ 로 시작하는 다음 4종(TASK_STATE_UNSOURCED, TASK_STATE_MISMATCH, TASK_NUMBER_UNSOURCED, TASK_BASIS_NOT_IN_MAP)은
# 자유서술 해야할일 검사를 없앤 뒤로 내보내지 않는다. 저장된 행과 CHECK 에 남아 있어 목록에서 빼지 않는다.
검증코드_전체: tuple[str, ...] = (
    "INVALID_JUDGMENT", "CITE_NOT_IN_MAP", "CITE_DB_MISSING", "CITE_PARAGRAPH_MISMATCH",
    "PREMISE_NO_BASIS", "PREMISE_BASIS_NOT_IN_MAP", "PREMISE_INVALID_OUTCOME", "PREMISE_UNMAPPED",
    "PREMISE_DB_MISSING", "NO_CITATION", "VLM_DOWNGRADE", "B_GRADE_DOWNGRADE",
    "UNVERIFIED_RULE", "TASK_CODE_INVALID", "TASK_DB_MISSING", "L3_ONLY_DOWNGRADE",
    "TENANT_LEAK", "DANGLING_WARN", "DOMAIN_WARN", "PRECEDENCE_OVERRIDE",
    "TASK_STATE_UNSOURCED", "TASK_STATE_MISMATCH", "TASK_NUMBER_UNSOURCED",
    "TASK_BASIS_NOT_IN_MAP", "ANSWER_UNCONFIRMED",
    "ONTOLOGY_BAN", "LIMIT_EXCEEDED", "VAT_BASIS_UNKNOWN",
)

# 하향으로 판정이 LLM 의 판정과 달라졌을 때 LLM 요약 대신 쓰는 문장. LLM 요약은 LLM 이 낸
# 판정을 설명하므로, 판정 배지와 다른 결론을 말하게 된다. 하향은 불가를 만들지 않는다.
_하향_요약 = {
    "조건부": "확인이 필요한 조건이 있어 조건부로 판정했습니다. 아래 확인 항목과 근거 조항을 살펴봐 주세요.",
    "판단불가": "근거를 확인하지 못해 판정을 내리지 않았습니다. 담당자 확인이 필요합니다.",
}

# 프로필 필드 경로는 tenant 의 실제 컬럼에서 만든다. 경로 표기는 "F1.정부지원.현금" 형식이고
# 컬럼 정부지원_현금 의 '_' 가 '.' 이 된다.
프로필_테이블 = {"F1": "profile_agreements", "F3": "profile_expenditures", "F4": "profile_personnel"}
# profile_agreements 에 있지만 접두어가 따로 붙은 컬럼
프로필_특례 = {"F2": {"과업범위요약"}}
# 저장하지 않는 항목(판정 후 폐기). 테이블이 없으므로 항목명을 그대로 허용한다
비저장_항목 = {"친족", "전직임직원", "구매명의"}
_메타컬럼 = {"profile_id", "org_id", "exec_id", "person_id", "created_at", "updated_at"}

_프로필경로_캐시: set[str] | None = None


def 프로필_경로집합(*, conn=None) -> set[str]:
    """허용되는 프로필 필드 경로 전체. tenant 프로필 테이블의 컬럼이 기준값이다.

    information_schema 는 실행 중에 바뀌지 않으므로 프로세스당 한 번만 읽는다(프로세스 캐시).
    """
    global _프로필경로_캐시
    if _프로필경로_캐시 is not None:
        return set(_프로필경로_캐시)
    out: set[str] = {f"F5.{x}" for x in 비저장_항목}
    for 접두어, 컬럼 in 프로필_특례.items():
        out |= {f"{접두어}.{c}" for c in 컬럼}
    with db.borrow(conn) as c:
        for 접두어, t in 프로필_테이블.items():
            for (col,) in c.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='tenant' AND table_name=%s", (t,)).fetchall():
                if col in _메타컬럼:
                    continue
                out.add(f"{접두어}." + col.replace("_", "."))
    _프로필경로_캐시 = set(out)
    return out


_항마커 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_RE_조 = re.compile(r"제\d+조")


def _항_추출(본문: str, 항호: str | None) -> tuple[str, str]:
    """조 본문에서 해당 항만 잘라 (원문, 범위) 를 돌려준다.

    반환 원문은 항상 본문의 부분 문자열이다. 항 기호를 찾지 못하면 조 전체를 돌려주고 범위로 알린다.
    """
    if not 본문:
        return "", "없음"
    if not 항호:
        return 본문, "조전체"
    # 온톨로지 후보의 별지 표 행(`/지급수수료/유의사항`). 표 칸을 이어 붙인 값이라 본문의 부분 문자열이 아닐 수 있다
    if 항호.startswith("/"):
        내용 = 별지_행_내용(본문, 항호)
        return (내용, "표행") if 내용 else (본문, "조전체(항호 미발견)")
    # 한 조 안에 같은 항 기호가 두 번 나오는 문서가 있어 `①#2` 처럼 몇 번째인지가 붙어 온다
    기호, _, 순 = 항호.partition("#")
    차례 = int(순) if 순.isdigit() else 1
    k, 남은 = -1, 차례
    while 남은 > 0:
        k = 본문.find(기호, k + 1)
        if k < 0:
            break
        남은 -= 1
    if k < 0:
        return 본문, "조전체(항호 미발견)"
    뒤 = [본문.find(m, k + 1) for m in _항마커 if 본문.find(m, k + 1) > 0]
    end = min(뒤) if 뒤 else len(본문)
    잘린 = 본문[k:end].strip()
    return (잘린, "항") if 잘린 else (본문, "조전체")


def 체크항목_본문(codes: set[str] | None = None, *, conn=None) -> dict[str, dict]:
    """`corpus.check_items` 의 code → {항목, 설명, 사업명, 비목}. `codes` 를 주면 그 code 만 읽는다.

    해야할일은 LLM 이 code 만 고르고 항목·설명은 여기서 읽은 DB 원문으로 채운다(인용과 같은 원칙).
    사업명·비목이 둘 다 NULL 인 항목은 모든 지출에 해당하는 공통 필수다(_공통필수 참고).
    """
    with db.borrow(conn) as c:
        if codes:
            rows = c.execute(
                'SELECT code, "항목", "설명", "사업명", "비목" '
                'FROM corpus.check_items WHERE code = ANY(%s)',
                (list(codes),)).fetchall()
        else:
            rows = c.execute(
                'SELECT code, "항목", "설명", "사업명", "비목" '
                'FROM corpus.check_items').fetchall()
    return {r[0]: {"항목": r[1], "설명": r[2], "사업명": r[3], "비목": r[4]} for r in rows}


def _공통필수(체크항목: dict[str, dict]) -> list[str]:
    """모든 지출에 해당하는 항목의 code. `사업명`·`비목` 이 둘 다 NULL 인 것이다.

    부가세 제외, 사업비 카드 사용, 협약기간 내 집행처럼 지출 종류와 무관하게 항상 해당하므로 LLM 이
    고르지 않아도 붙인다. LLM 이 빈 배열을 내면 화면의 결제 전 확인 목록이 비기 때문이다.
    """
    return [c for c, v in 체크항목.items()
            if v.get("사업명") is None and v.get("비목") is None]


_단락번호 = re.compile(r"^단락0*(\d+)$")


def _l3표시명(원본파일명: str | None) -> str | None:
    """L3 문서 UUID 대신 화면에 낼 이름. `L3_` 접두와 확장자를 떼고 밑줄을 공백으로 바꾼다.

    "L3_2024창업중심대학_경상국립대_사업비사용안내문.hwp" → "2024창업중심대학 경상국립대 사업비사용안내문".
    파일명이 없으면 None 이고 호출자가 원래 값(UUID)을 쓴다.
    """
    if not 원본파일명:
        return None
    이름 = 원본파일명.rsplit(".", 1)[0]
    if 이름.startswith("L3_"):
        이름 = 이름[3:]
    return 이름.replace("_", " ").strip() or None


_항호_기호만 = re.compile(r"^[①-⑳\s,·~]+$")


def _항호_표시(항호: str | None) -> str | None:
    """화면에 낼 항호. 항 기호(①②③)만 남긴다.

    `corpus.chunks.항호` 값은 None(대다수), "①" 같은 항 기호, "항1~③"·"-4#1" 같은 청킹 내부 위치 표기의
    세 종류다. 내부 위치 표기는 사용자가 읽을 표기가 아니어서 항 기호 부분만 남긴다. 원문은 `원문` 필드에 있다.
    """
    if not 항호:
        return None
    t = 항호.strip()
    if _항호_기호만.match(t):
        return t
    기호 = "".join(ch for ch in t if "①" <= ch <= "⑳")
    return 기호 or None


def _조제목_표시(조번호: str | None, 조제목: str | None) -> str | None:
    """조제목 앞에 붙은 조번호 반복을 뗀다.

    조번호="붙임2", 조제목="【붙임 2】창업기업등 사업비 비목 해설표" 처럼 원본이 중복이면 화면이 둘을 이어
    붙일 때 두 번 나온다. 조번호(위치)는 남기고 조제목 맨 앞의 괄호로 감싼 반복만 뗀다.
    """
    if not 조제목:
        return 조제목
    if not 조번호:
        return 조제목
    숫자만 = re.sub(r"\D", "", 조번호)
    글자만 = re.sub(r"[\d\s]", "", 조번호)
    if not (숫자만 and 글자만):
        return 조제목
    앞 = re.match(r"^\s*[【\[(]\s*" + re.escape(글자만) + r"\s*" + 숫자만 + r"\s*[】\])]\s*", 조제목)
    return 조제목[앞.end():].strip() if 앞 else 조제목


def _조번호_표시(조번호: str | None) -> str | None:
    """`단락035` 처럼 파서가 만든 조번호를 "35번째 항목" 으로 바꾼다.

    제N조 구조가 없는 문서(안내문·양식)는 단락 단위로 나뉘어 이런 조번호가 생긴다. 숫자는 사용자가
    원문에서 위치를 찾는 단서라 남긴다.
    """
    if not 조번호:
        return 조번호
    m = _단락번호.match(조번호.strip())
    return f"{int(m.group(1))}번째 항목" if m else 쪽_표시(조번호)


# 문장맵 종류별 메타 조회. 세 쿼리 모두 같은 12개 컬럼을 같은 순서로 돌려준다:
# (id, doc_id, 조번호, 조제목, 항호, version, extraction, 본문, 기관id, domain, layer, 원본파일명)
_메타_SQL = {
    "chunk": """
        SELECT c.chunk_id, c.doc_id, c.조번호, c.조제목, c.항호,
               COALESCE(c.version, d.version), d.extraction, c.text,
               COALESCE(c.기관id, d.기관id), d.domain, c.layer, NULL
          FROM corpus.chunks c JOIN corpus.documents d ON d.doc_id = c.doc_id
         WHERE c.chunk_id = ANY(%s)""",
    "article": """
        SELECT a.article_id, a.doc_id, a.조번호, a.조제목, NULL,
               d.version, d.extraction, a.본문, d.기관id, d.domain, d.layer, NULL
          FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id = a.doc_id
         WHERE a.article_id = ANY(%s)""",
    # L3 는 RLS 정책과 별도로 org_id 조건을 코드에서도 건다. version 이 없다. extraction 은 OCR 로 읽은
    # 스캔본('ocr')만 구분하고 나머지(hwpx·hwp·native)는 텍스트 레이어에서 읽었으므로 native 로 본다.
    # 기관id 자리에는 org_id 를 넣어 인용 단계의 TENANT_LEAK 대조에 쓴다. 원본파일명은 화면 표시용이다
    "l3": """
        SELECT a.article_id, a.doc_id::text, a.조번호, a.조제목, NULL,
               NULL, CASE WHEN d.extraction = 'ocr' THEN 'ocr' ELSE 'native' END,
               a.본문, a.org_id::text, '창업지원사업', 'L3', d.원본파일명
          FROM tenant.l3_articles a
          LEFT JOIN tenant.l3_documents d ON d.doc_id = a.doc_id
         WHERE a.article_id = ANY(%s) AND a.org_id = %s::uuid""",
}


def _메타_행(종류: str, 행: tuple, 항호: str | None) -> dict:
    """메타 조회 한 행을 인용 치환용 dict 로 만든다. 원문은 문장맵의 항호로 자른다.

    청크는 항 단위가 아닐 수 있다(작은 조는 조 전체가 한 청크). 프롬프트 구성 단계가 그 본문을 항으로 나눠
    문장 ID 를 여러 개 매기므로 여기서도 문장맵의 항호로 잘라야 같은 조 전문이 항 개수만큼 중복되지 않는다.
    """
    _, doc, 조, 제목, h, ver, ex, 본문, 기관, dom, lay, 파일명 = 행
    원문, 범위 = _항_추출(본문 or "", 항호)
    if 종류 == "l3":
        return dict(doc_id=_l3표시명(파일명) or doc, 조번호=_조번호_표시(조), 조제목=제목,
                    원문=원문, 원문범위=범위, version=ver, extraction=ex, 항호_DB=None,
                    기관id=기관, domain=dom, layer=lay)
    return dict(doc_id=doc, 조번호=_조번호_표시(조), 조번호_DB=조, 조제목=_조제목_표시(조, 제목), 원문=원문,
                원문범위="청크" if 종류 == "chunk" and 범위 == "조전체" else 범위,
                version=ver, extraction=ex, 항호_DB=_항호_표시(h),
                기관id=기관, domain=dom, layer=lay)


def 문장ID_메타(문장맵: dict, *, conn=None, org_id=None) -> dict[str, dict]:
    """문장맵의 문장 ID 별로 인용 치환과 신뢰등급 산정에 쓰는 DB 사실을 읽는다. DB 에 없는 문장 ID 는 빠진다.

    반환 값 키: doc_id, 조번호, 조제목, 원문, 원문범위, version, extraction, 항호_DB, 기관id, domain, layer.
    `conn` 을 주면 그 커넥션을 쓴다(호출자가 GUC app.org_id 를 이미 설정했다). 주지 않으면 새로 열고 같은
    트랜잭션 안에서 GUC 를 설정해 L3 조회에 RLS 정책이 적용되게 한다. org_id 가 없으면 L3 는 찾지 않는다.
    """
    with db.borrow(conn) as c:
        if conn is None and org_id:
            c.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
        out: dict[str, dict] = {}
        for 종류, sql in _메타_SQL.items():
            ids = {sid: i for sid, (k, i, _) in 문장맵.items() if k == 종류 and i is not None}
            if not ids:
                continue
            인자 = (list(ids.values()),) if 종류 != "l3" else (
                list(ids.values()), str(org_id) if org_id else None)
            행들 = {r[0]: r for r in c.execute(sql, 인자).fetchall()}
            for sid, i in ids.items():
                if i in 행들:
                    out[sid] = _메타_행(종류, 행들[i], 문장맵[sid][2])
        return out


def 인시던트_기록(종류: str, 상세: dict, *, conn=None, org_id=None) -> None:
    """`tenant.incidents` 에 한 건 넣는다. 실패해도 판정은 계속하고 오류 로그만 남긴다.

    `conn` 을 주면 그 커넥션에서 세이브포인트(또는 autocommit 트랜잭션) 안에 넣어, 실패가 호출자의
    트랜잭션을 깨지 않게 한다. 주지 않으면 새 커넥션을 연다.
    """
    try:
        with db.borrow(conn) as c, c.transaction():
            c.execute('INSERT INTO tenant.incidents ("종류", "상세", org_id) '
                      'VALUES (%s, %s::jsonb, %s::uuid)',
                      (종류, json.dumps(상세, ensure_ascii=False, default=str),
                       str(org_id) if org_id else None))
    except Exception:                                    # noqa: BLE001
        _log.exception("인시던트 기록 실패(%s): 상세=%s", 종류, 상세)


class _검증기록:
    """검증코드와 사유를 모은다. `검증기록(코드, 사유)` 로 부른다.

    검증코드_전체 에 없는 코드는 ValueError 로 막는다. 그대로 두면 decisions INSERT 가 CHECK 제약에서 거부된다.
    """

    def __init__(self) -> None:
        self.사유: list[str] = []
        self.코드: list[str] = []

    def __call__(self, 코드: str, 사유: str) -> None:
        if 코드 not in 검증코드_전체:
            raise ValueError(f"검증코드_전체 에 없는 코드: {코드}")
        self.사유.append(사유)
        if 코드 not in self.코드:
            self.코드.append(코드)


def _인용_검증(llm출력: dict, 문장맵: dict, 메타: dict[str, dict], 현재기관: Optional[str],
            검증기록: _검증기록) -> tuple[list[인용], list[str], list[dict]]:
    """LLM 이 고른 문장 ID 를 DB 원문으로 치환한다. 반환 (인용목록, 인용별 layer, 기관 유출 목록)."""
    인용목록: list[인용] = []
    인용계층: list[str] = []
    유출: list[dict] = []
    # 같은 항이 B2 청크와 B3 조문에 함께 있으면 문장 ID 가 둘이다. 둘 다 고르면 화면에 같은 인용이 두 번 나간다
    이미: set[tuple] = set()
    # 같은 문장 ID 를 두 번 고르면 같은 조문이 두 번 나간다. 순서를 지키며 중복을 뺀다
    for s in dict.fromkeys(llm출력.get("인용") or []):
        if s not in 문장맵:
            검증기록("CITE_NOT_IN_MAP", f"인용 {s} 가 문장맵 밖 → 폐기(환각 인용)")
            continue
        종류, _id, 항호 = 문장맵[s]
        m = 메타.get(s)
        if m is None:
            # 문장맵에는 있는데 DB 에 없다. 원문을 붙일 수 없으므로 버린다
            검증기록("CITE_DB_MISSING", f"인용 {s} 의 원본을 DB 에서 못 찾음({종류} id={_id}) → 폐기")
            continue
        # 기관id NULL 은 공용 규정(L1·L2)이다. NULL 도 현재기관도 아니면 다른 기관의 규정이 인용된 것이라
        # 버리고 인시던트로 기록한다
        기관 = m.get("기관id")
        if 현재기관 is not None and 기관 is not None and str(기관) != str(현재기관):
            검증기록("TENANT_LEAK",
                f"인용 {s} 의 기관id={기관} 이 현재기관={현재기관} 도 NULL 도 아님 → 폐기")
            유출.append({"문장ID": s, "종류": 종류, "id": _id, "기관id": str(기관),
                        "현재기관": str(현재기관), "doc_id": m.get("doc_id")})
            continue
        # 항호는 문장맵 값이 기준값이다. DB 청크가 다른 항을 가리키면 기록만 남긴다
        if m.get("항호_DB") and 항호 and m["항호_DB"] != 항호:
            검증기록("CITE_PARAGRAPH_MISMATCH",
                f"인용 {s} 항호 불일치(문장맵 {항호} vs DB {m['항호_DB']}) → 문장맵 값 채택")
        if m.get("domain") and m["domain"] != "창업지원사업":
            검증기록("DOMAIN_WARN",
                f"인용 {s} 의 문서 domain='{m['domain']}' 이 창업지원사업이 아니다 → 경고")
        키 = (m.get("doc_id"), m.get("조번호"), 항호 or m.get("원문"))
        if 키 in 이미:
            continue
        이미.add(키)
        인용계층.append(m.get("layer") or ("L3" if 종류 == "l3" else "?"))
        인용목록.append(인용(문장ID=s, doc_id=m.get("doc_id"), 조번호=m.get("조번호"),
                          조제목=m.get("조제목"),
                          항호=항호[1:].replace("/", " ") if (항호 or "").startswith("/") else 항호,
                          원문=m.get("원문"), 원문범위=m.get("원문범위"),
                          version=m.get("version"), extraction=m.get("extraction")))
    return 인용목록, 인용계층, 유출


def 근거_인용(문장맵: dict, *, conn=None, org_id=None, 현재기관: Optional[str] = None) -> list[dict]:
    """코드 판정의 근거를 인용과 같은 모양으로 바꾼다. LLM 이 고른 것이 아니어서 문장맵 전부가 인용이다. 원문은 DB 값이다."""
    메타 = 문장ID_메타(문장맵, conn=conn, org_id=org_id)
    인용목록, _계층, _유출 = _인용_검증({"인용": list(문장맵)}, 문장맵, 메타, 현재기관, _검증기록())
    return [asdict(c) for c in 인용목록]


def _전제_검증(llm출력: dict, 문장맵: dict, 메타: dict[str, dict], 허용경로: set[str],
            검증기록: _검증기록) -> tuple[list[전제], list[str]]:
    """전제의 근거조항·미충족시·매핑을 검증한다. 반환 (전제목록, 프로필 스키마 밖 매핑 경로)."""
    전제목록: list[전제] = []
    미매핑: list[str] = []
    for p in llm출력.get("전제") or []:
        사실 = (p.get("사실") or "").strip()
        근거 = p.get("근거조항")
        if not 근거:
            검증기록("PREMISE_NO_BASIS", f"전제 '{사실[:24]}' 근거조항 없음 → 폐기")
            continue
        if 근거 not in 문장맵:
            검증기록("PREMISE_BASIS_NOT_IN_MAP",
                f"전제 '{사실[:24]}' 근거조항 {근거} 가 문장맵 밖 → 폐기(환각 인용)")
            continue
        미충족 = p.get("미충족시")
        if 미충족 not in 미충족시_ENUM:
            검증기록("PREMISE_INVALID_OUTCOME", f"전제 '{사실[:24]}' 미충족시 '{미충족}' enum 밖 → 불가로 고정")
            미충족 = "불가"
        경로 = list(p.get("매핑") or [])
        밖 = [x for x in 경로 if x not in 허용경로]
        if 밖:
            # 전제 자체는 유효할 수 있어 버리지 않는다. 프로필 스키마에 없는 필드일 수 있으므로 표시만 한다
            검증기록("PREMISE_UNMAPPED",
                f"전제 '{사실[:24]}' 매핑 {밖} 가 프로필 스키마 밖 → 미매핑 전제로 표시")
            미매핑.extend(밖)
        # 인용과 같은 방식으로 문서·원문을 채운다. DB 에 없어도 `사실` 은 유효하므로 버리지 않는다
        m = 메타.get(근거)
        if m is None:
            검증기록("PREMISE_DB_MISSING",
                f"전제 '{사실[:24]}' 근거조항 {근거} 의 원본을 DB 에서 못 찾음 → "
                "문서/원문 없이 사실만 유지")
        전제목록.append(전제(
            사실=사실, 근거조항=근거, 매핑=경로, 미충족시=미충족, 미매핑=bool(밖),
            doc_id=(m or {}).get("doc_id"), 조번호=(m or {}).get("조번호"),
            조제목=(m or {}).get("조제목"), 원문=(m or {}).get("원문"),
            원문범위=(m or {}).get("원문범위")))
    return 전제목록, 미매핑


def _신뢰등급(인용목록: list[인용], 미해결참조: Optional[list], 검증기록: _검증기록) -> Optional[str]:
    """인용 문서의 실제 속성으로 신뢰등급을 정한다. 인용이 없으면 None."""
    if not 인용목록:
        등급 = None
    elif 스캔 := [c.문장ID for c in 인용목록 if c.extraction in ("vlm", "ocr")]:
        # 스캔 문서를 VLM·OCR 로 읽은 추출본('vlm'·'ocr')은 A등급 인용으로 쓰지 않는다. 코드 이름은 DB 제약과 맞춰 둔다
        등급 = "B"
        검증기록("VLM_DOWNGRADE", f"인용 {스캔} 가 스캔본 추출본(VLM·OCR) → A등급 금지, B 로 하향")
    else:
        등급 = "A"
    # 조가 지정된 미해결 참조만 센다. 문서 전체를 가리키는 참조(조 없음)는 원래 참조 확장 대상이 아니라서,
    # 이것까지 세면 거의 모든 판정이 하향되어 코드가 의미를 잃는다. 판정은 막지 않고 등급만 낮춘다
    조지정_미해결참조 = [x for x in (미해결참조 or []) if _RE_조.search(str(x))]
    if 조지정_미해결참조:
        if 등급 == "A":
            등급 = "B"
        검증기록("DANGLING_WARN",
            f"참조 확장에 조 지정된 미해결 참조 {len(조지정_미해결참조)}건 — "
            f"{조지정_미해결참조[:3]} → 신뢰등급 하향")
    return 등급


def _판정_하향(판정: str, 신뢰등급: Optional[str], 인용목록: list[인용], 인용계층: list[str],
            룰들: Optional[list[dict]],
            검증기록: _검증기록, *, 사용자답변: Optional[list[dict]] = None,
            답변유실: bool = False) -> tuple[str, Optional[str]]:
    """룰 검수 상태·신뢰등급·인용 계층·사용자 답변으로 판정을 하향한다. 반환 (판정, 신뢰등급).

    순서는 UNVERIFIED_RULE → B_GRADE_DOWNGRADE → L3_ONLY_DOWNGRADE → ANSWER_UNCONFIRMED 이다.
    어느 단계도 판정을 불가나 가능으로 올리지 않는다.
    """
    # corpus.rules.verified=false 인 룰만으로는 '가능' 을 내지 않는다
    if 룰들 is not None and 판정 == "가능" and 룰들 and not any(r.get("verified") for r in 룰들):
        검증기록("UNVERIFIED_RULE", "미검수(verified=false) 룰만으로 '가능' → 조건부로 하향")
        판정 = "조건부"
    # 스캔 문서의 추출본(VLM·OCR)을 근거로 '가능' 을 내면 금액·조번호 오인식 하나가 곧 잘못된 '가능' 이 된다
    if 판정 == "가능" and 인용목록 and 신뢰등급 == "B" and "VLM_DOWNGRADE" in 검증기록.코드:
        검증기록("B_GRADE_DOWNGRADE", "'가능' 인데 인용에 스캔본 추출본(VLM·OCR)이 있음 → 조건부로 하향")
        판정 = "조건부"
    # 기관 자체규정(L3)은 국가 지침 위에 추가되는 문서라 단독으로 '가능' 의 근거가 되지 못한다.
    # 상위 지침의 제약을 놓칠 수 있기 때문이다. L3 단독 '불가' 는 안전한 방향이라 건드리지 않는다
    if 판정 == "가능" and 인용계층 and all(x == "L3" for x in 인용계층):
        검증기록("L3_ONLY_DOWNGRADE", "'가능' 인데 인용이 L3(기관 규정) 단독 → 조건부로 하향")
        판정 = "조건부"
        if 신뢰등급 == "A":
            신뢰등급 = "B"
    # 요건을 충족하는 답이 아니거나(「아니오」·「모름」) 답변을 읽지 못했으면 '가능' 을 내지 않는다.
    # 충족하는 답은 사용자 진술일 뿐이라 판정을 올리지 않는다
    미확인 = sum(a.get("답") != a.get("충족답") for a in 사용자답변 or [])
    if 판정 == "가능" and (미확인 or 답변유실):
        검증기록("ANSWER_UNCONFIRMED",
            f"'가능' 인데 요건 미확인 답변 {미확인}건·답변 유실 {답변유실} → 조건부로 하향")
        판정 = "조건부"
    return 판정, 신뢰등급


def _해야할일(llm출력: dict, 체크코드: set[str], 체크항목: dict[str, dict],
           검증기록: _검증기록) -> list[dict]:
    """LLM 이 고른 code 를 check_items 원문으로 채우고 공통 필수 항목을 더한다.

    code 는 재판정해도 사용자 체크를 유지하는 키라 허용 목록 밖 code 는 버린다(tenant.plan_tasks FK).
    """
    해야할일: list[dict] = []
    for h in llm출력.get("해야할일") or []:
        if not isinstance(h, dict):
            continue
        c = h.get("code")
        if c is None or c not in 체크코드:
            if c is not None:
                검증기록("TASK_CODE_INVALID", f"해야할일 code '{c}' 가 check_items 밖 → 폐기")
            continue
        본문 = 체크항목.get(c)
        if 본문 is None:
            # 허용 목록 안인데 본문을 못 읽었다. 사용자 체크가 끊기지 않게 code 는 남긴다
            검증기록("TASK_DB_MISSING", f"해야할일 code '{c}' 의 항목·설명을 DB 에서 못 찾음")
            해야할일.append({"code": c})
            continue
        해야할일.append({"code": c, "항목": 본문["항목"], "설명": 본문["설명"]})
    이미 = {h.get("code") for h in 해야할일}
    for c in _공통필수(체크항목):
        if c in 이미 or c not in 체크코드:
            continue
        본문 = 체크항목.get(c)
        if 본문:
            해야할일.append({"code": c, "항목": 본문["항목"], "설명": 본문["설명"]})
    return 해야할일


def 검증(llm출력: dict, 문장맵: dict, *,
         메타: Optional[dict[str, dict]] = None,
         프로필경로: Optional[Iterable[str]] = None,
         체크항목: Optional[dict[str, dict]] = None,
         룰들: Optional[list[dict]] = None,
         체크코드: Optional[Iterable[str]] = None,
         현재기관: Optional[str] = None,
         사업명: Optional[str] = None,
         미해결참조: Optional[list] = None,
         org_id=None,
         conn=None,
         사용자답변: Optional[list[dict]] = None,
         답변유실: bool = False) -> tuple[dict, list[str]]:
    """판정 호출 출력을 검증하고 필요하면 하향해 (최종 응답 dict, 검증사유 목록) 을 돌려준다.

    반환 dict 는 최종응답 필드에 `검증코드` 가 더해진 형태다. 판정이 하향되면 요약은 _하향_요약 고정 문구로
    바뀐다. `메타`·`프로필경로`·`체크항목` 을 주지 않으면 `conn`(없으면 새 커넥션)으로 DB 에서 읽는다.
    `룰들` 은 B4 에 들어간 effective_rule 결과, `체크코드` 는 해야할일 code 허용 목록이다.
    `현재기관` 과 다른 기관id 를 가진 인용은 버리고 tenant.incidents 에 기록한다(부수 효과).
    `사용자답변`·`답변유실` 은 확인 질문 결과다. 요건을 충족하는 답이 아니면 '가능' 을 조건부로 낮춘다.
    """
    검증기록 = _검증기록()
    메타 = 메타 if 메타 is not None else 문장ID_메타(문장맵, conn=conn, org_id=org_id)
    허용경로 = set(프로필경로) if 프로필경로 is not None else 프로필_경로집합(conn=conn)
    체크항목 = (체크항목 if 체크항목 is not None
             else (체크항목_본문(set(체크코드), conn=conn) if 체크코드 else {}))

    # 판정이 enum 밖이면 고쳐 맞추지 않고 판단불가로 둔다
    판정 = llm출력.get("판정")
    if 판정 not in 판정_ENUM:
        검증기록("INVALID_JUDGMENT", f"판정 '{판정}' 이 판정 enum(4종) 밖 → 판단불가")
        판정 = "판단불가"

    인용목록, 인용계층, 유출 = _인용_검증(llm출력, 문장맵, 메타, 현재기관, 검증기록)
    if 유출:
        인시던트_기록("TENANT_LEAK", {"사업명": 사업명, "현재기관": str(현재기관), "인용": 유출},
                 conn=conn, org_id=org_id)
    전제목록, 미매핑 = _전제_검증(llm출력, 문장맵, 메타, 허용경로, 검증기록)

    if not 인용목록 and 판정 != "판단불가":
        검증기록("NO_CITATION", "인용 0건인데 판정이 판단불가가 아니다 → 판단불가")
        판정 = "판단불가"
    신뢰등급 = _신뢰등급(인용목록, 미해결참조, 검증기록)
    판정, 신뢰등급 = _판정_하향(판정, 신뢰등급, 인용목록, 인용계층, 룰들, 검증기록,
                         사용자답변=사용자답변, 답변유실=답변유실)
    해야할일 = _해야할일(llm출력, set(체크코드 or ()), 체크항목, 검증기록)

    # 규정버전은 인용된 문서의 version 만 모은다. 문장맵 전체를 쓰면 인용하지 않은 문서의 판본이 붙는다
    버전 = sorted({c.version for c in 인용목록 if c.version})
    요약 = (llm출력.get("요약") or "").strip()
    if 판정 != llm출력.get("판정"):
        요약 = _하향_요약[판정]
    d = 최종응답(
        판정=판정,
        요약=요약,
        해야할일=해야할일,
        인용목록=인용목록, 전제목록=전제목록,
        신뢰등급=신뢰등급,
        규정버전=", ".join(버전) or None,
        참조관계=[],                      # 판정 파이프라인이 채운다
        검증사유=검증기록.사유,
        미매핑전제=sorted(set(미매핑)),
    ).to_dict()
    # 검증코드는 tenant.decisions.검증코드 로 그대로 저장된다
    d["검증코드"] = 검증기록.코드
    return d, 검증기록.사유


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="허용 프로필 경로를 DB 에서 읽어 출력한다")
    ap.add_argument("--profile-paths", action="store_true", help="허용 프로필 경로 출력")
    a = ap.parse_args()
    if not a.profile_paths:
        ap.print_help()
        return
    for p in sorted(프로필_경로집합()):
        print(" ", p)


if __name__ == "__main__":
    main()
