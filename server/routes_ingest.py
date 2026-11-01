# -*- coding: utf-8 -*-
"""L1·L2 규정 적재 관리 API. 접두사 /admin/documents.

호출자: 운영자(관리자 토큰). server/main.py 가 app.include_router(routes_ingest.router) 로 붙인다.
처리는 scripts/corpus_ingest.py 가 하고, 이 모듈은 인증·입력 검증·업로드 저장·HTTP 상태 코드만 맡는다.
계약:
  POST   /admin/documents                  multipart(파일 + 메타데이터) -> 202 {doc_id}. 파싱은 백그라운드
  GET    /admin/documents?status=&limit=   업로드·문서 목록
  GET    /admin/documents/{doc_id}         상태(parsing|staged|active|failed|superseded), 품질, 경고, 샘플
  POST   /admin/documents/{doc_id}/activate  staged -> active, 이전 판 -> superseded (한 트랜잭션)
  DELETE /admin/documents/{doc_id}         staged·failed·parsing 업로드 폐기
불변식: 모든 경로는 관리자 토큰(server.auth.관리자_확인)을 먼저 검사한다. DB 는 SUDDOE_INGEST_DSN(롤 suddoe_ingest)
으로만 쓰고, 값이 없으면 모든 경로가 503 이다. 앱 DSN(suddoe_app)은 corpus 에 SELECT 권한만 있다.
layer 는 메타데이터로만 정하고 파일명은 보지 않는다. 파일 형식은 내용으로 판별한다.
이름을 바꾼 응답 필드는 한 릴리스 동안 이전 이름의 읽기 전용 필드(computed_field)로도 내보낸다.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import (BaseModel, ConfigDict, Field, ValidationError, computed_field, field_validator,
                      model_validator)
from starlette.datastructures import UploadFile

# 단독 import(테스트) 때도 scripts/ 의 파이프라인 모듈을 찾도록 경로를 건다. main.py 도 같은 경로를 건다.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import corpus_ingest  # noqa: E402
from server import auth, response_cache  # noqa: E402

_log = logging.getLogger("suddoe.routes_ingest")

_복사_단위 = 1024 * 1024
_요청_여유 = 1024 * 1024        # multipart 경계·메타데이터 필드 몫


def _적재_dsn() -> str:
    """적재 전용 DSN. 요청마다 환경 변수를 읽는다. 없으면 앱 DSN 으로 대신하지 않고 503 이다."""
    dsn = os.environ.get("SUDDOE_INGEST_DSN", "").strip()
    if not dsn:
        raise HTTPException(503, "SUDDOE_INGEST_DSN 이 설정되지 않아 규정 적재 API 를 쓸 수 없습니다. "
                                 "docs/8_운영/8-7_규정_적재_API.md 를 참고하세요.")
    return dsn


router = APIRouter(prefix="/admin/documents", tags=["관리·규정 적재"],
                   dependencies=[Depends(auth.관리자_확인)])


# 입력·응답 모델

class 적재메타(BaseModel):
    """업로드와 함께 오는 메타데이터. layer 는 여기서만 정한다."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    layer: Literal["L1", "L2"]
    발행기관: str = Field(min_length=2, max_length=60)
    문서명: str = Field(min_length=2, max_length=80)
    사업명: str | None = Field(default=None, max_length=60)
    시행일: date
    version: str | None = Field(default=None, max_length=40)
    적용대상: Literal["공통", "창업기업", "주관기관"] = "공통"

    @field_validator("문서명")
    @classmethod
    def _문서명_문자(cls, v: str) -> str:
        # doc_id 가 URL 경로의 한 세그먼트가 되고 인용 출처로 화면에 나온다
        if re.search(r"[/\\\x00-\x1f\x7f]", v):
            raise ValueError("문서명에 / \\ 나 제어 문자를 쓸 수 없습니다.")
        return v

    @field_validator("사업명", "version")
    @classmethod
    def _빈값_없음(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _사업명_규칙(self) -> "적재메타":
        if self.layer == "L2" and not self.사업명:
            raise ValueError("L2 문서는 사업명이 필요합니다.")
        if self.layer == "L1" and self.사업명:
            raise ValueError("L1 문서에는 사업명을 넣지 않습니다.")
        return self


class 접수응답(BaseModel):
    doc_id: str
    상태: Literal["parsing"] = "parsing"
    파일형식: str
    판별결과: str
    안내: str

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 파일정체(self) -> str:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.판별결과


class 문서요약(BaseModel):
    doc_id: str
    상태: str | None = None
    layer: str | None = None
    문서명: str | None = None
    사업명: str | None = None
    시행일: date | None = None
    parse_quality: str | None = None
    실패사유: str | None = None
    등록일시: datetime | None = None


class 문서목록응답(BaseModel):
    건수: int
    문서: list[문서요약]


class 조문샘플(BaseModel):
    조번호: str
    조제목: str | None = None
    본문: str


class 문서상태응답(BaseModel):
    doc_id: str
    상태: str | None = None
    layer: str | None = None
    문서명: str | None = None
    발행기관: str | None = None
    사업명: str | None = None
    시행일: date | None = None
    version: str | None = None
    적용대상: str | None = None
    원본파일명: str | None = None
    파일형식: str | None = None
    parse_quality: str | None = None
    retrieval_scope: str | None = None
    분할전략: str | None = None
    조_건수: int = 0
    삭제조_건수: int = 0
    청크_건수: int = 0
    임베딩_누락: int = 0
    참조_건수: int = 0
    미해결참조_건수: int = 0
    경고: list[str] = []
    실패사유: str | None = None
    이전판_doc_id: str | None = None
    샘플: list[조문샘플] = []
    등록일시: datetime | None = None
    갱신일시: datetime | None = None

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 분해전략(self) -> str | None:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.분할전략

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 끊긴참조_건수(self) -> int:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.미해결참조_건수

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 대체한_doc_id(self) -> str | None:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.이전판_doc_id


class 활성화요청(BaseModel):
    """이전 판을 자동으로 찾지 못할 때 대체_doc_id 나 신규문서 중 하나를 지정한다.

    신규문서인데 같은 사업·비슷한 이름의 active 문서가 있으면 409 다. 그 문서와 함께 검색돼야 하는
    별개 문서임을 확인했을 때만 중복_허용=true 를 준다.
    """
    model_config = ConfigDict(extra="forbid")

    대체_doc_id: str | None = None
    신규문서: bool = False
    중복_허용: bool = False


class 활성화응답(BaseModel):
    doc_id: str
    상태: str
    이전판_doc_id: str | None = None
    retrieval_scope: str
    이전판_청크_삭제: int
    판정캐시_삭제: int
    경고: list[str] = []

    @computed_field  # 구 이름 호환(제거 예정: 다음 릴리스)
    @property
    def 대체한_doc_id(self) -> str | None:  # 구 이름 호환(제거 예정: 다음 릴리스)
        return self.이전판_doc_id


class 삭제응답(BaseModel):
    doc_id: str
    이전상태: str | None = None
    청크_삭제: int
    참조_삭제: int


# DB 연결과 오류 변환

@contextmanager
def _db(dsn: str):
    """적재 DSN 연결. 파이프라인 예외와 DB 오류를 HTTP 상태 코드로 바꾼다."""
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            yield conn
    except corpus_ingest.문서없음 as e:
        raise HTTPException(404, str(e)) from e
    except corpus_ingest.요청거부 as e:
        raise HTTPException(409, {"사유": e.사유, "후보": e.후보}) from e
    except corpus_ingest.입력오류 as e:
        raise HTTPException(422, str(e)) from e
    except (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedColumn) as e:
        _log.error("규정 적재 DB 권한·스키마 오류: %s", e)
        raise HTTPException(503, "적재용 DB 권한이나 테이블이 없습니다. "
                                 "db/migrations/2026-11-01_corpus_ingest.sql 적용 여부를 확인하세요.") from e
    except psycopg.errors.LockNotAvailable as e:
        raise HTTPException(503, "다른 작업이 문서를 잠그고 있습니다. 잠시 뒤 다시 시도하세요.") from e
    except psycopg.OperationalError as e:
        _log.error("규정 적재 DB 연결 실패: %s", e)
        raise HTTPException(503, "적재용 DB 에 연결할 수 없습니다.") from e


# 업로드

_업로드_스키마 = {
    "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object",
        "required": ["파일", "layer", "발행기관", "문서명", "시행일"],
        "properties": {
            "파일": {"type": "string", "format": "binary",
                   "description": "PDF·HWP·HWPX·DOCX·법령 XML, 30MB 이하"},
            "layer": {"type": "string", "enum": ["L1", "L2"]},
            "발행기관": {"type": "string"},
            "문서명": {"type": "string", "description": "doc_id = 문서명_YYYYMMDD(시행일)"},
            "사업명": {"type": "string", "description": "L2 만. corpus.programs 의 사업명이나 별칭"},
            "시행일": {"type": "string", "format": "date"},
            "version": {"type": "string", "description": "생략하면 시행일로 만든다"},
            "적용대상": {"type": "string", "enum": ["공통", "창업기업", "주관기관"], "default": "공통"},
        }}}}},
}


