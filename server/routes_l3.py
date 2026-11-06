# -*- coding: utf-8 -*-
"""L3(기관 자체규정) 업로드 API — 파일 접수, 원본 저장, 파싱 상태 조회.

POST /api/l3/upload 는 tenant.l3_documents 행을 만들고 원본을 저장한 뒤 202 를 반환하고,
파싱은 백그라운드 작업(`백그라운드_파싱` → scripts/l3_parse.파싱)으로 실행한다. 프론트는
GET /api/l3/{doc_id} 를 폴링하고, GET /api/l3/current 로 적용 중인 문서 목록을 본다.
허용 형식은 PDF·HWPX·HWP 이고 파일 내용으로 형식을 확인한다. DOC·DOCX 는 415.
L3 가 바뀌면 캐시키의 l3_해시가 바뀌어 이전 판정 캐시가 답하지 않는다(response_cache.l3_해시).

기관당 판정에 쓰는 L3 는 한 문서다. 새 문서의 파싱이 성공하면 이전 문서를 대체하고, 실패하면 이전 문서가
계속 적용된다(scripts/l3_parse.적용문서_정리). 상태 응답의 메시지는 이 두 경우와 '적용 중인 규정 없음' 을
나눠 안내한다. 'pending' 으로 `_중단_판정_분` 분 넘게 남은 문서(서버 재시작 등으로 파싱이 끊김)는 실패로 보여준다.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import (APIRouter, BackgroundTasks, File, Form, HTTPException,
                     Request, UploadFile)

from ._common import DSN, MOCK, _org조건, _질의, _실행
from .file_format import 실제형식
from .models import L3업로드응답, L3현재문서, L3현재문서목록응답
from . import auth, mock_data

_log = logging.getLogger("suddoe.l3")

router = APIRouter(prefix="/api/l3", tags=["L3 업로드"])

허용_확장자 = {"pdf", "hwpx", "hwp"}
거부_확장자 = {"doc", "docx"}          # 파서가 없다
최대_바이트 = 30 * 1024 * 1024

_접수_메시지 = "접수했습니다. 조문 분할이 끝나면 상태가 「완료」로 바뀝니다."

# 파싱이 이 시간보다 오래 'pending' 이면 끊긴 것으로 본다. 정상 파싱은 수십 초 안에 끝난다
_중단_판정_분 = 10        # scripts/l3_parse.py `중단_판정_분` 과 같은 값(서버 기동 때 파서를 import 하지 않으려고 따로 둔다)

# 실패사유 코드(scripts/l3_parse.py 가 기록) → 사용자 안내. 다음에 할 일을 함께 적는다
_실패_안내 = {
    "원본없음": "서버에서 원본 파일을 찾지 못했습니다. 다시 올려 주세요.",
    "보호문서": "배포용이거나 암호가 걸린 문서라 내용을 읽을 수 없습니다. 보호를 해제한 HWP·HWPX 나 PDF 로 올려 주세요.",
    "추출실패": "파일이 손상됐거나 내용을 읽을 수 없습니다. 원본을 다시 저장해 올려 주세요.",
    "형식": "지원하지 않는 형식입니다. PDF·HWPX·HWP 로 올려 주세요.",
    "스캔본": "글자를 인식할 수 없는 문서입니다(스캔한 PDF 등). 글자를 선택할 수 있는 PDF 나 HWPX 로 올려 주세요.",
    # 아래 두 코드는 스캔본을 OCR(scripts/clova_ocr.py)로 읽으려다 실패한 경우다
    "쪽수초과": "스캔본은 OCR 로 읽는데, 쪽수가 한도를 넘었습니다. 규정 부분만 나눠 올리거나 "
             "글자를 선택할 수 있는 PDF·HWPX 로 올려 주세요.",
    "문자인식실패": "스캔본을 OCR 로 읽지 못했습니다. 잠시 뒤 다시 올리거나 글자를 선택할 수 있는 PDF·HWPX 로 올려 주세요.",
    "조문없음": "규정 조문을 하나도 찾지 못했습니다. 기관 규정 원문 파일이 맞는지 확인해 주세요.",
    "분할실패": "조문을 나누는 중 오류가 났습니다. 다시 올려 주세요.",
    "저장실패": "조문을 저장하는 중 오류가 났습니다. 다시 올려 주세요.",
    "서버오류": "처리 중 서버 오류가 났습니다. 다시 올려 주세요.",
    "중단": "처리가 중단됐습니다(서버 재시작 등). 다시 올려 주세요.",
}
_실패_기본안내 = "다시 올려 주세요."

# 화면에 보일 파싱품질. 오래 'pending' 인 문서는 끊긴 것이므로 fail 로 보여준다
_표시_품질_SQL = (f"CASE WHEN d.\"파싱품질\" = 'pending' "
                f"AND d.created_at < now() - interval '{_중단_판정_분} minutes' "
                f"THEN 'fail' ELSE d.\"파싱품질\" END")


def _업로드_org(요청: Request, 요청_org_id: str | None) -> str:
    """이 업로드의 소유 org 를 서버가 정한다.

    검증된 주체(토큰)가 있으면 토큰의 org 가 우선한다. 멀티파트 Form 의 org_id 는 클라이언트가
    보낸 검증되지 않은 값이고, 미들웨어의 쿼리스트링 교체도 적용되지 않는다. 토큰이 없으면
    Form 값을 쓰되 SUDDOE_ALLOW_ORG_PARAM=1 이 아니면 401 로 거부한다. Form 값의 UUID 형식은 여기서
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
        raise HTTPException(401, "기관을 확인할 수 없습니다. 로그인해 주세요.")
    return 요청_org_id


