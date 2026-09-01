# -*- coding: utf-8 -*-
"""L3 기관 규정 업로드 — 화면 4 온보딩 ③.   **[레인 C 소유]**

🔴 **이 레인은 파싱을 하지 않는다.** 파서(`scripts/hwp_extract.py` 등)는 다른 세션 소유이고
   이 세션의 훅이 `scripts/` 쓰기를 막는다. 여기서는 «접수 + 저장 + 상태» 까지만 한다.
   파싱은 뒤에 붙는다 — 그래서 202 Accepted 와 상태 폴링 경로를 둔다.

🔴 dangling 은 판정 시점이 아니라 **업로드 시점**에 알린다 (CLAUDE.md).
   그래서 응답에 자리를 만들어 둔다. 실제 채우기는 파서 연결 후다.

확장자: PDF · HWPX · HWP 만. DOC·DOCX 는 415 로 거부한다 (파서가 없다 — 만들지 않는다).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ._common import MOCK, _질의, _실행
from .models import L3업로드응답
from . import mock_data

router = APIRouter(prefix="/api/l3", tags=["L3 업로드"])

허용_확장자 = {"pdf", "hwpx", "hwp"}
거부_확장자 = {"doc", "docx"}          # 파서 없음. 프론트 온보딩 안내에서도 뺄 것
최대_바이트 = 30 * 1024 * 1024


@router.post("/upload", response_model=L3업로드응답, status_code=202)
async def 업로드(
    파일: UploadFile = File(...),
    org_id: str = Form(...),
    기관명: str | None = Form(None),
) -> L3업로드응답:
    이름 = 파일.filename or ""
    확장 = 이름.rsplit(".", 1)[-1].lower() if "." in 이름 else ""

    if 확장 in 거부_확장자:
        raise HTTPException(415, f".{확장} 은 지원하지 않습니다. PDF·HWPX·HWP 로 올려 주세요.")
    if 확장 not in 허용_확장자:
        raise HTTPException(415, f"지원하지 않는 형식입니다 (.{확장 or '?'}). PDF·HWPX·HWP 만 받습니다.")

    본문 = await 파일.read()
    if not 본문:
        raise HTTPException(400, "빈 파일입니다.")
    if len(본문) > 최대_바이트:
        raise HTTPException(413, "파일이 너무 큽니다 (30MB 이하).")

    if MOCK:
        return L3업로드응답(**{**mock_data.목_L3, "파일명": 이름, "확장자": 확장,
                            "doc_id": f"l3-mock-{uuid.uuid4().hex[:8]}"})
    return _실_업로드(본문, 이름, 확장, org_id, 기관명)


@router.get("/{doc_id}", response_model=L3업로드응답)
def 상태(doc_id: str, org_id: str | None = None) -> L3업로드응답:
    """프론트가 「분석 중」 스피너를 돌리며 폴링하는 경로."""
    if MOCK:
        # 목에서는 dangling 이 있는 완료본을 돌려준다 — 프론트가 실패 안내 UI 를 그려야 한다
        return L3업로드응답(**{**mock_data.목_L3_dangling, "doc_id": doc_id})
    return _실_상태(doc_id, org_id)


# ════════════════════════════════════════════════════════════════════
# 🔴 레인 C 작업 구역
# ════════════════════════════════════════════════════════════════════

_확장_추출방식 = {"hwpx": "hwpx", "hwp": "hwp"}   # pdf → 'native' (pdftext.py 경로)


def _실_업로드(본문: bytes, 파일명: str, 확장: str,
             org_id: str, 기관명: str | None) -> L3업로드응답:
    """tenant.l3_documents 행 생성 + 원본 저장 + 상태='파싱대기' 로 202.

    🔴 여기서 파서를 부르지 마라. `scripts/` 는 훅이 막고, 파싱은 다른 레인이 붙인다.
    🔴 org_id 는 l3_articles 에도 중복 저장된다 — 판정 검색이 기관을 못 넘게 하는 축이다.
       (그 INSERT 는 파서가 한다 — 이 레인은 l3_documents 접수까지다)
    🔴 `기관명` 은 저장하지 않는다 — `tenant.orgs.기관명` 이 정본이고 orgs 는 E 세션 소유다.
       (원본 파일 바이트도 이 함수 밖 — 저장소 경로는 파서 레인이 정한다)

    ✅ `파싱품질` CHECK 에 '대기' 가 추가됐다(2026-09-01 ai-25) — 업로드 시점엔 그대로
       '대기' 로 넣는다. 파서가 실제 파싱 후 UPDATE 로 pass/warn/fail 로 덮어쓴다.
    """
    추출방식 = _확장_추출방식.get(확장, "native")
    행 = _질의(
        """INSERT INTO tenant.l3_documents
               (org_id, "원본파일명", status, extraction, "파싱품질", "dangling수")
           VALUES (%s, %s, 'active', %s, '대기', 0)
           RETURNING doc_id""",
        (org_id, 파일명, 추출방식),
    )
    if not 행:
        raise HTTPException(400, "업로드를 저장하지 못했습니다 (org_id를 확인해 주세요).")
    return L3업로드응답(
        doc_id=str(행[0][0]), 파일명=파일명, 확장자=확장, 상태="파싱대기",
        조_건수=None, dangling=[],
        메시지="접수했습니다. 조문 분해가 끝나면 상태가 「완료」로 바뀝니다.",
    )


def _실_상태(doc_id: str, org_id: str | None) -> L3업로드응답:
    """`l3_documents` + `l3_articles` 건수로 상태를 파생한다.

    `파싱품질='fail'` → 실패. `l3_articles` 행이 있으면 완료. 둘 다 아니면 파싱대기 —
    다만 「아직 안 봤다」('대기')와 「평가는 했는데 조문을 못 뽑았다」('warn', 조_건수=0)
    는 원인이 다르므로 메시지로 구분한다. 🔴 API 로 나가는 상태 어휘(완료·실패·파싱대기)
    자체는 그대로다 — 파생 근거만 정확해지는 것이다 (2026-09-01 ai-14 후속 지시).
    dangling 상세(조·참조·사유)는 저장할 테이블이 아직 없다 — 자리만 두고 비워 둔다
    (`routes_l3.py` 상단 주석 · 실제 채우기는 파서 연결 후).
    """
    행 = _질의(
        """SELECT "원본파일명", "파싱품질", "dangling수"
           FROM tenant.l3_documents
           WHERE doc_id = %s AND (%s IS NULL OR org_id = %s)""",
        (doc_id, org_id, org_id),
    )
    if not 행:
        raise HTTPException(404, f"L3 문서 {doc_id} 을(를) 찾을 수 없습니다")
    파일명, 파싱품질, dangling수 = 행[0]
    확장 = 파일명.rsplit(".", 1)[-1].lower() if "." in (파일명 or "") else ""

    개수 = _질의('SELECT count(*) FROM tenant.l3_articles WHERE doc_id = %s', (doc_id,))
    조_건수 = 개수[0][0] if 개수 else 0

    if 파싱품질 == "fail":
        상태, 조_건수 = "실패", None
        메시지 = "파싱에 실패했습니다. 다시 업로드해 주세요."
    elif 조_건수:
        상태 = "완료"
        메시지 = f"{조_건수}개 조를 등록했습니다."
        if dangling수:
            메시지 += f" 참조 {dangling수}건은 상위 규범을 찾지 못했습니다."
    elif 파싱품질 == "warn":
        # 파서가 이미 한 번 돌았고(조문 0건) 품질을 낮게 평가한 경우다 — "아직 안
        # 봤다"(대기)와는 원인이 다르다. 상태 어휘는 그대로 「파싱대기」지만 메시지로
        # 갈라준다.
        상태, 조_건수 = "파싱대기", None
        메시지 = "파싱을 시도했지만 조문을 뽑아내지 못했습니다. 확인이 필요합니다."
    else:
        상태, 조_건수 = "파싱대기", None
        메시지 = "접수했습니다. 조문 분해가 끝나면 상태가 「완료」로 바뀝니다."

    return L3업로드응답(doc_id=doc_id, 파일명=파일명, 확장자=확장, 상태=상태,
                        조_건수=조_건수, dangling=[], 메시지=메시지)
