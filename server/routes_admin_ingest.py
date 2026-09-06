# -*- coding: utf-8 -*-
"""G3(2026-09-06, 레인 L3) — POST /admin/ingest · GET /admin/parse_report ·
POST /admin/ingest/weekly.   **[적재 API 계통]**

`LLM제외_백엔드_구현_작업_TASK.md` §7-G 명세를 따른다. L1(수집·ai-53)·L2(파싱검산·ai-47)를
잇는 자리 — 이 파일은 **적재 orchestration** 만 한다. 크롤링도 표 파싱 엔진도 새로 안 짠다.

🔴 **적재 경계(이 파일에서 제일 중요한 규칙)**
```
파싱 실패(fail)          -> corpus 에 «안 들어간다». 사람 확인 대기(GET /admin/parse_report)
품질 낮음(warn, low)     -> corpus 에 들어가되 parse_quality='low' 로 표시
품질 높음(pass, high)    -> corpus 에 들어간다(parse_quality='high')
```
🔴 **그런데 pass 여도 `documents.status` 는 자동으로 'active' 가 되지 않는다.**
   TASK.md §7-G 마지막 줄: 「자동화가 조용히 낡은 것을 덮어쓰는 방향은 금지다. 새 판을
   감지하면 recheck_queue 에 «올리기만» 하고, documents.status 를 바꾸는 것은 사람
   승인 뒤다.」

   🔴 **2026-09-06 오너 결정(ai-8c 전달) — 자리가 확정됐다.** 새 테이블을 만들지 않고
   기존 `corpus.documents`·`doc_articles`·`chunks` «그대로» 쓰되, `status='staged'`
   로 넣는다(신규 CHECK 값 하나 추가 — DDL 은 중앙이 친다, 이 파일은 SQL 문구만
   제안한다. 아래 「DDL 제안」 참조). 사람이 검수해 `staged → active` 로 한 줄
   UPDATE 하면 «같은 순간» 옛 판이 `active → superseded` 로 내려간다(그 UPDATE
   자체는 이 파일 범위 밖 — 승인 엔드포인트는 아직 없다).
   근거는 `scripts/retrieve.py:41` 의 판정 검색 필터: `status='active' AND
   parse_quality='high' AND retrieval_scope='진입점' AND layer IN ('L1','L2')`.
   **모든 판정 쿼리가 이미 `status='active'` 를 건다** — `staged` 는 이 필터를
   구조적으로 통과 못 한다. 「깜빡하고 새는」게 아니라 「필터를 깜빡할 자리 자체가
   없다」. 이점 셋:
     ① 격리가 규칙이 아니라 «구조» 다 — 코드 리뷰로 지켜야 하는 관례가 아니다
     ② 검수 전에도 DB 실측(표 행수·조 개수·dangling 건수)을 그대로 볼 수 있다 —
        파일만 보고 판단하는 것보다 정확하다
     ③ 되돌리기가 한 줄이다 — 잘못 승인해도 `active → staged` UPDATE 하나로 원복

   🔴 **parse_quality 는 새 어휘를 안 만든다 — 기존 high/low 를 그대로 쓴다.**
   `corpus.documents`·`chunks` 의 `parse_quality` CHECK 는 이미 high|low 뿐이고
   `retrieve.py:41` 이 문자열 `'high'` 를 정확히 문다 — 여기에 pass/warn/fail 을
   또 얹으면 «두 어휘가 같은 칸을 다르게 부르는» 혼선이 난다(L3 의 `파싱품질`
   대기/pass/warn/fail 은 «다른 테이블»(`tenant.l3_documents`)의 다른 칸이라 안
   섞인다). 그래서 이 파일의 라우팅 계산은 이렇게 접는다 —
     fail  = corpus 에 «아예 안 들어간다»(그래서 저장할 칸이 필요 없다.
             API 응답과 서버 로그가 fail 의 유일한 기록이다)
     warn  = parse_quality='low' 로 들어간다(=TASK §7-G3 다섯 규칙 중 하나라도 걸림)
     pass  = parse_quality='high' 로 들어간다(다섯 규칙 전부 통과)

   🔴 **embedding — «승인 시 생성» 으로 정한다(ai-8c 의견에 동의).** staged 행은
   `retrieve.py:41` 필터(`status='active'`)를 어차피 못 통과하므로, staged 시점에
   embedding 을 만들어도 승인 전까지는 «아무 데도 안 쓰인다» — 순수 비용이다.
   반대로 검수 중 표가 밀린 게 드러나 재적재하면 그 embedding 은 버려진다(이중
   낭비). 그래서 이 파일은 `chunks.embedding` 을 **NULL 로 남긴다** — 승인
   엔드포인트(범위 밖)가 `active` 로 올릴 때 임베딩 배치를 태우는 게 다음 자리다.

🔴 **판정 인덱스 경계도 이 층에서 지킨다.** `index_guard.assert_indexable()` 을
   경로·layer 판정에 태운다 — 실패하면 그 자체로 `fail` 라우팅이다(품질 문제가
   아니라 «애초에 넣을 자리가 아님»).
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

# 🔴 scripts/ 를 import 하려면 sys.path 에 있어야 한다 — `main.py` 가 이미 걸어두지만
#    이 라우터가 단독 임포트될 경우(테스트 등)를 대비해 방어적으로 한 번 더 건다.
_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

router = APIRouter(prefix="/admin", tags=["관리·적재"])


def _관리자(token: str | None) -> None:
    """🔴 `main.py:_관리자()` 와 «같은 규칙» 이다 — 중복이지 다른 정책이 아니다.

    `.main` 을 여기서 import 하면 순환참조(main 이 이 라우터를 include 한다)가 나서
    복붙했다. 정책이 갈리면(토큰 이름 등) 여기와 `main.py` 둘 다 고쳐야 한다.
    """
    기대 = os.environ.get("SUDDOE_ADMIN_TOKEN", "")
    if not 기대 or token != 기대:
        raise HTTPException(403, "관리자 토큰이 필요합니다 (SUDDOE_ADMIN_TOKEN)")


# ════════════════════════════════════════════════════════════════════
# ① parse_quality 자동판정 — TASK.md §7-G3, 설계 그대로 다섯 규칙
# ════════════════════════════════════════════════════════════════════

def parse_quality_판정(조목록: list[dict], *, extraction: str,
                     표_문서: bool, 목차_일치: bool | None) -> tuple[Literal["high", "low"], list[str]]:
    """다섯 규칙을 «순서대로» 검사한다. 하나라도 걸리면 'low' — 사유를 전부 모은다
    (첫 걸림에서 멈추지 않는다. 사람이 `/admin/parse_report` 에서 «전부» 봐야 한다).
    """
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


# ════════════════════════════════════════════════════════════════════
# ② recheck_queue 연계 — «재발명하지 않는다», agent_a4 엔진을 그대로 부른다
# ════════════════════════════════════════════════════════════════════

def _직전활성판_찾기(conn, doc_id: str) -> str | None:
    """같은 규범군(family)의 현재 'active' 문서 doc_id. 없으면 None(=최초 적재, diff 대상 없음)."""
    from archive.agents import agent_a4 as a4          # noqa: E402  (archive/ «읽기» — 수정 아님)
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
    """신doc(방금 적재한 문서) 을 구doc(현재 active) 과 조 단위로 대조해
    `corpus.recheck_queue` 에 올린다. `agent_a4` 의 조_매칭·판정_변경·영향_레코드·적재를
    «그대로» 쓴다 — G2 사유코드(BASIS_AMENDED·BASIS_RENUMBERED·UPSTREAM_LAW_AMENDED 등)
    는 그 모듈이 이미 정본이다.
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