@router.post("/upload", response_model=L3업로드응답, status_code=202)
def 업로드(
    요청: Request,
    백그라운드: BackgroundTasks,
    파일: UploadFile = File(...),
    # 선택 필드다. 로그인 상태면 _업로드_org 가 토큰의 org 로 대체한다
    org_id: str | None = Form(None),
    # 받기만 하고 저장하지 않는다. 기관명의 기준값은 tenant.orgs.기관명 이다
    기관명: str | None = Form(None),
) -> L3업로드응답:
    # 클라이언트가 보낸 org_id 가 아래 처리에 섞이지 않도록 맨 앞에서 확정한다
    org_id = _업로드_org(요청, org_id)

    이름 = 파일.filename or ""
    확장자 = 이름.rsplit(".", 1)[-1].lower() if "." in 이름 else ""

    if 확장자 in 거부_확장자:
        raise HTTPException(415, f".{확장자} 은 지원하지 않습니다. PDF·HWPX·HWP 로 올려 주세요.")
    if 확장자 not in 허용_확장자:
        raise HTTPException(415, f"지원하지 않는 형식입니다 (.{확장자 or '?'}). PDF·HWPX·HWP 만 받습니다.")

    # 최대 크기보다 1바이트만 더 읽는다. 전체를 메모리에 올린 뒤 크기를 보면 큰 업로드로 메모리가 고갈된다.
    본문 = 파일.file.read(최대_바이트 + 1)
    if not 본문:
        raise HTTPException(400, "빈 파일입니다.")
    if len(본문) > 최대_바이트:
        raise HTTPException(413, "파일이 너무 큽니다 (30MB 이하).")

    # 30MB 초과가 413 으로 먼저 걸리도록 크기·빈 파일 검사 다음에 내용을 본다
    실제_형식 = 실제형식(본문)
    if 실제_형식 != 확장자:
        raise HTTPException(415, f"파일 내용이 .{확장자} 이 아닙니다 (실제: {실제_형식}). "
                                 f"확장자만 바꾼 파일은 파싱할 수 없습니다.")

    if MOCK:
        return L3업로드응답(**{**mock_data.mock_L3, "파일명": 이름, "확장자": 확장자,
                            "doc_id": f"l3-mock-{uuid.uuid4().hex[:8]}"})
    응답 = _live_업로드(본문, 이름, 확장자, org_id)
    # 202 응답 뒤 백그라운드로 파싱한다. org_id 는 백그라운드_파싱 이 GUC app.org_id 를 설정하는 데 쓴다
    백그라운드.add_task(백그라운드_파싱, 응답.doc_id, org_id)
    return 응답


