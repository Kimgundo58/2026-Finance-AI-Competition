# -*- coding: utf-8 -*-
"""L3(기관 자체규정) 업로드 API — 파일 접수, 원본 저장, 파싱 상태 조회.

POST /api/l3/upload 는 tenant.l3_documents 행을 만들고 원본을 저장한 뒤 202 를 반환하고,
파싱은 백그라운드 작업(`파싱_배경` → scripts/l3_parse.파싱)으로 실행한다. 프론트는
GET /api/l3/{doc_id} 를 폴링하고, GET /api/l3/current 로 적용 중인 문서 목록을 본다.
허용 형식은 PDF·HWPX·HWP 이고 파일 내용으로 형식을 확인한다. DOC·DOCX 는 415.
파싱이 성공하면 이 기관의 tenant.judge_cache 행을 지운다(L3 가 바뀐 뒤 캐시가 답하지 않게).
"""
from __future__ import annotations

import io
import logging
import uuid
import zipfile

from fastapi import (APIRouter, BackgroundTasks, File, Form, HTTPException,
                     Request, UploadFile)

from ._common import DSN, MOCK, _org조건, _질의, _실행
from .models import L3업로드응답, L3현재문서, L3현재문서목록응답
from . import auth, mock_data

_log = logging.getLogger("suddoe.l3")

router = APIRouter(prefix="/api/l3", tags=["L3 업로드"])

허용_확장자 = {"pdf", "hwpx", "hwp"}
거부_확장자 = {"doc", "docx"}          # 파서가 없다
최대_바이트 = 30 * 1024 * 1024

_접수_메시지 = "접수했습니다. 조문 분해가 끝나면 상태가 「완료」로 바뀝니다."