@router.post("", response_model=접수응답, status_code=202, openapi_extra=_업로드_스키마)
async def 업로드(요청: Request, 백그라운드: BackgroundTasks, dsn: str = Depends(_적재_dsn)) -> 접수응답:
    """파일을 저장하고 접수한 뒤 202 를 돌려준다. 파싱·임베딩·저장은 응답 뒤 백그라운드에서 돈다.

    본문을 읽기 전에 Content-Length 로 크기를 거른다. 실제 복사도 바이트를 세며 상한을 넘으면 멈춘다.
    """
    길이 = 요청.headers.get("content-length")
    if not 길이 or not 길이.isdigit():
        raise HTTPException(411, "Content-Length 가 필요합니다.")
    if int(길이) > corpus_ingest.최대_바이트 + _요청_여유:
        raise HTTPException(413, "파일이 너무 큽니다 (30MB 이하).")

    폼 = await 요청.form(max_files=1, max_fields=20)
    try:
        파일 = 폼.get("파일")
        if not isinstance(파일, UploadFile):
            raise RequestValidationError([{"type": "missing", "loc": ("body", "파일"),
                                           "msg": "파일 필드가 필요합니다.", "input": None}])
        값 = {k: v for k, v in 폼.items() if k != "파일"}
        try:
            메타 = 적재메타.model_validate(값)
        except ValidationError as e:
            raise RequestValidationError(
                [{**err, "loc": ("body", *err["loc"])}
                 for err in e.errors(include_url=False, include_context=False)]) from e

        원본파일명 = Path(파일.filename or "upload").name[:200]
        저장소 = corpus_ingest.저장소()
        임시 = 저장소 / f"{uuid.uuid4().hex}.upload"
        try:
            크기 = await run_in_threadpool(_원본_저장, 파일, 임시)
        except BaseException:
            임시.unlink(missing_ok=True)       # 디스크 부족·I/O 오류로 중간까지 쓴 파일을 남기지 않는다
            raise
    finally:
        await 폼.close()

    try:
        if 크기 == 0:
            raise HTTPException(400, "빈 파일입니다.")
        if 크기 > corpus_ingest.최대_바이트:
            raise HTTPException(413, "파일이 너무 큽니다 (30MB 이하).")
        형식, 판별결과 = await run_in_threadpool(corpus_ingest.파일형식, 임시)
        if 형식 is None:
            raise HTTPException(415, f"지원하지 않는 파일입니다 (판별 결과: {판별결과}). "
                                     "PDF·HWP·HWPX·DOCX·법령 XML 만 받습니다. 배포용·암호화 HWP 는 읽을 수 없습니다.")
        저장경로 = 임시.with_suffix(f".{형식}")      # text_extract 는 확장자로 추출기를 고른다
        임시.rename(저장경로)
        임시 = 저장경로
        doc_id = await run_in_threadpool(_접수, dsn, 메타.model_dump(), 원본파일명, 형식, 저장경로)
    except BaseException:
        임시.unlink(missing_ok=True)
        raise

    백그라운드.add_task(corpus_ingest.적재_실행, dsn, doc_id)
    _log.info("규정 업로드 접수 doc_id=%s layer=%s 형식=%s 크기=%d", doc_id, 메타.layer, 형식, 크기)
    return 접수응답(doc_id=doc_id, 파일형식=형식, 판별결과=판별결과,
                   안내="파싱 중입니다. GET /admin/documents/{doc_id} 로 상태를 확인하세요.")