@router.get("/current", response_model=L3현재문서목록응답)
def 현재문서(org_id: str | None = None) -> L3현재문서목록응답:
    """org 의 active L3 문서 목록. org_id 가 없으면(게스트) 빈 목록이다.

    기관당 판정에 쓰는 문서(pass/warn)는 많아야 하나이고 맨 앞에 둔다. 그 뒤로 처리 중인 문서(pending, 동시에
    여러 건 올리면 여러 건일 수 있다)와 최근 실패 1건(fail)이 온다. 적용 문서가 없으면 pending 이 맨 앞일 수
    있으므로 프론트는 파싱품질이 pass/warn 인 문서만 「적용 중」으로 보여준다(lib/adapt.ts L3현재문서요약짓기).
    `/{doc_id}` 보다 먼저 등록해야 한다. 그렇지 않으면 'current' 가 doc_id 경로 파라미터로 매칭된다.
    """
    if MOCK:
        return L3현재문서목록응답(문서=[])
    조건, org인자 = _org조건(org_id, "d")
    행 = _질의(
        f'SELECT d.doc_id, d."원본파일명", d.version, d."시행일", d.created_at, {_표시_품질_SQL}, '
        f'       (SELECT count(*) FROM tenant.l3_articles a WHERE a.doc_id = d.doc_id), d.extraction '
        f'  FROM tenant.l3_documents d '
        f' WHERE {조건} AND d.status = \'active\' '
        f' ORDER BY (d."파싱품질" IN (\'pass\', \'warn\')) DESC, d.created_at DESC',
        org인자,
    )
    return L3현재문서목록응답(문서=[
        L3현재문서(doc_id=str(doc_id), 원본파일명=이름, version=버전,
                시행일=시행일.isoformat() if 시행일 else None,
                등록일=등록.date().isoformat() if 등록 else None,
                파싱품질=파싱품질, 조_건수=건수, 추출방식=추출방식)
        for doc_id, 이름, 버전, 시행일, 등록, 파싱품질, 건수, 추출방식 in 행])


@router.get("/{doc_id}", response_model=L3업로드응답)
def 상태(doc_id: str, org_id: str | None = None) -> L3업로드응답:
    """업로드한 문서의 파싱 상태. 프론트가 '분석 중' 표시를 하며 폴링한다."""
    if MOCK:
        # 미해결 참조가 있는 완료본을 돌려준다. 프론트가 실패 안내 UI 를 그릴 수 있게 한다
        return L3업로드응답(**{**mock_data.mock_L3_미해결참조, "doc_id": doc_id})
    return _live_상태(doc_id, org_id)


# ════════════════════════════════════════════════════════════════════
# live 모드 구현
# ════════════════════════════════════════════════════════════════════

# 확장자 → l3_documents.extraction 값. pdf 는 'native'(pdftext.py 추출)로 기록한다
_확장자별_추출방식 = {"hwpx": "hwpx", "hwp": "hwp"}


