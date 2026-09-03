# -*- coding: utf-8 -*-
"""L3 기관 규정 업로드 — 화면 4 온보딩 ③.   **[L3 업로드 계통]**

🔴 **이 계통은 파싱을 하지 않는다.** 파서(`scripts/hwp_extract.py` 등)는 다른 세션 소유이고
   이 세션의 훅이 `scripts/` 쓰기를 막는다. 여기서는 «접수 + 저장 + 상태» 까지만 한다.
   파싱은 뒤에 붙는다 — 그래서 202 Accepted 와 상태 폴링 경로를 둔다.

🔴 dangling 은 판정 시점이 아니라 **업로드 시점**에 알린다 (CLAUDE.md).
   그래서 응답에 자리를 만들어 둔다. 실제 채우기는 파서 연결 후다.

확장자: PDF · HWPX · HWP 만. DOC·DOCX 는 415 로 거부한다 (파서가 없다 — 만들지 않는다).
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
from .models import L3업로드응답
from .routes_plans import _org조건
from . import auth, mock_data

_log = logging.getLogger("suddoe.l3")

router = APIRouter(prefix="/api/l3", tags=["L3 업로드"])

허용_확장자 = {"pdf", "hwpx", "hwp"}
거부_확장자 = {"doc", "docx"}          # 파서 없음. 프론트 온보딩 안내에서도 뺄 것
최대_바이트 = 30 * 1024 * 1024


def 실제형식(본문: bytes) -> str:
    """파일 «내용»으로 형식을 본다. 확장자를 믿지 않는다.

    🔴 **확장자는 거짓말을 한다** — 실물 증거가 있다. 코퍼스의
       `민관공동창업자발굴육성(TIPS)….hwpx` 는 내부가 **XLSX** 다
       (`namelist()` 가 `['[Content_Types].xml','_rels/.rels','xl/…']`).

    🔴 **매직바이트만으로는 부족하다.** HWPX 도 XLSX 도 DOCX 도 전부 ZIP 이라
       앞 4바이트가 똑같이 `PK\\x03\\x04` 다 — 위 파일은 매직바이트 검사를 그냥 통과한다.
       그래서 ZIP 이면 **안의 항목 이름**까지 본다 (hwpx = `Contents/`, xlsx = `xl/`).

    ⚠️ OLE 헤더(`\\xd0\\xcf\\x11\\xe0…`)는 구형 HWP·DOC·XLS 가 **공유**한다.
       여기서 `hwp` 로 답하는 건 「OLE 복합문서다」까지이지 「한글 문서다」가 아니다.
       그 이상은 파서가 열어봐야 안다 — 여기서 단정하지 않는다.
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


def _업로드_주인(요청: Request, 자기신고: str) -> str:
    """이 업로드가 «어느 기관» 것인지 서버가 정한다.

    🔴 **업로드의 org_id 는 `auth.OrgId주입` 이 못 막는다** (2026-09-03 재현).
       그 미들웨어는 `scope["query_string"]` 만 갈아끼우는데 이 경로의 org_id 는
       **multipart Form 필드**라 손이 안 닿는다. 같은 요청·같은 토큰으로 두 축을
       나란히 태워 확인했다 — 쿼리스트링 축은 토큰 주인으로 «교정» 됐는데
       (`GET /api/l3/{doc_id}?org_id=<남의 org>` → SELECT 인자가 토큰의 org),
       Form 축은 **남의 기관 UUID 가 그대로 INSERT 에 박혔다.**
       그 값이 `l3_documents`·`l3_articles` 의 org_id 가 되고 그게 판정 검색의
       기관 축이므로, **남의 기관에 규정을 심을 수 있었다.**

    🔴 고칠 자리가 미들웨어가 «아닌» 이유: 미들웨어에서 multipart 를 뜯으면
       `receive` 를 소진해 스트리밍 업로드가 깨진다 (30MB 파일을 메모리에 두 번
       얹는다). 미들웨어는 검증된 주체를 `scope["suddoe_주체"]` 에 이미 넣어 두므로
       라우터가 그걸 «집어» 쓰면 된다 — 그게 이 함수다.

    🔴 `SUDDOE_ORG_PARAM=0` 으로도 이 축은 안 닫힌다 — 실측했다. 그 스위치는
       미들웨어의 «쿼리» 자기신고만 본다. 토큰 없이 Form 에 남의 org 를 실으면
       스위치를 꺼도 202 였다. 그래서 여기서 스위치를 «직접» 한 번 더 본다.
       (`auth.` 를 붙여 «속성으로» 읽는다 — 값을 복사해 오면 테스트가 못 끈다)

    ⚠️ 자기신고 값의 UUID 검사는 여기서 «하지 않는다». 목 경로 계약 테스트가
       `org-test-1` 같은 비-UUID 를 보내고 있어서, 검사를 넣으면 인증과 무관한
       기존 테스트가 깨진다. 실 경로에서 비-UUID 는 INSERT 가 거부해 400 으로 닫힌다.
    """
    주 = 요청.scope.get("suddoe_주체")
    if 주 is not None and 주.검증됨 and 주.org_id:
        if 자기신고 and str(자기신고) != str(주.org_id):
            # 정상 프론트는 이런 요청을 안 만든다. 사고든 공격이든 흔적이 남는 게 낫다.
            _log.warning("L3 업로드 org_id 불일치 — 토큰=%s Form=%s · 토큰을 쓴다",
                         주.org_id, 자기신고)
        return str(주.org_id)                      # 🔴 토큰이 이긴다
    if not auth.ORG_PARAM_허용:
        raise HTTPException(401, "기관을 확인할 수 없습니다 — 로그인이 필요합니다.")
    return 자기신고