def _원본_저장(파일: UploadFile, 경로: Path) -> int:
    """업로드를 1MB 단위로 디스크에 복사하며 바이트를 센다. 상한을 넘으면 즉시 멈춘다."""
    경로.parent.mkdir(parents=True, exist_ok=True)
    크기 = 0
    파일.file.seek(0)
    with 경로.open("wb") as out:
        while 조각 := 파일.file.read(_복사_단위):
            크기 += len(조각)
            if 크기 > corpus_ingest.최대_바이트:
                break
            out.write(조각)
    return 크기


def _접수(dsn: str, 메타: dict, 원본파일명: str, 형식: str, 저장경로: Path) -> str:
    with _db(dsn) as conn:
        return corpus_ingest.접수(conn, 메타, 원본파일명=원본파일명, 형식=형식, 저장경로=저장경로)


# 조회·활성화·삭제

@router.get("", response_model=문서목록응답)
def 목록(status: Literal["parsing", "staged", "active", "failed", "superseded"] | None = None,
       limit: int = Query(100, ge=1, le=500),
       dsn: str = Depends(_적재_dsn)) -> 문서목록응답:
    with _db(dsn) as conn:
        행 = corpus_ingest.문서_목록(conn, status, limit)
    return 문서목록응답(건수=len(행), 문서=[문서요약(**r) for r in 행])