def _live_업로드(본문: bytes, 파일명: str, 확장자: str, org_id: str) -> L3업로드응답:
    """tenant.l3_documents 행을 만들고 원본을 저장한 뒤 상태='파싱대기' 응답을 만든다.

    파서는 여기서 부르지 않는다. 저장 경로는 scripts/l3_parse 의 `원본경로()` 규칙을 그대로
    쓴다(파서가 같은 경로에서 읽는다). 파서 모듈을 불러오지 못하거나 파일 저장이 실패하면 방금 만든
    행을 지우고 500.
    """
    추출방식 = _확장자별_추출방식.get(확장자, "native")
    행 = _질의(
        """INSERT INTO tenant.l3_documents
               (org_id, "원본파일명", status, extraction, "파싱품질", "미해결참조수")
           VALUES (%s, %s, 'active', %s, 'pending', 0)
           RETURNING doc_id""",
        (org_id, 파일명, 추출방식),
    )
    if not 행:
        raise HTTPException(400, "업로드를 저장하지 못했습니다 (org_id를 확인해 주세요).")
    doc_id = str(행[0][0])

    # 경로가 doc_id 에서 나오므로 INSERT 다음에 쓴다. 지연 import: mock 모드와 서버 기동 시에는
    # 파서 의존성을 읽지 않는다. import 실패도 저장 실패와 같이 행을 지운다.
    try:
        from l3_parse import L3_저장소, 원본경로    # scripts/ — sys.path 는 main.py 가 잡는다
        L3_저장소.mkdir(parents=True, exist_ok=True)
        원본경로(doc_id, 확장자).write_bytes(본문)
    except (OSError, ImportError) as e:
        _실행("DELETE FROM tenant.l3_documents WHERE doc_id = %s", (doc_id,))
        raise HTTPException(500, f"원본을 저장하지 못했습니다 ({type(e).__name__}).") from e

    return L3업로드응답(
        doc_id=doc_id, 파일명=파일명, 확장자=확장자, 상태="파싱대기",
        조_건수=None, 미해결참조=[], 메시지=_접수_메시지,
    )


def 백그라운드_파싱(doc_id: str, org_id: str) -> None:
    """업로드 응답 뒤 백그라운드 작업으로 파서를 실행한다(202 + 상태 폴링 계약).

    `_common` 헬퍼를 거치지 않는 커넥션이라 GUC app.org_id 를 직접 설정한다. 설정하지 않으면
    RLS 정책 때문에 0행이 된다. 파싱이 성공하면 같은 트랜잭션에서 계정 없는 judge_cache 행을 지운다.
    예외를 밖으로 던지지 않는다. 실패는 파싱품질='fail' 로 기록해 상태가 'pending' 으로 남지 않게 한다.
    파싱() 이 실패를 직접 기록하므로, 여기서는 파서 밖에서 난 예외만 `_실패_직접기록` 으로 남긴다.
    """
    try:
        import psycopg

        from l3_parse import 파싱                      # scripts/ — sys.path 는 main.py 가 잡는다
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
            with conn.cursor() as cur:
                결과 = 파싱(cur, doc_id)
                if 결과.get("ok", True):
                    # 이 연결은 app.org_id 만 설정하므로 RLS 때문에 계정 없는 행(데모 세션)만 지워진다.
                    # 계정별 행은 남지만 캐시키의 l3_해시가 바뀌어 이전 근거로 답하지 않는다.
                    cur.execute("DELETE FROM tenant.judge_cache WHERE org_id = %s", (str(org_id),))
            conn.commit()
        if not 결과.get("ok", True):
            _log.error("L3 파싱 실패(예외 아님) doc_id=%s 사유=%s", doc_id, 결과.get("사유"))
    except Exception:                                   # noqa: BLE001
        _log.exception("L3 파싱 백그라운드 작업 실패 doc_id=%s", doc_id)
        _실패_직접기록(doc_id, org_id, "서버오류")