@router.post("/upload", response_model=L3업로드응답, status_code=202)
async def 업로드(
    요청: Request,
    배경: BackgroundTasks,
    파일: UploadFile = File(...),
    org_id: str = Form(...),
    기관명: str | None = Form(None),
) -> L3업로드응답:
    # 🔴 «맨 앞» 이다. 아래 검사들이 org_id 를 안 쓰긴 하지만, 나중에 누가
    #    org 별 한도·중복 검사를 끼워 넣을 때 자기신고 값이 쓰이면 안 된다.
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

    # 🔴 확장자 다음에 «내용» 을 본다. 순서가 중요하다 — 크기·빈파일 검사를 먼저 통과시켜야
    #    30MB 초과가 415 가 아니라 413 으로 나간다.
    #    🔴 목·실 양쪽에 건다. 목이 받아주고 실서버가 거부하면 계약(「갈아끼워도 프론트
    #       코드는 한 줄도 안 바뀐다」)이 깨진다.
    진짜 = 실제형식(본문)
    if 진짜 != 확장:
        raise HTTPException(415, f"파일 내용이 .{확장} 이 아닙니다 (실제: {진짜}). "
                                 f"확장자만 바꾼 파일은 파싱할 수 없습니다.")

    if MOCK:
        return L3업로드응답(**{**mock_data.목_L3, "파일명": 이름, "확장자": 확장,
                            "doc_id": f"l3-mock-{uuid.uuid4().hex[:8]}"})
    응답 = _실_업로드(본문, 이름, 확장, org_id, 기관명)
    # 🔴 응답을 «보낸 뒤» 에 파싱한다. 202 Accepted 계약이라 여기서 붙들면 안 된다.
    배경.add_task(파싱_배경, 응답.doc_id)
    return 응답


@router.get("/{doc_id}", response_model=L3업로드응답)
def 상태(doc_id: str, org_id: str | None = None) -> L3업로드응답:
    """프론트가 「분석 중」 스피너를 돌리며 폴링하는 경로."""
    if MOCK:
        # 목에서는 dangling 이 있는 완료본을 돌려준다 — 프론트가 실패 안내 UI 를 그려야 한다
        return L3업로드응답(**{**mock_data.목_L3_dangling, "doc_id": doc_id})
    return _실_상태(doc_id, org_id)


# ════════════════════════════════════════════════════════════════════
# 🔴 L3 실 경로 구역
# ════════════════════════════════════════════════════════════════════

# pdf → 'native' (pdftext.py 경로)
#
# 🔴 **이 표를 확장자로 조회해도 되는 이유는 `실제형식()` 이 앞에서 걸러주기 때문이다.**
#    그 검사가 없으면 위장 파일이 **틀린 `extraction` 태그**를 DB 에 박는다.
#    CLAUDE.md 가 `extraction` 을 신뢰등급에 묶어놨으므로(`vlm` → A등급 인용 금지)
#    태그가 틀리면 **신뢰등급이 틀린다.** 업로드 경로에서 내용 검사를 빼면 이 줄이 거짓말한다.
_확장_추출방식 = {"hwpx": "hwpx", "hwp": "hwp"}

