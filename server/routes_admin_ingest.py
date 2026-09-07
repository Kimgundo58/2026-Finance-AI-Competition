# -*- coding: utf-8 -*-
"""적재 API — POST /admin/ingest · GET /admin/parse_report · POST /admin/ingest/weekly.

파싱 품질에 따라 fail(코퍼스 미반영)·warn(parse_quality='low')·pass(parse_quality='high')로
라우팅한다. 적재는 항상 status='staged' 로 들어가고 active 승격은 사람 승인(범위 밖)이
한다 — 판정 쿼리는 모두 status='active' 만 보므로 staged 는 구조적으로 판정에 안 걸린다.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ._common import _질의, _실행

_log = logging.getLogger("suddoe.admin_ingest")

# scripts/ 를 import 하려면 sys.path 에 있어야 한다 — main.py 가 이미 걸어두지만
# 이 라우터가 단독 임포트될 경우(테스트 등)를 대비해 방어적으로 한 번 더 건다
_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

router = APIRouter(prefix="/admin", tags=["관리·적재"])


def _관리자(token: str | None) -> None:
    """관리자 토큰을 검사한다 — `main.py:_관리자()` 와 같은 규칙이다(순환참조를 피하려 복붙했다)."""
    기대 = os.environ.get("SUDDOE_ADMIN_TOKEN", "")
    if not 기대 or token != 기대:
        raise HTTPException(403, "관리자 토큰이 필요합니다 (SUDDOE_ADMIN_TOKEN)")


# parse_quality 자동판정 — 다섯 규칙

def parse_quality_판정(조목록: list[dict], *, extraction: str,
                     표_문서: bool, 목차_일치: bool | None) -> tuple[Literal["high", "low"], list[str]]:
    """다섯 규칙을 순서대로 검사한다. 하나라도 걸리면 'low' — 사유를 전부 모아 반환한다."""
    사유 = []
    if len(조목록) == 0:
        사유.append("조 개수 0 — 문자중복 레이어 의심(`제제5조조` 류)")
    조번호_int들 = [a.get("조번호_int") for a in 조목록 if a.get("조번호_int") is not None]
    if len(조번호_int들) >= 2 and any(b <= a for a, b in zip(조번호_int들, 조번호_int들[1:])):
        사유.append("조번호가 비단조 — 2단 조판이 섞였을 수 있다")
    if extraction == "vlm":
        사유.append("스캔 판독본(extraction='vlm')")
    if 표_문서:
        표행수 = sum(a.get("본문", "").count("|") for a in 조목록 if "참고" in (a.get("조번호") or "")
                   or "붙임" in (a.get("조번호") or "") or "별지" in (a.get("조번호") or ""))
        if 표행수 < 3:
            사유.append("표 문서인데 표 행 3줄 미만 — 별표가 뭉개졌을 수 있다")
    if 목차_일치 is False:
        사유.append("조 목록이 목차와 안 맞는다")
    return ("low" if 사유 else "high"), 사유


# recheck_queue 연계 — agent_a4 엔진을 그대로 부른다

def _직전활성판_찾기(conn, doc_id: str) -> str | None:
    """같은 규범군(family)의 현재 'active' 문서 doc_id. 없으면 None(=최초 적재, diff 대상 없음)."""
    from archive.agents import agent_a4 as a4          # noqa: E402 — archive/ 는 읽기 전용, 수정 아님
    키 = a4.family(doc_id)
    if not 키:
        return None
    rows = conn.execute(
        "SELECT doc_id FROM corpus.documents WHERE status='active'").fetchall()
    for (d,) in rows:
        if a4.family(d) == 키:
            return d
    return None


def _recheck_큐_반영(conn, *, 신doc: str, 구doc: str | None, dry: bool) -> dict:
    """신doc(방금 적재한 문서)을 구doc(현재 active)과 조 단위로 대조해 corpus.recheck_queue 에 올린다.

    조 매칭·판정 변경·영향 레코드 산출은 agent_a4 모듈을 그대로 쓴다.
    """
    if 구doc is None:
        return {"대상": None, "비교조": 0, "레코드": 0, "메시지": "같은 규범군의 현행판이 없다 — 최초 적재"}
    from archive.agents import agent_a4 as a4          # noqa: E402

    구 = a4.조_읽기(conn, 구doc)
    신 = a4.조_읽기(conn, 신doc)
    쌍들 = a4.조_매칭(구, 신)
    recs: list[dict] = []
    for 쌍 in 쌍들:
        변경 = a4.판정_변경(쌍)
        if 변경["변경유형"] == "동일":
            continue
        recs.extend(a4.영향_레코드(conn, 신doc, 구doc, 쌍, 변경))
    recs = a4.접기(recs)
    n, 메시지 = a4.적재(conn, recs, dry=dry)
    return {"대상": 구doc, "비교조": len(쌍들), "레코드": len(recs), "반영": n, "메시지": 메시지}


# VLM 분기 — 텍스트 추출량이 임계 미만이면 스캔본으로 보고 VLM 판독을 시도한다

# 판단불가 임계치를 stage0_extract 에서 재사용한다 — 여러 곳이 같은 기준을 쓰게 한다
from stage0_extract import 빈_추출_글자수_임계치 as VLM_임계_글자수  # noqa: E402

try:
    import vlm_extract as _vlm          # 없으면 None — VLM 판독이 필요한 경로는 fail 로 처리한다
except ImportError:
    _vlm = None


def _vlm_페이지수_추정(path: Path) -> int | None:
    """VLM 을 부르지 않고 페이지 수만 추정한다(비용추정 전용, pypdf 사용)."""
    try:
        import pypdf
        return len(pypdf.PdfReader(str(path)).pages)
    except Exception:                                          # noqa: BLE001
        return None


def _이미_판독됨(doc_id: str, version: str | None, 시행일) -> bool:
    """같은 판(version 또는 시행일)이 이미 extraction='vlm' 으로 판독돼 있으면 True — 재판독 비용 가드."""
    행 = _질의("SELECT extraction, version, 시행일 FROM corpus.documents WHERE doc_id=%s",
              (doc_id,))
    if not 행:
        return False
    ext, v, d = 행[0]
    if ext != "vlm":
        return False
    return (version is not None and v == version) or (시행일 is not None and d == 시행일)


# 요청/응답 모델

class 적재요청(BaseModel):
    doc_id: str
    src_path: str                          # L1 수집기가 받아둔 로컬 경로
    layer: Literal["L1", "L2"]
    사업명: str | None = None
    도메인: str | None = None
    기관ID: str | None = None
    doc_type: str | None = None
    시행일: date | None = None
    version: str | None = None
    표_문서: bool = False                   # 별표·붙임·참고 위주 문서인지(호출부가 판단해 알려준다)
    dry: bool = False                       # True 면 판정만 하고 아무것도 안 쓴다
    # True 면 VLM 을 부르지 않고 페이지 수·예상 호출 수만 준다(문서는 적재하지 않는다)
    vlm_비용추정만: bool = False


class 적재응답(BaseModel):
    doc_id: str
    라우팅: Literal["fail", "warn", "pass"]
    parse_quality: Literal["high", "low"] | None = None
    조_개수: int = 0
    사유: list[str] = Field(default_factory=list)
    recheck_queue: dict[str, Any] | None = None
    dry: bool = False
    vlm_사용: bool = False
    vlm_비용추정: dict[str, Any] | None = None  # {"페이지수":.., "호출예상":..}


@router.post("/ingest", response_model=적재응답)
def admin_ingest(body: 적재요청,
                  x_admin_token: str | None = Header(default=None)) -> 적재응답:
    _관리자(x_admin_token)

    # 0) 판정 인덱스 경계 — 품질 이전에 자리 자체가 맞는지 확인한다
    from archive.eval import index_guard
    사유 = index_guard.reject_reason(body.src_path, body.layer)
    if 사유:
        return 적재응답(doc_id=body.doc_id, 라우팅="fail", 사유=[f"index_guard 거부 — {사유}"])

    # 1) 추출
    from stage0_extract import extract as _extract
    try:
        종류, payload = _extract(Path(body.src_path))
    except Exception as e:                                     # noqa: BLE001
        _log.exception("ingest 추출 실패 doc_id=%s", body.doc_id)
        return 적재응답(doc_id=body.doc_id, 라우팅="fail",
                      사유=[f"추출 실패 — {type(e).__name__}: {e}"])

    확장 = Path(body.src_path).suffix.lower().lstrip(".")
    extraction = {"hwp": "hancom" if 확장 == "hwp" else "native",
                  "hwpx": "native", "pdf": "native", "xml": "native"}.get(확장, "native")

    vlm_사용 = False
    vlm_비용추정: dict[str, Any] | None = None
    vlm메타: dict[str, Any] | None = None
    if 종류 == "articles":
        조목록 = payload
    else:
        본문, 오프셋 = payload
        from stage0_extract import 추출_품질_점검
        점검 = 추출_품질_점검(본문)

        if 점검["글자수"] < VLM_임계_글자수:
            # 텍스트가 임계 미만 — 스캔본 의심, VLM 분기로 들어간다
            if body.vlm_비용추정만:
                페이지수 = _vlm_페이지수_추정(Path(body.src_path))
                return 적재응답(doc_id=body.doc_id, 라우팅="fail",
                              사유=["비용추정 모드 — 아무것도 적재하지 않았다"],
                              vlm_비용추정={"페이지수": 페이지수,
                                        "호출예상": 페이지수 if 페이지수 else "페이지수 확인 불가"})
            if _이미_판독됨(body.doc_id, body.version, body.시행일):
                return 적재응답(doc_id=body.doc_id, 라우팅="fail",
                              사유=["④ 이미 같은 판이 extraction='vlm' 으로 판독돼 있다 — "
                                    "재판독 안 함(비용 가드). 원래 doc_id 를 그대로 쓴다"])
            if _vlm is None:
                return 적재응답(doc_id=body.doc_id, 라우팅="fail",
                              사유=[f"텍스트 {점검['글자수']}자(임계 {VLM_임계_글자수}자 미만) — "
                                    "스캔본으로 보여 VLM 판독이 필요하나 scripts/vlm_extract.py "
                                    "를 못 찾았다(ai-47 모듈 미착수 또는 이 배포에 미포함) — 못 태웠다"])
            try:
                # extract_meta 는 (본문, 메타dict) 튜플을 반환한다 — 메타의 판독불가_페이지 를 표 검산에 쓴다
                본문, vlm메타 = _vlm.extract_meta(Path(body.src_path))
            except Exception as e:                               # noqa: BLE001
                _log.exception("VLM 판독 실패 doc_id=%s", body.doc_id)
                return 적재응답(doc_id=body.doc_id, 라우팅="fail",
                              사유=[f"VLM 판독 실패 — {type(e).__name__}: {e} — 못 태웠다"])
            extraction, vlm_사용 = "vlm", True
            오프셋 = vlm메타["페이지오프셋"]
            점검 = 추출_품질_점검(본문)
            if 점검["판단불가"]:
                return 적재응답(doc_id=body.doc_id, 라우팅="fail", vlm_사용=True,
                              사유=[f"VLM 판독도 텍스트가 부족하다 — {점검['사유']}"])
        elif 점검["판단불가"]:
            return 적재응답(doc_id=body.doc_id, 라우팅="fail", 사유=[점검["사유"]])

        from stage0_articles import split_articles
        조목록, _전략 = split_articles(본문, 오프셋, doc_id=body.doc_id)
        if not 조목록:
            return 적재응답(doc_id=body.doc_id, 라우팅="fail", vlm_사용=vlm_사용,
                          사유=["조 분해 실패 — split_articles 가 0건을 냈다"])

    # 2) parse_quality 자동판정 (다섯 규칙)
    quality, 품질사유 = parse_quality_판정(조목록, extraction=extraction,
                                        표_문서=body.표_문서, 목차_일치=None)

    # VLM 판독분은 pass 를 주지 않는다 — 다섯 규칙을 통과해도 사람 검수를 거치게 낮춘다
    if vlm_사용 and quality == "high":
        quality = "low"
        품질사유 = [*품질사유, "VLM 판독본은 다섯 규칙을 다 통과해도 최고등급을 안 준다(사람 검수 필수)"]

    # 표 문서인데 VLM 이 표를 못 살렸으면 warn 이 아니라 fail — 판독불가_페이지 를 우선
    # 신호로 쓰고, 없으면 표 행 카운트(파이프 개수)를 보조 신호로 쓴다
    if vlm_사용 and body.표_문서:
        if vlm메타 and vlm메타.get("판독불가_페이지"):
            return 적재응답(doc_id=body.doc_id, 라우팅="fail", vlm_사용=True,
                          사유=[f"VLM 판독이 표를 못 살렸다 — 판독불가 페이지 {vlm메타['판독불가_페이지']}. "
                                "warn 이 아니라 fail 이다"])
        표행수 = sum(a.get("본문", "").count("|") for a in 조목록)
        if 표행수 < 3:
            return 적재응답(doc_id=body.doc_id, 라우팅="fail", vlm_사용=True,
                          사유=["VLM 판독이 표를 못 살렸다(표 문서인데 표 행 3줄 미만) — "
                                "warn 이 아니라 fail 이다. ai-47 표 검산과 같은 잣대"])

    라우팅: Literal["warn", "pass"] = "warn" if quality == "low" else "pass"

    if body.dry:
        return 적재응답(doc_id=body.doc_id, 라우팅=라우팅, parse_quality=quality,
                      조_개수=len(조목록), 사유=품질사유, dry=True, vlm_사용=vlm_사용)

    # 3) 적재 — status='staged' · index_target=False 로 항상 시작한다. 자동 승격은 없다
    import psycopg
    from psycopg.rows import tuple_row
    from ._common import DSN
    with psycopg.connect(DSN, connect_timeout=10) as conn:
        conn.row_factory = tuple_row
        try:
            conn.execute("""
                INSERT INTO corpus.documents
                    (doc_id, layer, domain, 기관id, doc_type, version, 시행일,
                     status, parse_quality, extraction, src_path, index_target, retrieval_scope)
                VALUES (%s,%s,%s,%s,%s,%s,%s, 'staged', %s, %s, %s, false, '진입점')
                ON CONFLICT (doc_id) DO UPDATE SET
                    parse_quality = EXCLUDED.parse_quality,
                    src_path = EXCLUDED.src_path
            """, (body.doc_id, body.layer, body.도메인, body.기관ID, body.doc_type,
                  body.version, body.시행일, quality, extraction, body.src_path))
            # 재적재 대비 — 이 doc_id 의 옛 조·청크를 지우고 새로 넣는다
            conn.execute("DELETE FROM corpus.doc_articles WHERE doc_id = %s", (body.doc_id,))
            conn.execute("DELETE FROM corpus.chunks WHERE doc_id = %s", (body.doc_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO corpus.doc_articles
                           (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제)
                       VALUES (%s,%s,%s,%s,%s,%s,false)""",
                    [(body.doc_id, a.get("조번호"), a.get("조제목"), a.get("조번호_int"),
                      a.get("본문"), a.get("페이지")) for a in 조목록])
            # article_id 는 시퀀스라 executemany 직후 다시 읽는다 — 위치로 원본 조목록과 짝짓는다
            신조_id행 = conn.execute(
                "SELECT article_id FROM corpus.doc_articles "
                "WHERE doc_id=%s ORDER BY article_id", (body.doc_id,)).fetchall()
            신조 = [(신조_id행[i][0], a.get("조번호"), a.get("조제목"), a.get("페이지"), a.get("본문"))
                   for i, a in enumerate(조목록)]
            # 1조=1청크 단순 매핑이다 — 토큰 길이 기준 재분할은 active 승격 시점(범위 밖)의
            # 몫이다. embedding 은 NULL 로 남기고 승인 시 생성한다
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO corpus.chunks
                           (doc_id, article_id, layer, 기관id, parse_quality, version,
                            status, retrieval_scope, 조번호, 조제목, 페이지, 사업명,
                            적용대상, text, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s, 'staged', '진입점', %s,%s,%s,%s,%s,%s, NULL)""",
                    [(body.doc_id, aid, body.layer, body.기관ID, quality, body.version,
                      jo, title, page, [body.사업명] if body.사업명 else None,
                      "공통", 본문)
                     for aid, jo, title, page, 본문 in 신조])
            # 4) recheck_queue — 신규는 항상 시도한다(구판이 없으면 함수가 조용히 스킵한다)
            구doc = _직전활성판_찾기(conn, body.doc_id)
            recheck = _recheck_큐_반영(conn, 신doc=body.doc_id, 구doc=구doc, dry=False)
            conn.commit()
        except Exception as e:                                 # noqa: BLE001
            conn.rollback()
            _log.exception("ingest 적재 실패 doc_id=%s", body.doc_id)
            raise HTTPException(500, f"적재 실패 — {type(e).__name__}: {e}") from e

    return 적재응답(doc_id=body.doc_id, 라우팅=라우팅, parse_quality=quality,
                  조_개수=len(조목록), 사유=품질사유, recheck_queue=recheck, vlm_사용=vlm_사용)


@router.get("/parse_report")
def admin_parse_report(x_admin_token: str | None = Header(default=None),
                        layer: str | None = None) -> dict:
    """parse_quality='low' 목록과 사유를 준다 — 재처리 버튼 재료.

    fail 은 corpus.documents 에 아예 안 들어가므로 여기 안 나온다 — fail 이력은 서버 로그에만 남는다.
    """
    _관리자(x_admin_token)
    where, 인자 = ["parse_quality = 'low'"], []
    if layer:
        where.append("layer = %s")
        인자.append(layer)
    조건 = " AND ".join(where)
    행 = _질의(f"""SELECT doc_id, layer, 사업명 as 사업명, status, extraction, src_path
                  FROM corpus.documents d
                  LEFT JOIN corpus.programs p ON false
                  WHERE {조건} ORDER BY doc_id""", tuple(인자))
    return {
        "건수": len(행),
        "항목": [{"doc_id": d, "layer": l, "status": s, "extraction": e, "src_path": p}
                for d, l, _, s, e, p in 행],
        "비고": "재처리는 POST /admin/ingest 를 같은 doc_id 로 다시 호출한다(문서 단위 재현성).",
    }


@router.post("/ingest/weekly")
def admin_ingest_weekly(x_admin_token: str | None = Header(default=None)) -> dict:
    """대기 목록을 하나씩 admin_ingest() 에 넘기는 주 1회 배치(Cloud Scheduler 가 칠 자리).

    `_수집_대기_목록()` 이 아직 스텁이라 지금은 `_l3_수집_대기/` 의 로컬 파일만 본다.
    DB 에 쓰는 호출이라 관리자 토큰을 요구한다(fail-closed).
    """
    _관리자(x_admin_token)
    대기 = _수집_대기_목록()
    결과 = []
    for item in 대기:
        try:
            r = admin_ingest(적재요청(**item), x_admin_token=x_admin_token)
            결과.append(r.model_dump())
        except HTTPException as e:                             # noqa: BLE001
            결과.append({"doc_id": item.get("doc_id"), "라우팅": "fail", "사유": [str(e.detail)]})
    return {"처리": len(결과), "항목": 결과}


def _수집_대기_목록() -> list[dict]:
    """수집 대기 목록 — 아직 스텁. `_l3_수집_대기/*.json` 을 읽어 적재요청 모양으로 반환한다."""
    import json
    폴더 = _ROOT / "_l3_수집_대기"
    if not 폴더.is_dir():
        return []
    out = []
    for f in sorted(폴더.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:                                       # noqa: BLE001
            _log.warning("수집 대기 파일 파싱 실패: %s", f)
    return out