def 실제형식(본문: bytes) -> str:
    """파일 내용으로 실제 형식을 판별한다. 확장자는 신뢰하지 않는다.

    ZIP 계열(HWPX·XLSX·DOCX·PPTX)은 매직바이트가 같아 내부 항목 이름으로 구분한다.
    OLE 헤더는 구형 HWP·DOC·XLS 가 공유하므로 'hwp' 판정이 확정은 아니다.
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


def _업로드_org(요청: Request, 요청_org_id: str | None) -> str:
    """이 업로드의 소유 org 를 서버가 정한다.

    검증된 주체(토큰)가 있으면 토큰의 org 가 우선한다. 멀티파트 Form 의 org_id 는 클라이언트가
    보낸 검증되지 않은 값이고, 미들웨어의 쿼리스트링 교체도 적용되지 않는다. 토큰이 없으면
    Form 값을 쓰되 SUDDOE_ORG_PARAM=0 이면 401 로 거부한다. Form 값의 UUID 형식은 여기서
    검사하지 않는다(잘못된 값은 INSERT 단계에서 400 이 된다).
    """
    소유_org = auth.검증된_org(요청)
    if 소유_org:
        if 요청_org_id and str(요청_org_id) != 소유_org:
            _log.warning("L3 업로드 org_id 불일치 — 토큰=%s Form=%s · 토큰을 쓴다",
                         소유_org, 요청_org_id)
        return 소유_org
    # 프론트는 org_id 를 모르므로 Form 값이 없는 게스트 요청도 여기로 온다
    if not auth.ORG_PARAM_허용 or not 요청_org_id:
        raise HTTPException(401, "기관을 확인할 수 없습니다 — 로그인이 필요합니다.")
    return 요청_org_id


@router.post("/upload", response_model=L3업로드응답, status_code=202)
def 업로드(
    요청: Request,
    배경: BackgroundTasks,
    파일: UploadFile = File(...),
    # 선택 필드다. 로그인 상태면 _업로드_org 가 토큰의 org 로 대체한다
    org_id: str | None = Form(None),
    # 받기만 하고 저장하지 않는다. 기관명의 기준값은 tenant.orgs.기관명 이다
    기관명: str | None = Form(None),
) -> L3업로드응답:
    # 클라이언트가 보낸 org_id 가 아래 처리에 섞이지 않도록 맨 앞에서 확정한다
    org_id = _업로드_org(요청, org_id)

    이름 = 파일.filename or ""
    확장 = 이름.rsplit(".", 1)[-1].lower() if "." in 이름 else ""

    if 확장 in 거부_확장자:
        raise HTTPException(415, f".{확장} 은 지원하지 않습니다. PDF·HWPX·HWP 로 올려 주세요.")
    if 확장 not in 허용_확장자:
        raise HTTPException(415, f"지원하지 않는 형식입니다 (.{확장 or '?'}). PDF·HWPX·HWP 만 받습니다.")

    # 최대 크기보다 1바이트만 더 읽는다. 전체를 메모리에 올린 뒤 크기를 보면 큰 업로드로 메모리가 고갈된다.
    본문 = 파일.file.read(최대_바이트 + 1)
    if not 본문:
        raise HTTPException(400, "빈 파일입니다.")
    if len(본문) > 최대_바이트:
        raise HTTPException(413, "파일이 너무 큽니다 (30MB 이하).")

    # 30MB 초과가 413 으로 먼저 걸리도록 크기·빈 파일 검사 다음에 내용을 본다
    진짜 = 실제형식(본문)
    if 진짜 != 확장:
        raise HTTPException(415, f"파일 내용이 .{확장} 이 아닙니다 (실제: {진짜}). "
                                 f"확장자만 바꾼 파일은 파싱할 수 없습니다.")

    if MOCK:
        return L3업로드응답(**{**mock_data.목_L3, "파일명": 이름, "확장자": 확장,
                            "doc_id": f"l3-mock-{uuid.uuid4().hex[:8]}"})
    응답 = _실_업로드(본문, 이름, 확장, org_id)
    # 202 응답 뒤 백그라운드로 파싱한다. org_id 는 파싱_배경 이 GUC app.org_id 를 설정하는 데 쓴다
    배경.add_task(파싱_배경, 응답.doc_id, org_id)
    return 응답


@router.get("/current", response_model=L3현재문서목록응답)
def 현재문서(org_id: str | None = None) -> L3현재문서목록응답:
    """org 에 지금 적용 중인(status='active') L3 문서 목록. org_id 가 없으면(게스트) 빈 목록이다.

    `/{doc_id}` 보다 먼저 등록해야 한다. 그렇지 않으면 'current' 가 doc_id 경로 파라미터로 매칭된다.
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
    """업로드한 문서의 파싱 상태. 프론트가 '분석 중' 표시를 하며 폴링한다."""
    if MOCK:
        # dangling 이 있는 완료본을 돌려준다. 프론트가 실패 안내 UI 를 그릴 수 있게 한다
        return L3업로드응답(**{**mock_data.목_L3_dangling, "doc_id": doc_id})
    return _실_상태(doc_id, org_id)


# ════════════════════════════════════════════════════════════════════
# DB 모드 구현
# ════════════════════════════════════════════════════════════════════

# 확장자 → l3_documents.extraction 값. pdf 는 'native'(pdftext.py 추출)로 기록한다
_확장_추출방식 = {"hwpx": "hwpx", "hwp": "hwp"}


def _실_업로드(본문: bytes, 파일명: str, 확장: str, org_id: str) -> L3업로드응답:
    """tenant.l3_documents 행을 만들고 원본을 저장한 뒤 상태='파싱대기' 응답을 만든다.

    파서는 여기서 부르지 않는다. 저장 경로는 scripts/l3_parse 의 `원본경로()` 규칙을 그대로
    쓴다(파서가 같은 경로에서 읽는다). 파일 저장이 실패하면 방금 만든 행을 지우고 500.
    """
    # 지연 import: mock 모드와 서버 기동 시에는 파서 의존성을 읽지 않는다
    from l3_parse import L3_저장소, 원본경로        # scripts/ — sys.path 는 main.py 가 잡는다

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

    # 경로가 doc_id 에서 나오므로 INSERT 다음에 쓴다
    try:
        L3_저장소.mkdir(parents=True, exist_ok=True)
        원본경로(doc_id, 확장).write_bytes(본문)
    except OSError as e:
        _실행("DELETE FROM tenant.l3_documents WHERE doc_id = %s", (doc_id,))
        raise HTTPException(500, f"원본을 저장하지 못했습니다 ({type(e).__name__}).") from e

    return L3업로드응답(
        doc_id=doc_id, 파일명=파일명, 확장자=확장, 상태="파싱대기",
        조_건수=None, dangling=[], 메시지=_접수_메시지,
    )