def _실패_직접기록(doc_id: str, org_id: str, 실패사유: str) -> None:
    """파서 밖 예외(파서 import 실패, DB 오류 등)를 fail 로 남긴다.

    `_실행()` 은 요청 컨텍스트의 org 로 GUC 를 설정하는데, 백그라운드 작업에서는 그 값에 기대지 않는다.
    자체 커넥션에 GUC 를 설정하고, 파서를 불러올 수 있으면 적용 문서 정리까지 같은 트랜잭션에서 한다.
    """
    try:
        import psycopg
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            conn.execute("SELECT set_config('app.org_id', %s, true)", (str(org_id),))
            try:
                from l3_parse import 실패_기록
            except ImportError:
                conn.execute('UPDATE tenant.l3_documents SET "파싱품질" = \'fail\', "실패사유" = %s '
                             'WHERE doc_id = %s', (실패사유, doc_id))
            else:
                with conn.cursor() as cur:
                    실패_기록(cur, doc_id, org_id, 실패사유)
            conn.commit()
        _log.error("  → 파싱품질을 fail 로 기록했다 (실패사유=%s)", 실패사유)
    except Exception:                                   # noqa: BLE001
        _log.exception("L3 실패 기록도 실패했다 doc_id=%s — 상태 조회에서 %d분 뒤 실패로 보인다",
                       doc_id, _중단_판정_분)


def _상태_메시지(파싱품질: str | None, 조_건수: int, 미해결참조수: int | None, *,
              실패사유: str | None = None, 대체됨: bool = False,
              다른_적용문서: str | None = None, 이전_문서: str | None = None,
              ocr: bool = False) -> tuple[str, int | None, str]:
    """(상태, 조_건수, 메시지) 를 정한다. DB 를 읽지 않는 순수 함수다.

    다른_적용문서: 이 문서가 아닌, 지금 판정에 쓰는 같은 기관 문서의 이름. 없으면 None.
    이전_문서: 이 문서가 대체한 이전 문서 이름. 대체됨: 이 문서가 나중에 올린 문서로 대체됐는가.
    ocr: 스캔본을 OCR 로 읽은 문서인가(extraction='ocr').
    """
    if 파싱품질 == "fail":
        메시지 = "파싱에 실패했습니다. " + _실패_안내.get(실패사유 or "", _실패_기본안내)
        if 다른_적용문서:
            메시지 += f" 판정에는 기존 규정을 계속 씁니다(「{다른_적용문서}」)."
        else:
            메시지 += " 지금은 적용 중인 기관 규정이 없어 상위 규정(중기부·창진원 지침)만으로 판정합니다."
        return "실패", None, 메시지
    if 조_건수:
        메시지 = f"{조_건수}개 조를 등록했습니다."
        if 대체됨:
            메시지 += " 이후 올린 문서로 교체되어 판정에는 쓰지 않습니다" + (
                f"(현재: 「{다른_적용문서}」)." if 다른_적용문서 else ".")
        elif 이전_문서:
            메시지 += f" 이전 규정을 대체해 이 문서로 판정합니다(이전: 「{이전_문서}」)."
        if 미해결참조수:
            메시지 += f" 참조 {미해결참조수}건은 상위 규정을 등록된 규정에서 찾지 못했습니다."
        # 조문은 뽑혔지만 품질이 낮다. 상태는 '완료'로 두고 메시지로만 알린다
        if ocr:
            # 이 문서를 인용한 '가능' 은 조건부로 낮춘다(scripts/llm_validate.py B_GRADE_DOWNGRADE)
            메시지 += (" 스캔본이라 OCR 로 읽었습니다. 금액·조번호를 잘못 읽었을 수 있어 이 문서를 근거로 한 "
                     "'가능' 은 '조건부' 로 안내합니다. 원문과 대조해 확인해 주세요.")
        elif 파싱품질 == "warn":
            메시지 += " (품질 확인 필요 — 조문 인식이 형식과 어긋났을 수 있습니다.)"
        return "완료", 조_건수, 메시지
    if 파싱품질 == "warn":
        # 파서는 실행됐지만 조문이 0건이다. 아직 처리 전인 경우와 원인이 달라 메시지를 나눈다
        return "파싱대기", None, "파싱을 시도했지만 조문을 뽑아내지 못했습니다. 확인이 필요합니다."
    if 대체됨:
        # 처리 중에 나중에 올린 문서가 먼저 성공했다. 이 문서는 끝나도 판정에 쓰이지 않는다
        return "파싱대기", None, "이후 올린 문서로 교체되어 이 문서는 판정에 쓰지 않습니다" + (
            f"(현재: 「{다른_적용문서}」)." if 다른_적용문서 else ".")
    메시지 = _접수_메시지
    if 다른_적용문서:
        메시지 += f" 처리가 끝날 때까지는 기존 규정으로 판정합니다(「{다른_적용문서}」)."
    return "파싱대기", None, 메시지