@router.get("/{doc_id}", response_model=문서상태응답)
def 상태(doc_id: str, dsn: str = Depends(_적재_dsn)) -> 문서상태응답:
    with _db(dsn) as conn:
        return 문서상태응답(**corpus_ingest.문서_상태(conn, doc_id))


@router.post("/{doc_id}/activate", response_model=활성화응답)
def 활성화(doc_id: str, 본문: 활성화요청 | None = None,
         dsn: str = Depends(_적재_dsn)) -> 활성화응답:
    """staged 문서를 active 로 올린다. 이전 판을 정할 수 없으면 409 와 후보 목록을 돌려준다."""
    본문 = 본문 or 활성화요청()
    with _db(dsn) as conn:
        결과: dict[str, Any] = corpus_ingest.활성화(
            conn, doc_id, 대체_doc_id=본문.대체_doc_id, 신규문서=본문.신규문서,
            중복_허용=본문.중복_허용)
    # 활성화는 judge_cache 를 비우지만, 이 프로세스가 들고 있는 데이터 버전 해시(최대 300초)도 지워야
    # 이후 요청이 새 해시로 조회·저장한다.
    response_cache.데이터버전_해시_무효화()
    _log.info("규정 활성화 doc_id=%s 이전판=%s", doc_id, 결과.get("이전판_doc_id"))
    return 활성화응답(**결과)


@router.delete("/{doc_id}", response_model=삭제응답)
def 삭제(doc_id: str, dsn: str = Depends(_적재_dsn)) -> 삭제응답:
    with _db(dsn) as conn:
        결과 = corpus_ingest.삭제(conn, doc_id)
    _log.info("규정 업로드 삭제 doc_id=%s 이전상태=%s", doc_id, 결과["이전상태"])
    return 삭제응답(**결과)