def 파싱_배경(doc_id: str, org_id: str) -> None:
    """업로드 응답 뒤 백그라운드 작업으로 파서를 실행한다(202 + 상태 폴링 계약).

    `_common` 헬퍼를 거치지 않는 커넥션이라 GUC app.org_id 를 직접 설정한다. 설정하지 않으면
    RLS 정책 때문에 0행이 된다. 파싱이 성공하면 같은 트랜잭션에서 이 기관의 judge_cache 를 지운다.
    예외를 밖으로 던지지 않는다. 실패는 파싱품질='fail' 로 기록해 상태가 '대기' 로 남지 않게 한다.
    """
    try:
        import psycopg

        from l3_parse import 파싱                      # scripts/ — sys.path 는 main.py 가 잡는다
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
            with conn.cursor() as cur:
                결과 = 파싱(cur, doc_id)
                if 결과.get("ok", True):
                    # L3 가 바뀌면 이 기관의 판정 캐시는 낡은 근거로 답할 수 있다. 같은 트랜잭션에서 지운다.
                    cur.execute("DELETE FROM tenant.judge_cache WHERE org_id = %s", (str(org_id),))
            conn.commit()
        if not 결과.get("ok", True):
            # 파싱() 은 실패도 예외 없이 dict 로 돌려준다. 여기서 'fail' 로 기록한다
            _log.error("L3 파싱 실패(예외 아님) doc_id=%s 사유=%s", doc_id, 결과.get("사유"))
            _실행('UPDATE tenant.l3_documents SET "파싱품질" = %s WHERE doc_id = %s',
                 ("fail", doc_id))
    except Exception as e:                              # noqa: BLE001
        _log.exception("L3 파싱 배경 작업 실패 doc_id=%s", doc_id)
        _실행('UPDATE tenant.l3_documents SET "파싱품질" = %s WHERE doc_id = %s',
             ("fail", doc_id))
        _log.error("  → 파싱품질을 fail 로 기록했다 (%s)", type(e).__name__)


def _상태_메시지(파싱품질: str | None, 조_건수: int, dangling수: int | None
              ) -> tuple[str, int | None, str]:
    """파싱품질과 조문 건수로 (상태, 조_건수, 메시지) 를 정한다. DB 를 읽지 않는 순수 함수다."""
    if 파싱품질 == "fail":
        return "실패", None, "파싱에 실패했습니다. 다시 업로드해 주세요."
    if 조_건수:
        메시지 = f"{조_건수}개 조를 등록했습니다."
        if dangling수:
            메시지 += f" 참조 {dangling수}건은 상위 규범을 찾지 못했습니다."
        # 조문은 뽑혔지만 품질이 낮다. 상태는 '완료'로 두고 메시지로만 알린다
        if 파싱품질 == "warn":
            메시지 += " (품질 확인 필요 — 조문 인식이 형식과 어긋났을 수 있습니다.)"
        return "완료", 조_건수, 메시지
    if 파싱품질 == "warn":
        # 파서는 실행됐지만 조문이 0건이다. 아직 처리 전인 경우와 원인이 달라 메시지를 나눈다
        return "파싱대기", None, "파싱을 시도했지만 조문을 뽑아내지 못했습니다. 확인이 필요합니다."
    return "파싱대기", None, _접수_메시지


def _실_상태(doc_id: str, org_id: str | None) -> L3업로드응답:
    """l3_documents 와 l3_articles 건수로 상태(완료/실패/파싱대기)를 정해 반환한다.

    dangling 상세(조·참조·사유)는 저장하는 테이블이 없어 항상 빈 배열이다.
    `_org조건()` 을 써서 게스트(org_id 없음)는 0행, 즉 404 가 된다.
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
    상태값, 조_건수, 메시지 = _상태_메시지(파싱품질, 개수[0][0] if 개수 else 0, dangling수)
    return L3업로드응답(doc_id=doc_id, 파일명=파일명, 확장자=확장, 상태=상태값,
                        조_건수=조_건수, dangling=[], 메시지=메시지, 파싱품질=파싱품질)
