# -*- coding: utf-8 -*-
"""L3 기관 규정 업로드 API — 파일 접수·저장·상태 조회만 담당한다. 파싱은 별도 파서가 붙는다.

확장자: PDF · HWPX · HWP 만 허용. DOC·DOCX 는 415 로 거부한다 (파서가 없다).
"""
from __future__ import annotations

import io
import logging
import os
import uuid
import zipfile
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, File, Form, HTTPException,
                     Request, UploadFile)

from ._common import DSN, MOCK, ROOT, _질의, _실행
from .models import L3업로드응답, L3현재문서, L3현재문서목록응답
from .routes_plans import _org조건
from . import auth, mock_data

_log = logging.getLogger("suddoe.l3")

router = APIRouter(prefix="/api/l3", tags=["L3 업로드"])

허용_확장자 = {"pdf", "hwpx", "hwp"}
거부_확장자 = {"doc", "docx"}          # 파서가 없다
최대_바이트 = 30 * 1024 * 1024


def 실제형식(본문: bytes) -> str:
    """파일 내용으로 실제 형식을 판별한다 — 확장자는 신뢰하지 않는다.

    ZIP 계열(HWPX·XLSX·DOCX·PPTX)은 매직바이트가 같아 내부 항목 이름으로 구분한다.
    OLE 헤더는 구형 HWP·DOC·XLS 가 공유해 hwp 로 판정해도 확정은 아니다.
    """
    if 본문[:5] == b"%PDF-":
        return "pdf"
    if 본문[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "hwp"                       # OLE 복합문서 (구형 HWP·DOC·XLS 공통)
    if 본문[:4] == b"PK\x03\x04":
        try:
            이름들 = zipfile.ZipFile(io.BytesIO(본문)).namelist()
        except (zipfile.BadZipFile, OSError):
            return "손상된zip"
        if any(n.startswith("Contents/") for n in 이름들):
            return "hwpx"
        if any(n.startswith("xl/") for n in 이름들):
            return "xlsx"
        if any(n.startswith("word/") for n in 이름들):
            return "docx"
        if any(n.startswith("ppt/") for n in 이름들):
            return "pptx"
        return "zip"
    return "알수없음"


def _업로드_주인(요청: Request, 자기신고: str | None) -> str:
    """이 업로드가 어느 기관 것인지 서버가 정한다.

    로그인 토큰이 있으면 토큰의 org_id 가 우선한다 — 멀티파트 Form 필드의 org_id 는
    위조 가능한 자기신고라 미들웨어의 쿼리스트링 보정이 닿지 않는다. 토큰이 없으면
    자기신고를 그대로 쓰되 SUDDOE_ORG_PARAM 스위치로 막을 수 있다. 자기신고의 UUID
    형식은 여기서 검사하지 않는다 — 잘못된 값은 INSERT 단계에서 400 으로 걸린다.
    """
    주 = 요청.scope.get("suddoe_주체")
    if 주 is not None and 주.검증됨 and 주.org_id:
        if 자기신고 and str(자기신고) != str(주.org_id):
            # 자기신고와 토큰 org 가 다르면 기록만 남기고 토큰을 쓴다
            _log.warning("L3 업로드 org_id 불일치 — 토큰=%s Form=%s · 토큰을 쓴다",
                         주.org_id, 자기신고)
        return str(주.org_id)                      # 토큰이 이긴다
    # 자기신고가 없어도 여기로 온다 — 프론트는 org_id 를 모르기 때문이다
    if not auth.ORG_PARAM_허용 or not 자기신고:
        raise HTTPException(401, "기관을 확인할 수 없습니다 — 로그인이 필요합니다.")
    return 자기신고


@router.post("/upload", response_model=L3업로드응답, status_code=202)
async def 업로드(
    요청: Request,
    배경: BackgroundTasks,
    파일: UploadFile = File(...),
    # 선택 필드다 — 로그인 시 _업로드_주인 이 토큰의 org_id 로 덮어쓴다
    org_id: str | None = Form(None),
    기관명: str | None = Form(None),
) -> L3업로드응답:
    # 자기신고 값이 아래 검사에 섞이지 않도록 맨 앞에서 확정한다
    org_id = _업로드_주인(요청, org_id)

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

    # 크기·빈파일 검사 다음에 내용을 본다 — 30MB 초과가 413 으로 먼저 걸리게 하는 순서다
    진짜 = 실제형식(본문)
    if 진짜 != 확장:
        raise HTTPException(415, f"파일 내용이 .{확장} 이 아닙니다 (실제: {진짜}). "
                                 f"확장자만 바꾼 파일은 파싱할 수 없습니다.")

    if MOCK:
        return L3업로드응답(**{**mock_data.목_L3, "파일명": 이름, "확장자": 확장,
                            "doc_id": f"l3-mock-{uuid.uuid4().hex[:8]}"})
    응답 = _실_업로드(본문, 이름, 확장, org_id, 기관명)
    # 202 응답 뒤 백그라운드로 파싱한다 — org_id 는 파싱_배경 이 RLS GUC 를 세우는 데 쓴다
    배경.add_task(파싱_배경, 응답.doc_id, org_id)
    return 응답


@router.get("/current", response_model=L3현재문서목록응답)
def 현재문서(org_id: str | None = None) -> L3현재문서목록응답:
    """org 에 지금 적용 중인 L3 문서 목록을 준다.

    `/{doc_id}` 보다 먼저 등록해야 한다 — 아니면 FastAPI 가 doc_id="current" 로 먹는다.
    status='active' 인 것만 준다. org_id 가 없으면(게스트) 빈 목록이다.
    """
    if MOCK:
        return L3현재문서목록응답(문서=[])
    조건, org인자 = _org조건(org_id, "d")
    행 = _질의(
        f'SELECT d.doc_id, d."원본파일명", d.version, d."시행일", d.created_at, d."파싱품질", '
        f'       (SELECT count(*) FROM tenant.l3_articles a WHERE a.doc_id = d.doc_id) '
        f'  FROM tenant.l3_documents d '
        f' WHERE {조건} AND d.status = \'active\' '
        f' ORDER BY d.created_at DESC',
        org인자,
    )
    return L3현재문서목록응답(문서=[
        L3현재문서(doc_id=str(doc_id), 원본파일명=이름, version=버전,
                시행일=시행일.isoformat() if 시행일 else None,
                등록일=등록.date().isoformat() if 등록 else None,
                파싱품질=파싱품질, 조_건수=건수)
        for doc_id, 이름, 버전, 시행일, 등록, 파싱품질, 건수 in 행])


@router.get("/{doc_id}", response_model=L3업로드응답)
def 상태(doc_id: str, org_id: str | None = None) -> L3업로드응답:
    """프론트가 「분석 중」 스피너를 돌리며 폴링하는 경로."""
    if MOCK:
        # 목에서는 dangling 이 있는 완료본을 돌려준다 — 프론트가 실패 안내 UI 를 그려야 한다
        return L3업로드응답(**{**mock_data.목_L3_dangling, "doc_id": doc_id})
    return _실_상태(doc_id, org_id)


# L3 실 경로 구역

# 확장자→extraction 라벨 매핑. pdf 는 여기 없고 'native'(pdftext.py 경로)로 처리한다
_확장_추출방식 = {"hwpx": "hwpx", "hwp": "hwp"}

# 업로드 원본 저장 위치 — DB 는 파일 경로를 안 갖고 doc_id 로 경로를 유도한다
L3_저장소 = Path(os.environ.get("SUDDOE_L3_DIR", str(ROOT / "_l3_업로드")))


def 원본경로(doc_id: str, 확장: str) -> Path:
    """파일 경로를 doc_id(서버 생성 uuid)로만 짓는다 — 사용자 파일명을 쓰면 경로 조작에 노출된다."""
    안전확장 = 확장 if 확장 in 허용_확장자 else "bin"
    return L3_저장소 / f"{uuid.UUID(str(doc_id))}.{안전확장}"


def _실_업로드(본문: bytes, 파일명: str, 확장: str,
             org_id: str, 기관명: str | None) -> L3업로드응답:
    """tenant.l3_documents 행을 만들고 원본을 저장한 뒤 상태='파싱대기' 로 202 를 반환한다.

    파서는 여기서 부르지 않는다. org_id 는 l3_articles 에도 저장돼 판정 검색의 기관
    경계로 쓰인다. 기관명은 저장하지 않는다 — tenant.orgs.기관명 이 기준이다.
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
    doc_id = str(행[0][0])

    # 경로가 doc_id 에서 나오므로 INSERT 뒤에 쓴다 — 저장 실패 시 행을 되돌린다
    try:
        L3_저장소.mkdir(parents=True, exist_ok=True)
        원본경로(doc_id, 확장).write_bytes(본문)
    except OSError as e:
        _실행("DELETE FROM tenant.l3_documents WHERE doc_id = %s", (doc_id,))
        raise HTTPException(500, f"원본을 저장하지 못했습니다 ({type(e).__name__}).") from e

    return L3업로드응답(
        doc_id=doc_id, 파일명=파일명, 확장자=확장, 상태="파싱대기",
        조_건수=None, dangling=[],
        메시지="접수했습니다. 조문 분해가 끝나면 상태가 「완료」로 바뀝니다.",
    )


def 파싱_배경(doc_id: str, org_id: str) -> None:
    """업로드 응답을 보낸 뒤 백그라운드로 파서를 태운다.

    동기로 부르면 안 된다 — 202 Accepted + 상태 폴링 계약이다. 이 연결은
    `_common` 의 헬퍼를 거치지 않는 생 연결이라 GUC app.org_id 를 직접 세운다 —
    없으면 RLS 가 0행으로 막는다. 여기서 예외를 밖으로 던지지 않는다 — 실패는
    파싱품질='fail' 로 닫아 상태가 '대기' 에 영원히 남지 않게 한다.
    """
    try:
        import psycopg

        from l3_parse import 파싱                      # scripts/ — sys.path 는 main.py 가 잡는다
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
            with conn.cursor() as cur:
                결과 = 파싱(cur, doc_id)
            conn.commit()
        if not 결과.get("ok", True):
            # 파싱() 은 실패도 예외 없이 dict 로 돌려준다 — 여기서 'fail' 로 닫는다
            _log.error("L3 파싱 실패(예외 아님) doc_id=%s 사유=%s", doc_id, 결과.get("사유"))
            _실행('UPDATE tenant.l3_documents SET "파싱품질" = %s WHERE doc_id = %s',
                 ("fail", doc_id))
    except Exception as e:                              # noqa: BLE001
        _log.exception("L3 파싱 배경 작업 실패 doc_id=%s", doc_id)
        # '대기' 로 남지 않도록 예외도 'fail' 로 닫는다
        _실행('UPDATE tenant.l3_documents SET "파싱품질" = %s WHERE doc_id = %s',
             ("fail", doc_id))
        _log.error("  → 파싱품질을 fail 로 닫았다 (%s)", type(e).__name__)


def _실_상태(doc_id: str, org_id: str | None) -> L3업로드응답:
    """l3_documents + l3_articles 건수로 상태(완료/실패/파싱대기)를 파생해 반환한다.

    dangling 상세(조·참조·사유)는 저장할 테이블이 없어 항상 빈 배열이다.
    org_id 비교는 `_org조건()` 을 써서 게스트(org_id 없음)가 0행이 되게 한다.
    """
    조건, org인자 = _org조건(org_id, "d")
    행 = _질의(
        f'SELECT d."원본파일명", d."파싱품질", d."dangling수" '
        f'FROM tenant.l3_documents d '
        f'WHERE d.doc_id = %s AND {조건}',
        (doc_id, *org인자),
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
        # 조문은 뽑혔지만 품질이 낮은 경우 — 상태는 '완료'를 유지하고 메시지로만 알린다
        if 파싱품질 == "warn":
            메시지 += " (품질 확인 필요 — 조문 인식이 형식과 어긋났을 수 있습니다.)"
    elif 파싱품질 == "warn":
        # 파서는 돌았지만 조문 0건인 경우 — '아직 안 봤다'와 원인이 달라 메시지로 구분한다
        상태, 조_건수 = "파싱대기", None
        메시지 = "파싱을 시도했지만 조문을 뽑아내지 못했습니다. 확인이 필요합니다."
    else:
        상태, 조_건수 = "파싱대기", None
        메시지 = "접수했습니다. 조문 분해가 끝나면 상태가 「완료」로 바뀝니다."

    return L3업로드응답(doc_id=doc_id, 파일명=파일명, 확장자=확장, 상태=상태,
                        조_건수=조_건수, dangling=[], 메시지=메시지, 파싱품질=파싱품질)