# ════════════════════════════════════════════════════════════════════
# ③′ W3(2026-09-06, 레인 W3, 오너 지시) — VLM 분기
# ════════════════════════════════════════════════════════════════════
# 🔴 구멍이 실측으로 드러났다 — 「2026년 재도전성공패키지 세부관리기준」이 fail(53자)
#    난 이유는 «규정이 아니라서» 가 아니라 «스캔본» 이라서다. pdftext 는 페이지번호
#    5자만 준다. DB 엔 이미 extraction='vlm' 으로 사람이 판독해 들어가 있다 —
#    지금 구조로는 «스캔본 규정집이 자동화로는 영원히 fail» 이었다.

# 🔴 «같은 값을 세 곳이 쓴다» — `stage0_extract.추출_품질_점검()` 의 판단불가
#    임계치를 «재사용» 한다. 새 숫자를 만들면 L3(ai-7d)·검산(ai-47)과 다른 기준으로
#    갈려 「셋이 다르면 못 읽는다」가 그대로 벌어진다.
from stage0_extract import 빈_추출_글자수_임계치 as VLM_임계_글자수  # noqa: E402

try:
    import vlm_extract as _vlm          # ai-47 소유 모듈. 2026-09-06 현재 저장소에 없다
except ImportError:
    _vlm = None