def _live_상태(doc_id: str, org_id: str | None) -> L3업로드응답:
    """l3_documents 와 l3_articles 건수로 상태(완료/실패/파싱대기)를 정해 반환한다.

    같은 기관에서 지금 판정에 쓰는 문서(active, pass/warn 중 최신)와 이 문서가 대체한 이전 문서를 함께
    읽어 메시지를 정한다. 미해결 참조 상세(조·참조·사유)는 저장하는 테이블이 없어 항상 빈 배열이다.
    `_org조건()` 을 써서 게스트(org_id 없음)는 0행, 즉 404 가 된다.
    """
    조건, org인자 = _org조건(org_id, "d")
    행 = _질의(
        f'SELECT d."원본파일명", d."파싱품질", {_표시_품질_SQL}, d."미해결참조수", d."실패사유", '
        f'       d.status, d.org_id, d.created_at, d.extraction '
        f'FROM tenant.l3_documents d '
        f'WHERE d.doc_id = %s AND {조건}',
        (doc_id, *org인자),
    )
    if not 행:
        raise HTTPException(404, f"L3 문서 {doc_id} 을(를) 찾을 수 없습니다")
    파일명, 원래_품질, 파싱품질, 미해결참조수, 실패사유, status, 문서_org, 등록, 추출방식 = 행[0]
    if 원래_품질 == "pending" and 파싱품질 == "fail":
        실패사유 = "중단"                     # 파싱이 끊겨 'pending' 으로 남은 문서
    확장자 = 파일명.rsplit(".", 1)[-1].lower() if "." in (파일명 or "") else ""

    개수 = _질의('SELECT count(*) FROM tenant.l3_articles WHERE doc_id = %s', (doc_id,))
    적용 = _질의(
        'SELECT doc_id, "원본파일명" FROM tenant.l3_documents '
        " WHERE org_id = %s AND status = 'active' AND \"파싱품질\" IN ('pass', 'warn') "
        ' ORDER BY created_at DESC LIMIT 1', (문서_org,))
    적용_id, 적용_이름 = (str(적용[0][0]), 적용[0][1]) if 적용 else (None, None)
    이전 = []
    if 적용_id == doc_id:
        이전 = _질의(
            'SELECT "원본파일명" FROM tenant.l3_documents '
            " WHERE org_id = %s AND status = 'superseded' AND \"파싱품질\" IN ('pass', 'warn') "
            '   AND created_at < %s ORDER BY created_at DESC LIMIT 1', (문서_org, 등록))
    상태값, 조_건수, 메시지 = _상태_메시지(
        파싱품질, 개수[0][0] if 개수 else 0, 미해결참조수, 실패사유=실패사유,
        대체됨=status == "superseded",
        다른_적용문서=적용_이름 if 적용_id != doc_id else None,
        이전_문서=이전[0][0] if 이전 else None, ocr=추출방식 == "ocr")
    return L3업로드응답(doc_id=doc_id, 파일명=파일명, 확장자=확장자, 상태=상태값,
                        조_건수=조_건수, 미해결참조=[], 메시지=메시지, 파싱품질=파싱품질,
                        실패사유=실패사유 if 파싱품질 == "fail" else None,
                        적용중_문서=적용_이름, 추출방식=추출방식)
