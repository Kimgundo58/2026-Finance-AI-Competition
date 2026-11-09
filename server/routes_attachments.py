# -*- coding: utf-8 -*-
"""지출계획 첨부파일(견적서·증빙 등). 보관용이며 파싱하거나 LLM 에 넘기지 않는다.

POST/GET/DELETE /api/plans/{plan_id}/attachments. 목록은 계획 상세의 `첨부` 로 준다(routes_plans._live_상세).
파일은 SUDDOE_ATTACH_DIR(운영은 compose 볼륨 plan_attachments)에 attachment_id 이름으로 저장하고,
원본 파일명은 DB 에만 둔다. 내려받기는 항상 attachment 로 준다(브라우저가 열어 실행하지 않게).
소유 확인은 계획의 org·계정으로 하며, 다른 계정의 파일은 404 다.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from ._common import MOCK, 작성자표시, 트랜잭션, 현재_account, 현재_본인, 현재_org, _계정조건, _정수_환경변수, _질의
from .file_format import 실제형식
from .models import 첨부항목
from . import mock_data

router = APIRouter(tags=["첨부파일"])
_log = logging.getLogger(__name__)

허용_확장자 = {"pdf", "hwp", "hwpx", "jpg", "png", "xlsx", "docx"}
_확장자_별칭 = {"jpeg": "jpg"}
_허용_안내 = "PDF·HWP·HWPX·JPG·PNG·XLSX·DOCX 만 올릴 수 있습니다."
최대_바이트 = 3 * 1024 * 1024
계획당_최대 = 10
_없음 = "첨부파일을 찾을 수 없습니다"
_다운로드_헤더 = {"X-Content-Type-Options": "nosniff"}


def _저장폴더() -> Path:
    기본 = Path(__file__).resolve().parent.parent / "_plan_attachments"
    폴더 = Path(os.environ.get("SUDDOE_ATTACH_DIR") or 기본)
    폴더.mkdir(parents=True, exist_ok=True)
    return 폴더


def _경로(attachment_id: str) -> Path:
    # attachment_id 는 서버가 만든 UUID 다. 경로 파라미터를 그대로 쓰기 전에 형식을 확인한다.
    return _저장폴더() / str(uuid.UUID(str(attachment_id)))


def _여유_확인() -> None:
    최소 = _정수_환경변수("SUDDOE_ATTACH_MIN_FREE_MB", 2048) * 1024 * 1024
    if shutil.disk_usage(_저장폴더()).free < 최소:
        _log.error("첨부 거부: 디스크 여유 부족 (최소 %d MB)", 최소 // (1024 * 1024))
        raise HTTPException(507, "서버 저장 공간이 부족해 올리지 못했습니다. 담당자에게 알려 주세요.")


def _파일_검사(파일: UploadFile) -> tuple[str, str, bytes]:
    """(원본 파일명, 확장자, 내용). 확장자와 실제 내용이 다르면 거부한다."""
    이름 = re.sub(r"[\x00-\x1f\x7f]", "", (파일.filename or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()[:200]
    확장자 = 이름.rsplit(".", 1)[-1].lower() if "." in 이름 else ""
    확장자 = _확장자_별칭.get(확장자, 확장자)
    if 확장자 not in 허용_확장자:
        raise HTTPException(415, f"올릴 수 없는 형식입니다. {_허용_안내}")
    본문 = 파일.file.read(최대_바이트 + 1)
    if not 본문:
        raise HTTPException(400, "빈 파일입니다.")
    if len(본문) > 최대_바이트:
        raise HTTPException(413, "파일이 너무 큽니다. 3MB 이하만 올릴 수 있습니다.")
    if 실제형식(본문) != 확장자:
        raise HTTPException(415, f"파일 내용이 확장자(.{확장자})와 다릅니다. 원본 파일을 올려 주세요.")
    return 이름, 확장자, 본문


def 계획_첨부목록(plan_id: int) -> list[첨부항목]:
    """계획 상세의 `첨부`. 등록순이고 소유 확인은 호출한 쪽(계획 조회)과 RLS 가 한다."""
    if MOCK:
        return [첨부항목(**{k: v for k, v in 행.items() if k != "본문"})
                for 행 in mock_data.mock_첨부.values() if 행["plan_id"] == plan_id]
    행들 = _질의(
        "SELECT a.attachment_id::text, a.원본파일명, a.크기, a.task_id, a.created_at, a.올린_account_id, w.email "
        "FROM tenant.plan_attachments a LEFT JOIN tenant.accounts w ON w.account_id = a.올린_account_id "
        "WHERE a.plan_id = %s ORDER BY a.created_at, a.attachment_id",
        (plan_id,))
    return [첨부항목(attachment_id=r[0], 원본파일명=r[1], 크기=r[2], task_id=r[3], created_at=r[4].isoformat(),
                     작성자=작성자표시(r[5], r[6]))
            for r in 행들]


def 파일들_지우기(id목록: list[str]) -> None:
    """DB 행을 지운 뒤 부른다. 파일 삭제 실패는 고아 파일이 남을 뿐이므로 기록만 한다."""
    for 첨부id in id목록:
        try:
            _경로(첨부id).unlink(missing_ok=True)
        except OSError:
            _log.exception("첨부 파일 삭제 실패 attachment_id=%s", 첨부id)


@router.post("/api/plans/{plan_id}/attachments", response_model=첨부항목, status_code=201)
def 첨부_올리기(plan_id: int, 파일: UploadFile = File(...), task_id: int | None = Form(None)) -> 첨부항목:
    """파일 하나를 저장한다. task_id 를 주면 그 증빙 항목에 묶인다."""
    org = 현재_org.get()
    # 데모 세션도 막는다. 만료 정리(routes_orgs._만료데모_정리)는 DB 행만 지워 파일이 볼륨에 남는다.
    if not MOCK and (org is None or 현재_account.get() is None):
        raise HTTPException(401, "로그인하면 파일을 저장할 수 있습니다.")
    이름, 확장자, 본문 = _파일_검사(파일)
    if MOCK:
        return _mock_올리기(plan_id, task_id, 이름, 확장자, 본문)

    _여유_확인()
    조건, 인자 = _계정조건(org, "p")
    첨부id = str(uuid.uuid4())
    경로 = _경로(첨부id)
    임시 = 경로.with_name(f".{첨부id}.part")
    try:
        with 트랜잭션() as cur:
            # 계획 행을 잠가 동시 업로드가 개수 제한을 넘지 못하게 한다
            행 = cur.execute(
                f"SELECT p.org_id, p.account_id FROM tenant.expense_plans p "
                f"WHERE p.plan_id = %s AND {조건} FOR UPDATE", (plan_id, *인자)).fetchone()
            if not 행:
                raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
            if task_id is not None and not cur.execute(
                    "SELECT 1 FROM tenant.plan_tasks t WHERE t.task_id = %s AND t.plan_id = %s",
                    (task_id, plan_id)).fetchone():
                raise HTTPException(404, "연결할 증빙 항목을 찾을 수 없습니다")
            개수 = cur.execute("SELECT count(*) FROM tenant.plan_attachments a WHERE a.plan_id = %s",
                             (plan_id,)).fetchone()[0]
            if 개수 >= 계획당_최대:
                raise HTTPException(409, f"첨부파일은 지출 계획 하나에 {계획당_최대}개까지 올릴 수 있습니다.")
            임시.write_bytes(본문)
            os.replace(임시, 경로)
            시각 = cur.execute(
                "INSERT INTO tenant.plan_attachments (attachment_id, org_id, account_id, 올린_account_id, plan_id, "
                "task_id, 원본파일명, 확장자, 크기) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING created_at",
                (첨부id, 행[0], 행[1], 현재_본인.get(), plan_id, task_id, 이름, 확장자, len(본문))).fetchone()[0]
    except BaseException:
        임시.unlink(missing_ok=True)
        경로.unlink(missing_ok=True)
        raise
    return 첨부항목(attachment_id=첨부id, 원본파일명=이름, 크기=len(본문), task_id=task_id, created_at=시각.isoformat())


def _행_찾기(plan_id: int, attachment_id: str) -> tuple | None:
    try:
        uuid.UUID(attachment_id)
    except ValueError:
        return None
    조건, 인자 = _계정조건(현재_org.get(), "a")
    행 = _질의(f"SELECT a.원본파일명 FROM tenant.plan_attachments a "
              f"WHERE a.attachment_id = %s::uuid AND a.plan_id = %s AND {조건}",
              (attachment_id, plan_id, *인자), 예외전파=True)
    return 행[0] if 행 else None


@router.get("/api/plans/{plan_id}/attachments/{attachment_id}")
def 첨부_받기(plan_id: int, attachment_id: str) -> Response:
    if MOCK:
        행 = mock_data.mock_첨부.get(attachment_id)
        if not 행 or 행["plan_id"] != plan_id:
            raise HTTPException(404, _없음)
        return Response(행["본문"], media_type="application/octet-stream", headers={
            **_다운로드_헤더, "Content-Disposition": "attachment; filename*=utf-8''download"})
    행 = _행_찾기(plan_id, attachment_id)
    if not 행:
        raise HTTPException(404, _없음)
    경로 = _경로(attachment_id)
    if not 경로.is_file():
        _log.error("첨부 행은 있는데 파일이 없다 attachment_id=%s", attachment_id)
        raise HTTPException(404, "파일을 찾을 수 없습니다. 다시 올려 주세요.")
    return FileResponse(경로, media_type="application/octet-stream", filename=행[0], headers=_다운로드_헤더)


@router.delete("/api/plans/{plan_id}/attachments/{attachment_id}")
def 첨부_삭제(plan_id: int, attachment_id: str) -> dict:
    if MOCK:
        행 = mock_data.mock_첨부.get(attachment_id)
        if not 행 or 행["plan_id"] != plan_id:
            raise HTTPException(404, _없음)
        del mock_data.mock_첨부[attachment_id]
        return {"삭제": True}
    if not _행_찾기(plan_id, attachment_id):
        raise HTTPException(404, _없음)
    조건, 인자 = _계정조건(현재_org.get(), "a")
    행 = _질의(f"DELETE FROM tenant.plan_attachments a WHERE a.attachment_id = %s::uuid AND a.plan_id = %s "
              f"AND {조건} RETURNING a.attachment_id", (attachment_id, plan_id, *인자), 예외전파=True)
    if not 행:
        raise HTTPException(404, _없음)
    파일들_지우기([attachment_id])
    return {"삭제": True}


def _mock_올리기(plan_id: int, task_id: int | None, 이름: str, 확장자: str, 본문: bytes) -> 첨부항목:
    if not any(p["plan_id"] == plan_id for p in mock_data.mock_계획):
        raise HTTPException(404, f"지출계획 {plan_id} 을(를) 찾을 수 없습니다")
    if task_id is not None and not any(
            t["task_id"] == task_id and t["plan_id"] == plan_id for t in mock_data.mock_할일):
        raise HTTPException(404, "연결할 증빙 항목을 찾을 수 없습니다")
    if sum(행["plan_id"] == plan_id for 행 in mock_data.mock_첨부.values()) >= 계획당_최대:
        raise HTTPException(409, f"첨부파일은 지출 계획 하나에 {계획당_최대}개까지 올릴 수 있습니다.")
    첨부id = str(uuid.uuid4())
    값 = {"attachment_id": 첨부id, "원본파일명": 이름, "크기": len(본문), "task_id": task_id,
         "created_at": datetime.now(timezone.utc).isoformat()}
    mock_data.mock_첨부[첨부id] = {**값, "plan_id": plan_id, "본문": 본문}
    return 첨부항목(**값)