def _vlm_페이지수_추정(path: Path) -> int | None:
    """⑤ 비용추정 전용 — VLM 을 «부르지 않고» 페이지 수만 본다(pypdf, 이미 의존성에 있다:
    `stage0_extract._pdf_pypdf` 가 이미 pypdf 를 쓴다 — 새 의존성 아니다)."""
    try:
        import pypdf
        return len(pypdf.PdfReader(str(path)).pages)
    except Exception:                                          # noqa: BLE001
        return None


def _이미_판독됨(doc_id: str, version: str | None, 시행일) -> bool:
    """④ 비용 가드 — 같은 판을 매주 다시 판독하지 않는다.

    🔴 「수집기(ai-53) 앞단에서 걸러지는지 확인하라」는 지시를 받았는데 그 코드가
    아직 이 저장소에 없다(G1 미착수) — 그래서 «여기서도» 막는다. 이중 방어가 맞다:
    수집기가 나중에 걸러도, 스케줄러 밖에서 누가 `/admin/ingest` 를 직접 재호출하면
    이 층이 없으면 여전히 매번 새로 판독한다.
    """
    행 = _질의("SELECT extraction, version, 시행일 FROM corpus.documents WHERE doc_id=%s",
              (doc_id,))
    if not 행:
        return False
    ext, v, d = 행[0]
    if ext != "vlm":
        return False
    return (version is not None and v == version) or (시행일 is not None and d == 시행일)


# ════════════════════════════════════════════════════════════════════
# ③ 요청/응답 모양
# ════════════════════════════════════════════════════════════════════