# 업로드 원본을 두는 곳. 파서가 `doc_id` 로 찾아간다.
#
# 🔴 **DB 에 파일 칸이 없다.** `l3_documents` 는 `원본파일명`(표시용)·`출처`(라벨)만 들고
#    바이트를 담을 컬럼이 없다. 컬럼을 새로 파는 건 DDL 이라 여기서 안 한다 —
#    대신 **`doc_id` 에서 경로가 유도되게** 해서 DB 에 경로를 안 적고도 찾을 수 있게 한다.
#    (`doc_id` 는 PK 라 충돌하지 않고, 행이 지워지면 파일도 고아가 되는 게 눈에 보인다)
L3_저장소 = Path(os.environ.get("SUDDOE_L3_DIR", str(ROOT / "_l3_업로드")))


def 원본경로(doc_id: str, 확장: str) -> Path:
    """🔴 파일명을 **사용자 입력에서 만들지 않는다.** 서버가 만든 `doc_id`(uuid)로만 짓는다 —
    사용자 파일명을 경로에 쓰면 `../` 로 저장소 밖에 쓸 수 있다."""
    안전확장 = 확장 if 확장 in 허용_확장자 else "bin"
    return L3_저장소 / f"{uuid.UUID(str(doc_id))}.{안전확장}"


def _실_업로드(본문: bytes, 파일명: str, 확장: str,
             org_id: str, 기관명: str | None) -> L3업로드응답:
    """tenant.l3_documents 행 생성 + **원본 저장** + 상태='파싱대기' 로 202.

    🔴 여기서 파서를 부르지 마라. `scripts/` 는 훅이 막고, 파싱은 파서 쪽에서 붙인다.
    🔴 org_id 는 l3_articles 에도 중복 저장된다 — 판정 검색이 기관을 못 넘게 하는 축이다.
       (그 INSERT 는 파서가 한다 — 여기는 l3_documents 접수까지다)
    🔴 `기관명` 은 저장하지 않는다 — `tenant.orgs.기관명` 이 기준이다.

    🔴 **2026-09-02 — 이 함수는 그 전까지 `본문`(파일 바이트)을 인자로 받아놓고 버렸다.**
       INSERT 에 안 쓰고 디스크에도 안 썼다. 그래서 사용자가 규정을 올리면
       **「접수했습니다」 → 행만 생기고 → 파일은 사라지고 → 영원히 「분석 중」** 이었다.
       415(거부)는 실패라고 말해주는데 202 는 **성공했다고 말하고 아무 일도 안 했다** —
       프로젝트 원칙(「모든 실패의 기본값은 판단불가」) 기준으로 이쪽이 훨씬 나쁘다.
       RAG 축이 「L3 먼저」로 시작하는데 L3 가 들어올 길이 없던 것이다.

    ✅ `파싱품질` CHECK 에 '대기' 가 있다 — 업로드 시점엔 '대기'. 파서가 실제 파싱 후
       UPDATE 로 pass/warn/fail 로 덮어쓴다. **그 UPDATE 는 아직 아무도 안 한다**
       (`server/`·`scripts/` 통틀어 0건) — 파일이 남으니 이제 붙일 수는 있다.
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

    # 🔴 INSERT 로 doc_id 를 받은 «다음에» 쓴다 — 경로가 doc_id 에서 나오기 때문이다.
    #    쓰기가 실패하면 행만 남아 「파일 없는 파싱대기」가 된다. 그건 고치려던 그 상태와
    #    같으므로 **행을 되돌리고 실패를 알린다.** 조용히 202 를 주지 않는다.
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


def 파싱_배경(doc_id: str) -> None:
    """업로드 응답을 보낸 «뒤» 에 파서를 태운다.

    🔴 **동기로 부르면 안 된다.** 계약이 `202 Accepted` + 상태 폴링이고
       (`API_계약_v1.0.md` §7-2), 파싱은 PDF 면 초 단위다. 응답을 붙들면
       프론트의 「분석 중」 스피너가 존재 이유를 잃는다.

    🔴 **여기서 예외를 밖으로 던지지 마라.** 이 함수는 응답이 나간 뒤에 도는지라
       터져도 사용자에게 전달할 길이 없다 — 조용히 죽으면 상태가 '대기' 에 영원히 남는다.
       `l3_parse.파싱()` 은 자체적으로 모든 실패를 `파싱품질='fail'` 로 닫고 예외를
       안 던지지만(ai-e5 확인), **DB 접속 자체가 실패하는 경로**는 그 밖이다.
       그 경우까지 '대기' 로 남지 않게 여기서 잡아 'fail' 로 닫는다.
    """
    try:
        import psycopg

        from l3_parse import 파싱                      # scripts/ — sys.path 는 main.py 가 잡는다
        with psycopg.connect(DSN, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                파싱(cur, doc_id)
            conn.commit()
    except Exception as e:                              # noqa: BLE001
        _log.exception("L3 파싱 배경 작업 실패 doc_id=%s", doc_id)
        # 🔴 '대기' 로 남기면 사용자는 영원히 「분석 중」을 본다. 실패를 실패라고 말한다.
        _실행('UPDATE tenant.l3_documents SET "파싱품질" = %s WHERE doc_id = %s',
             ("fail", doc_id))
        _log.error("  → 파싱품질을 fail 로 닫았다 (%s)", type(e).__name__)


def _실_상태(doc_id: str, org_id: str | None) -> L3업로드응답:
    """`l3_documents` + `l3_articles` 건수로 상태를 파생한다.

    `파싱품질='fail'` → 실패. `l3_articles` 행이 있으면 완료. 둘 다 아니면 파싱대기 —
    다만 「아직 안 봤다」('대기')와 「평가는 했는데 조문을 못 뽑았다」('warn', 조_건수=0)
    는 원인이 다르므로 메시지로 구분한다. 🔴 API 로 나가는 상태 어휘(완료·실패·파싱대기)
    자체는 그대로다 — 파생 근거만 정확해지는 것이다 (2026-09-01 ai-14 후속 지시).
    dangling 상세(조·참조·사유)는 저장할 테이블이 아직 없다 — 자리만 두고 비워 둔다
    (`routes_l3.py` 상단 주석 · 실제 채우기는 파서 연결 후).

    🔴 2026-09-02 수정 — 이 함수는 그 전까지 **주인에게도 항상 404** 였다.
       **관측**: 옛 SQL 은 `WHERE doc_id = %s AND (%s IS NULL OR org_id = %s)` 였다.
       실제로 태워 보니(psycopg 직접 호출) org_id 를 **str** 로 넘기면 org 를 줬든
       None 이든 둘 다 `IndeterminateDatatype: could not determine data type of
       parameter $2` 로 죽었다. `uuid.UUID` 객체로 넘기면 성공했다.
       **추론**: `%s` 가 `... IS NULL` 자리에만 나와 Postgres 가 타입을 못 정한다 —
       컬럼 비교(`org_id = %s`)와 달리 타입 문맥이 없다. HTTP 경로는 쿼리 파라미터를
       항상 str 로 주므로 실서버에서는 100% 죽었고, `_질의` 가 예외를 삼켜 빈 리스트를
       주는 바람에 호출부가 그걸 404 로 바꿨다 — **격리가 막은 게 아니라 고장이었다.**
       (인수인계 기록 `백엔드_동결상태_0901.md` 는 "쿼리 자체가 매번 죽는다"고만 적었는데,
        정확히는 «str 로 바인딩될 때» 다. uuid 객체면 살아서, 원인을 좁히는 데 이 구분이 필요하다.)

       고칠 길이 둘이었다. 캐스트(`%s::text IS NULL`)도 실측으로 되살아나지만,
       그러면 **org_id 없는 게스트가 남의 기관 L3 를 읽는다** (`NULL → 조건 통과` —
       이것도 태워서 확인했다). 그래서 `_org조건()` 파이썬 분기를 쓴다:
       `l3_documents.org_id` 는 NOT NULL 이라 게스트(`org_id IS NULL`)는 0행 → 404 다.
       판정 경로가 이미 쓰는 관용구와 같아지는 것은 덤이다.
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