class 적재요청(BaseModel):
    doc_id: str
    src_path: str                          # L1 수집기(ai-53)가 이미 받아둔 로컬 경로
    layer: Literal["L1", "L2"]
    사업명: str | None = None
    도메인: str | None = None
    기관ID: str | None = None
    doc_type: str | None = None
    시행일: date | None = None
    version: str | None = None
    표_문서: bool = False                   # 별표·붙임·참고 위주 문서인지(호출부가 판단해 알려준다)
    dry: bool = False                       # True 면 판정만 하고 아무것도 안 쓴다
    # W3(2026-09-06, 레인 W3) — ⑤ VLM 비용만 미리 보고 싶을 때. True 면 VLM 을
    # «부르지 않고» 페이지 수·예상 호출 수만 준다(문서 자체는 안 들어간다).
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

    # ── 0) 판정 인덱스 경계 — 품질 이전에 «자리 자체» 가 맞는지 ──────────
    from archive.eval import index_guard
    사유 = index_guard.reject_reason(body.src_path, body.layer)
    if 사유:
        return 적재응답(doc_id=body.doc_id, 라우팅="fail", 사유=[f"index_guard 거부 — {사유}"])

    # ── 1) 추출 ──────────────────────────────────────────────────────
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
            # ── W3 VLM 분기 — 텍스트가 «거의 없다»(임계 미만). 스캔본 의심 ──
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
                # 🔴 진단용(`extract_meta`)을 쓴다 — `판독불가_페이지` 가 ③(표 검산)의
                #    재료다. 반환은 (본문, {페이지오프셋, vlm_페이지, 판독불가_페이지, ...})
                #    — `extract()` 단순형이 아니라 «튜플» 이다(먼저 짰다가 확인 없이
                #    문자열로 받는 실수를 했었다 — 실측으로 잡았다).
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

    # ── 2) parse_quality 자동판정 (TASK §7-G3 다섯 규칙) ────────────────
    quality, 품질사유 = parse_quality_판정(조목록, extraction=extraction,
                                        표_문서=body.표_문서, 목차_일치=None)

    # ② W3 — VLM 판독분은 «pass 를 주지 않는다». 다섯 규칙을 전부 통과해도 사람
    #    검수를 거치게 한 단 낮춘다 — 판독은 틀릴 수 있다(모델이 표를 잘못 읽고도
    #    형식은 멀쩡할 수 있다).
    if vlm_사용 and quality == "high":
        quality = "low"
        품질사유 = [*품질사유, "VLM 판독본은 다섯 규칙을 다 통과해도 최고등급을 안 준다(사람 검수 필수)"]

    # ③ W3 — 표 문서인데 VLM 이 표를 못 살렸으면 warn 이 아니라 «fail». 별표·
    #    한도표가 판정 재료라, 표가 깨진 채로 들어가면 «틀린 확신» 이 된다
    #    (rule450 자작 예외와 같은 급의 사고 — 검수자가 «품질 낮음» 배지만 보고
    #    안 열어볼 위험까지 감안해 아예 안 들어가게 한다).
    #    🔴 «ai-47 표 검산과 같은 잣대» — `vlm_extract.extract_meta()` 가 이미
    #    프롬프트에서 [판독불가] 마커로 표를 명시적으로 자백하게 시켜뒀다
    #    (표 안 셀을 못 읽으면 지어내지 말고 마커를 남기라는 지시가 모듈 안에 있다).
    #    그 신호(`판독불가_페이지`)를 «직접» 쓴다 — 표 행 카운트(파이프 개수)는
    #    vlm메타 가 없을 때만 쓰는 보조 신호다.
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

    # ── 3) 적재 — status='staged' · index_target=False 로 «항상» 시작 ──
    #    (자동 승격 금지. 승인은 사람이 별도 절차로 한다. 'staged' 는 2026-09-06
    #    오너 결정으로 documents_status_check 에 추가되는 값이다 — DDL 은 중앙이
    #    친다. 그 전까지는 이 INSERT 가 CHECK 위반으로 막힌다 — «조용히 새지
    #    않는다», 실패 자체가 「DDL 이 아직 안 왔다」는 신호다.)
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
            # 재적재 대비 — 이 doc_id 의 옛 조·청크를 지우고 새로 넣는다(문서 «단위» 재현성)
            conn.execute("DELETE FROM corpus.doc_articles WHERE doc_id = %s", (body.doc_id,))
            conn.execute("DELETE FROM corpus.chunks WHERE doc_id = %s", (body.doc_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO corpus.doc_articles
                           (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제)
                       VALUES (%s,%s,%s,%s,%s,%s,false)""",
                    [(body.doc_id, a.get("조번호"), a.get("조제목"), a.get("조번호_int"),
                      a.get("본문"), a.get("페이지")) for a in 조목록])
            # 🔴 article_id 는 시퀀스 발급이라 위 executemany 직후 다시 읽어야 한다.
            #    «위치로» 원본 조목록과 짝짓는다(조번호로 다시 찾으면 중복 조번호가
            #    있을 때 엉뚱한 본문이 붙는다) — article_id 순서 == 위 executemany 의
            #    리스트 순서다(같은 트랜잭션 안의 단일 시퀀스 발급이라 순서가 보장된다).
            신조_id행 = conn.execute(
                "SELECT article_id FROM corpus.doc_articles "
                "WHERE doc_id=%s ORDER BY article_id", (body.doc_id,)).fetchall()
            신조 = [(신조_id행[i][0], a.get("조번호"), a.get("조제목"), a.get("페이지"), a.get("본문"))
                   for i, a in enumerate(조목록)]
            # 🔴 **1조=1청크 단순 매핑이다.** `scripts/archive/indexing/stage2_chunk.py`
            #    가 토큰 길이 기준으로 조를 쪼개고 합치는 «정식» 로직을 갖고 있지만
            #    한 문서만 다시 태우는 재사용 가능한 함수를 노출하지 않는다(모놀리식
            #    `main()` 뿐). staged 검수 단계는 "표·조가 제대로 들어왔나" 를 보는
            #    자리라 조 단위로도 충분하다 — active 승격 시점에 그 모듈로 다시
            #    쪼개 넣는 것을 승인 파이프라인의 다음 자리로 남긴다(이 파일 범위 밖).
            #    embedding 은 NULL — 위 설계 근거(승인 시 생성) 참조.
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
            # ── 4) recheck_queue — 신규는 항상 시도한다(구판 없으면 함수가 조용히 스킵)
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
    """G4 — parse_quality='low' 목록 + 사유 + 재처리 버튼 재료.

    🔴 「fail」은 여기 안 나온다 — fail 은 애초에 `corpus.documents` 에 안 들어가서
       보여줄 행이 없다. fail 이력은 이 엔드포인트가 아니라 서버 로그가 진실이다
       (`_log.exception` — TASK.md 어디에도 fail 이력 테이블이 없다. 필요해지면
       별도 요청으로 만든다. 지금 지어내지 않는다).
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
    """**매주 월요일 오전 10시(KST) Cloud Scheduler 가 치는 자리로 설계했다.**

    `server/gpu_watchdog.py:gpu_reap()` 과 «같은 자리」다 — 1회 실행 엔드포인트를
    먼저 굳히고, 실제 Scheduler 등록은 오너 승인 뒤 중앙이 한다(TASK 지시).

    🔴 **지금은 미배선이다.** `_수집_대기_목록()` 이 스텁이다 — G1(ai-53) 이 만드는
    발행처별 수집기가 아직 없어서, 오늘 「이미 로컬에 받아둔 원본이 있는지」만 본다
    (`_l3_수집_대기/` 디렉터리 관례 — 없으면 빈 배치로 끝난다. 고장이 아니다).
    G1 이 실제 목록을 주기 시작하면 이 함수의 스텁 부분만 바꾸면 된다 — 나머지
    (한 건씩 `admin_ingest()` 호출 → 집계) 는 이미 돈다.

    🔴 인증 — `gpu_reap()` 과 같은 이유로 관리자 토큰을 요구한다(그쪽은 fail-open
    이지만 여긴 **DB 에 쓰는» 호출이라 fail-closed 인 `_관리자()` 를 그대로 쓴다 —
    gpu_reap 과 위험 등급이 다르다. 스케줄러 쪽 설정에 헤더로 토큰을 실어야 한다.
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
    """🔴 스텁 — G1(ai-53) 이 발행처별 수집기를 붙이면 이 함수만 바뀐다.

    지금은 `_l3_수집_대기/*.json`(각 파일이 `적재요청` 모양) 을 로컬에서 읽는다 —
    수집과 적재 사이를 파일로 느슨하게 잇는 임시 다리다. 디렉터리가 없으면 빈 배치.
    """
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
