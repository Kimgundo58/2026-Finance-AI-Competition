# -*- coding: utf-8 -*-
"""L1·L2 규정 적재 파이프라인. 업로드 파일 한 건을 corpus 에 status='staged' 로 넣고, 활성화·삭제를 처리한다.

호출자: server/routes_ingest.py. 업로드는 접수() 뒤 백그라운드 작업으로 적재_실행() 을, 관리 요청은
활성화()·삭제()·문서_상태()·문서_목록() 을 부른다. FastAPI 는 import 하지 않는다.
흐름: 텍스트 추출(stage0_extract, PDF 는 pdftext) -> 조 분해·품질 판정(stage0_articles) -> 조별 적용대상 태깅
-> 청크 분할 -> KURE-v1 임베딩(retrieve.모델) -> 참조(build_refs.scan) -> 한 트랜잭션으로 저장.
BM25 색인 행(stage2_bm25 토큰화)은 활성화 트랜잭션에서 넣는다.
쓰는 테이블: corpus.ingest_jobs, documents, doc_articles, chunks, refs, chunk_len·chunk_terms·term_df(활성화 때),
tenant.judge_cache(활성화 때 삭제). 접속은 호출자가 넘기는 DSN(SUDDOE_INGEST_DSN, 롤 suddoe_ingest) 만 쓴다.
불변식: 추출 실패·조 0건은 failed 이고 staged 가 되지 않는다. staged 문서는 검색 필터(status='active')에
걸리지 않고 BM25 통계에도 들어가지 않는다. 적용대상을 정하지 못한 조는 청크로 만들지 않는다.
활성화는 한 트랜잭션에서 새 판을 active, 이전 판을 superseded 로 바꾼다.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from collections import Counter
from datetime import date
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

_log = logging.getLogger("suddoe.corpus_ingest")

최대_바이트 = 30 * 1024 * 1024

# 기존 L1·L2 행이 모두 이 값이다. llm_validate 가 인용 메타데이터로 함께 읽는다.
DOMAIN = "창업지원사업"

# 청크 분할 기준. 임베딩 모델(retrieve.모델)의 max_seq_length 가 1024 토큰이라, 컨텍스트 헤더(약 50토큰)를
# 붙여도 넘지 않도록 본문은 900토큰에서 자른다. 50자 미만 조각은 같은 조의 앞 조각에 붙인다.
청크_최대토큰 = 900
임베딩_최대토큰 = 1024
청크_최소글자 = 50

_적재_잠금 = threading.Lock()       # 임베딩은 CPU 를 오래 쓴다. 적재 작업은 한 번에 하나만 돈다
_토크나이저 = None


class 문서없음(LookupError):
    """doc_id 에 해당하는 업로드도 문서도 없다 (HTTP 404)."""


class 입력오류(ValueError):
    """메타데이터 값이 규칙에 맞지 않는다 (HTTP 422)."""


class 요청거부(Exception):
    """현재 상태에서 할 수 없는 요청이다 (HTTP 409). 후보는 활성화 때 이전 판으로 고를 수 있는 문서 목록이다."""

    def __init__(self, 사유: str, 후보: list[str] | None = None):
        super().__init__(사유)
        self.사유 = 사유
        self.후보 = 후보 or []


class _적재실패(Exception):
    """파싱 결과를 staged 로 둘 수 없다. 사유는 ingest_jobs.실패사유 로 남는다."""


# 업로드 접수

def 저장소() -> Path:
    """업로드 원본을 파싱이 끝날 때까지 두는 폴더. 파싱 결과는 DB 에 있으므로 원본은 파싱 뒤 지운다."""
    return Path(os.environ.get("SUDDOE_INGEST_DIR")
                or Path(tempfile.gettempdir()) / "suddoe_corpus_upload")


# 판을 나타내는 괄호 표기: (2025년), (2026년도), (제10차), (11차 개정), (개정본), (2025.3.1. 개정) 등.
# '(창업기업용)'·'(주관기관용)' 처럼 문서를 구별하는 괄호는 남겨야 서로 다른 문서가 같은 키가 되지 않는다.
_RE_판_괄호 = re.compile(
    r"[(（\[]\s*(?:"
    r"\d{4}\s*년도?|\d{4}[.\-]\s*\d{1,2}(?:[.\-]\s*\d{1,2})?\.?|제?\s*\d+\s*차"
    r")?\s*(?:개정(?:본|안)?|시행|판)?\s*[)）\]]")


def 문서키(문서명: str) -> str:
    """같은 문서의 이전 판을 찾는 키. 괄호 안의 연도·차수·개정 표기와 공백·구두점을 뺀다.

    판 표기가 아닌 괄호(예: '(창업기업용)')는 키에 남긴다.
    """
    s = _RE_판_괄호.sub("", 문서명)
    return re.sub(r"[\s_·ㆍ‧\-—「」『』'\".,()（）\[\]]", "", s)


def doc_id_생성(문서명: str, 시행일: date) -> str:
    """doc_id 는 사람이 읽는 제목이다. 인용 화면에 그대로 나오고 임베딩 헤더에도 들어간다."""
    return f"{문서명.strip()}_{시행일:%Y%m%d}"


def 파일형식(경로: Path) -> tuple[str | None, str]:
    """파일 내용으로 형식을 판별한다. 확장자는 믿지 않는다. (허용 형식 또는 None, 판별 결과 설명)

    zip·OLE 판별은 stage0_extract 가 쓰는 hwpx_extract.sniff 에 맡긴다. 배포용·암호화 HWP 는
    본문을 추출할 수 없어 None 이다. XML 은 HWPML 이면 hwp, 아니면 법령 XML 로 본다.
    """
    from hwpx_extract import sniff

    정체 = sniff(경로)
    if 정체 == "PDF":
        return "pdf", 정체
    if 정체 == "hwpx":
        return "hwpx", 정체
    if 정체 == "OLE2 (HWP v5)":
        return "hwp", 정체
    if 정체.startswith("DOCX"):
        return "docx", 정체
    with 경로.open("rb") as f:
        머리 = f.read(4096).lstrip(b"\xef\xbb\xbf \t\r\n")
    if 머리.startswith(b"<?xml"):
        return ("hwp" if b"<HWPML" in 머리 else "xml"), "XML"
    return None, 정체


def 사업명_확인(cur, 사업명: str) -> str:
    """사업명이나 별칭을 corpus.programs 의 표준 사업명으로 바꾼다. 없으면 입력오류."""
    row = cur.execute(
        "SELECT 사업명 FROM corpus.programs WHERE 사업명 = %s OR %s = ANY(별칭) ORDER BY 사업명 LIMIT 1",
        (사업명, 사업명)).fetchone()
    if not row:
        raise 입력오류(f"사업명 '{사업명}' 이 corpus.programs 에 없습니다.")
    return row[0]


def 접수(conn, 메타: dict, *, 원본파일명: str, 형식: str, 저장경로: Path) -> str:
    """corpus.ingest_jobs 에 상태 parsing 행을 만들고 doc_id 를 돌려준다.

    메타: layer, 발행기관, 문서명, 사업명(L2 만), 시행일(date), version, 적용대상.
    같은 doc_id 의 문서나 업로드가 이미 있으면 요청거부. documents 행은 파싱이 성공한 뒤에 생긴다.
    """
    시행일: date = 메타["시행일"]
    doc_id = doc_id_생성(메타["문서명"], 시행일)
    version = 메타.get("version") or f"{시행일.year}.{시행일.month}.{시행일.day}"
    with conn.transaction():
        cur = conn.cursor()
        사업명 = 사업명_확인(cur, 메타["사업명"]) if 메타["layer"] == "L2" else None
        if cur.execute("SELECT 1 FROM corpus.documents WHERE doc_id = %s", (doc_id,)).fetchone():
            raise 요청거부(f"같은 doc_id 의 문서가 이미 있습니다: {doc_id}")
        row = cur.execute(
            """INSERT INTO corpus.ingest_jobs
                   (doc_id, layer, 발행기관, 문서명, 문서키, 사업명, 시행일, version, 적용대상,
                    원본파일명, 파일형식, 저장경로)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (doc_id) DO NOTHING
               RETURNING doc_id""",
            (doc_id, 메타["layer"], 메타["발행기관"], 메타["문서명"], 문서키(메타["문서명"]), 사업명,
             시행일, version, 메타["적용대상"], 원본파일명, 형식, str(저장경로))).fetchone()
        if not row:
            raise 요청거부(f"같은 doc_id 의 업로드가 이미 있습니다: {doc_id}. 먼저 삭제하세요.")
    return doc_id


# 백그라운드 적재

def 적재_실행(dsn: str, doc_id: str) -> None:
    """업로드 한 건을 파싱해 staged 로 저장한다. 예외를 밖으로 던지지 않는다.

    어떤 실패든 ingest_jobs.상태 를 failed 로 바꾸고 사유를 남긴다. 저장은 한 트랜잭션이라
    중간에 실패하면 documents·chunks 에 아무것도 남지 않는다. 업로드 원본은 끝나면 지운다.
    """
    with _적재_잠금:
        작업 = None
        try:
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                작업 = _작업_읽기(conn, doc_id)
            if 작업 is None or 작업["상태"] != "parsing":
                return                    # 파싱 중에 삭제된 업로드
            적재본 = _준비(작업)
            with psycopg.connect(dsn, connect_timeout=10) as conn:
                저장됨 = _저장(conn, 작업, 적재본)
            if 저장됨:
                _log.info("규정 적재 staged doc_id=%s 조=%d 청크=%d",
                          doc_id, len(적재본["조문"]), len(적재본["청크"]))
            else:
                _log.info("파싱 중에 삭제된 업로드라 저장하지 않았다 doc_id=%s", doc_id)
        except _적재실패 as e:
            _log.warning("규정 적재 실패 doc_id=%s: %s", doc_id, e)
            _실패_기록(dsn, doc_id, str(e), (작업 or {}).get("저장경로"))
        except Exception as e:                                       # noqa: BLE001
            _log.exception("규정 적재 중 예외 doc_id=%s", doc_id)
            _실패_기록(dsn, doc_id, f"{type(e).__name__}: {e}", (작업 or {}).get("저장경로"))
        finally:
            if 작업 and 작업.get("저장경로"):
                Path(작업["저장경로"]).unlink(missing_ok=True)


def _작업_읽기(conn, doc_id: str) -> dict | None:
    cur = conn.cursor()
    row = cur.execute(
        """SELECT doc_id, layer, 문서명, 사업명, 시행일, version, 적용대상, 원본파일명, 파일형식,
                  저장경로, 상태
             FROM corpus.ingest_jobs WHERE doc_id = %s""", (doc_id,)).fetchone()
    if not row:
        return None
    return dict(zip([c.name for c in cur.description], row))


def _실패_기록(dsn: str, doc_id: str, 사유: str, 저장경로: str | None = None) -> None:
    """parsing 상태인 업로드만 failed 로 바꾼다. 저장경로를 알면 같은 업로드인지도 확인한다.

    이 기록마저 실패하면 로그만 남고 업로드는 parsing 으로 남는다(운영자가 삭제한다).
    """
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            conn.execute(
                """UPDATE corpus.ingest_jobs
                      SET 상태 = 'failed', 실패사유 = %s, 저장경로 = NULL, updated_at = now()
                    WHERE doc_id = %s AND 상태 = 'parsing'
                      AND (%s::text IS NULL OR 저장경로 = %s)""",
                (사유[:2000], doc_id, 저장경로, 저장경로))
    except Exception:                                                # noqa: BLE001
        _log.exception("실패 상태를 기록하지 못했다 doc_id=%s", doc_id)


def _준비(작업: dict) -> dict:
    """DB 에 쓰기 전의 계산 전부: 추출, 조 분해, 품질 판정, 적용대상 태깅, 청크, 임베딩.

    반환: {조문, 전략, 추출방식, 품질, 경고, 청크}. staged 로 둘 수 없으면 _적재실패.
    """
    from stage0_articles import is_deleted, validate

    doc_id, layer = 작업["doc_id"], 작업["layer"]
    조문, 전략, 추출방식 = _조문_추출(Path(작업["저장경로"]))
    v = validate(조문, 전략)
    if not v["ok"]:
        raise _적재실패(f"조문을 하나도 찾지 못했습니다 (분해 전략 {전략}).")

    # 품질 기준은 기존 코퍼스와 같다: validate 가 low 로 보는 문서(단락·장 단위 분해, 조 5개 미만)는 low.
    # 조번호 비단조(V1)·빈 조 과다(V3)도 low 로 둔다. 조 경계나 본문 추출을 믿을 수 없다는 신호다.
    # low 문서는 검색 필터(parse_quality='high')에 걸리지 않고, 활성화도 거부된다.
    품질 = ("low" if v["quality"] == "low"
            or any(f.startswith(("V1:", "V3:")) for f in v["flags"]) else "high")
    경고 = list(v["flags"])
    # 범위 판정은 운영자가 입력한 문서명이 아니라 표준 사업명으로 한다. 별칭으로 올려도 같은 구간이 빠진다.
    조문, 범위밖_수 = _범위밖_제거(f"{작업['사업명'] or ''} {doc_id}", 조문)
    if 범위밖_수:
        경고.append(f"사업 범위 밖 조 {범위밖_수}건 제외")
    경고 += _조번호_중복_정리(조문)
    for a in 조문:
        a["삭제"] = is_deleted(a)
    경고 += _적용대상_태깅(조문, layer=layer, 문서명=작업["문서명"], 기본값=작업["적용대상"])

    사업 = [작업["사업명"]] if 작업["사업명"] else None
    청크, 표_제외 = _청크_만들기(조문, layer=layer, 사업=사업, doc_id=doc_id)
    if 표_제외:
        경고.append(f"박스표 부속 {표_제외}건은 청크에서 제외 (조문으로는 저장)")
    if not 청크:
        raise _적재실패("검색에 넣을 청크가 없습니다 (모든 조가 삭제 조문·표이거나 적용대상을 정하지 못했습니다).")

    입력 = [c["입력"] for c in 청크]
    초과 = sum(1 for n in _토큰수(입력) if n > 임베딩_최대토큰)
    if 초과:
        경고.append(f"임베딩 입력이 {임베딩_최대토큰}토큰을 넘어 뒷부분이 잘린 청크 {초과}건")
    for c, 벡터 in zip(청크, _임베딩(입력)):
        c["embedding"] = 벡터
    return {"조문": 조문, "전략": 전략, "추출방식": 추출방식, "품질": 품질, "경고": 경고,
            "청크": 청크}


def _조문_추출(경로: Path) -> tuple[list[dict], str, str]:
    """원본 -> (조 목록, 분해 전략, documents.extraction 값). 텍스트가 거의 없으면 _적재실패.

    PDF 는 stage0_extract.extract 대신 pdftext.extract_meta 로 읽는다. 기존 L1·L2 적재와 같은 경로이고,
    글자가 두 번씩 찍힌 텍스트 레이어와 2단 조판을 풀어 준다. stage0_extract.extract_pdf 는 둘 다
    풀지 않아 조 헤더가 깨진다. 이 경로는 페이지 오프셋이 없어 페이지는 NULL 이다(기존 행과 같다).
    """
    from stage0_articles import sanitize, split_articles
    from stage0_extract import extract, 빈_추출_글자수_임계치

    try:
        if 경로.suffix.lower() == ".pdf":
            import pdftext
            본문, 메타 = pdftext.extract_meta(경로)
            종류, payload = "text", (본문, {})
            추출방식 = "dedupe" if 메타.get("dedupe") else "native"
        else:
            종류, payload = extract(경로)
            추출방식 = "native"
    except Exception as e:                                           # noqa: BLE001
        raise _적재실패(f"텍스트를 추출하지 못했습니다 ({type(e).__name__}: {e})") from e
    if 종류 == "articles":
        # 법령 XML 은 조문 구조가 이미 있다. 정렬·단조성 검사용 조번호_int 만 채운다.
        for a in payload:
            a["본문"] = sanitize(a["본문"])
            m = re.match(r"제(\d+)조", a["조번호"])
            a["조번호_int"] = int(m.group(1)) if m else None
        return payload, "xml_native", 추출방식
    본문, 오프셋 = payload
    if len(본문.strip()) < 빈_추출_글자수_임계치:
        raise _적재실패(f"추출한 텍스트가 {len(본문.strip())}자로 {빈_추출_글자수_임계치}자 미만입니다. "
                       "스캔본이나 배포용 문서는 적재할 수 없습니다.")
    조문, 전략 = split_articles(본문, 오프셋)
    return 조문, 전략, 추출방식


def _범위밖_제거(doc_id: str, 조문: list[dict]) -> tuple[list[dict], int]:
    """scope.범위밖_조 가 지정한 구간(모두의 창업 제3편 로컬트랙)은 조문으로도 저장하지 않는다."""
    from scope import 범위밖_조

    밖 = 범위밖_조(doc_id, 조문)
    return [a for a in 조문 if a["조번호"] not in 밖], len(밖)


def _조번호_중복_정리(조문: list[dict]) -> list[str]:
    """doc_articles 는 UNIQUE(doc_id, 조번호) 다. 겹치는 조번호에는 [n] 을 붙이고 경고를 돌려준다."""
    본: set[str] = set()
    바꾼: list[str] = []
    for a in 조문:
        원래 = a["조번호"]
        표기, n = 원래, 1
        while 표기 in 본:
            n += 1
            표기 = f"{원래}[{n}]"
        if 표기 != 원래:
            a["조번호"] = 표기
            바꾼.append(표기)
        본.add(표기)
    return [f"조번호 중복 {len(바꾼)}건에 [n] 표기 추가 ({', '.join(바꾼[:5])})"] if 바꾼 else []


# 적용대상 태깅
#
# 검색 필터는 적용대상 IN ('창업기업','공통') 이다. 세부관리기준·통합관리지침은 주관기관 조항과 창업기업 조항이
# 한 문서에 섞여 있어, 문서 하나에 값 하나를 붙이면 주관기관 전담인력 규정이 창업기업 판정 근거로 검색된다.
# 그래서 이런 문서는 조마다 결정적 규칙으로 정한다. 우선순위는 조제목 > 절 표제 > 본문 첫머리 주어다
# (기존 코퍼스 실측: 조제목이 주체를 정한 조에서 절 표제와 충돌 0건, 본문 주어는 절 표제와 충돌하면 대부분 절이 맞았다).
# 규칙으로 정하지 못한 조는 None 으로 두고 청크로 만들지 않는다. 틀린 값으로 색인하는 것보다 빠지는 편이 안전하다.
# 법령처럼 주체 구분이 없는 문서는 업로드 메타데이터의 적용대상(기본 '공통')을 모든 조에 쓴다.

_태깅_대상어 = ("세부관리기준", "관리기준", "통합관리지침", "운영지침")
_태깅_제외어 = ("별지서식", "모집공고", "공고문", "현황", "신청서")
RE_주관 = re.compile(r"주관기관|전담조직|전담인력|운영기관|지역허브|전문기관|총괄기관|운영사")
RE_창업 = re.compile(r"창업기업|창업자|진출자|도전자|입주기업|창업팀")
# 절 표제는 주체를 선언하므로 본문 주어보다 어휘를 넓게 잡는다. 창업중심대학은 그 사업의 주관기관이다.
RE_절_주관 = re.compile(RE_주관.pattern + r"|창업중심대학|전담기관|주관대학")
RE_절장 = re.compile(r"[<〈]?\s*제\s*(\d+)\s*(절|장)\s*[>〉]?\s*([^<>\n]{0,22})")
_본문_주어_구간 = 200       # 조 첫머리 몇 글자에서 주어를 찾는다
# 조 분해 때 다음 장·절 표제가 앞 조 본문 끝에 붙는다. 줄 첫머리에 있고 뒤에 공백·괄호·줄끝이 오는 것만
# 표제로 본다("제2장에 따른" 같은 본문 속 참조는 제외).
RE_표제_줄 = re.compile(r"(?m)^\s*[<〈]?\s*제\s*\d+\s*(?:절|장)(?=\s|[>〉]|$)")


def _태깅_대상(layer: str, 문서명: str) -> bool:
    """조별 태깅을 하는 문서인가. L2 전부와 이름에 관리기준·지침이 들어간 L1 문서. 서식·공고는 제외."""
    if any(k in 문서명 for k in _태깅_제외어):
        return False
    return layer == "L2" or any(k in 문서명 for k in _태깅_대상어)


def _절값(이름: str) -> str | None:
    """절 표제 이름에서 적용대상을 읽는다. 못 읽으면 None."""
    if "공통" in 이름:
        return "공통"
    주, 창 = bool(RE_절_주관.search(이름)), bool(RE_창업.search(이름))
    if 주 and not 창:
        return "주관기관"
    if 창 and not 주:
        return "창업기업"
    return None


def _절_상속(조문: list[dict]) -> dict[str, str | None]:
    """조번호 -> 그 조가 속한 절의 적용대상. 장이 바뀌면 상속을 끊는다.

    조 분해 결과에서 장·절 표제는 앞 조 본문 끝에 붙어 온다. 조가 '장' 필드를 가지고 있으면 그것을 쓴다.
    """
    out: dict[str, str | None] = {}
    상태: str | None = None
    for a in 조문:
        장 = (a.get("장") or "").strip()
        if 장:
            상태 = _절값(장.split("/")[-1]) if "절" in 장 else None
        out[a["조번호"]] = 상태
        if not 장:
            for m in RE_절장.finditer(a.get("본문") or ""):
                상태 = None if m.group(2) == "장" else _절값(m.group(3))
    return out


def _조_주체(조제목: str | None, 본문: str) -> tuple[str | None, str]:
    """(적용대상, 근거). 조제목 주어를 먼저 보고, 없으면 본문 첫머리 주어를 본다. 못 정하면 (None, '미정').

    짧은 조는 첫머리 구간에 다음 장·절 표제가 들어와 주어로 잡히므로, 표제 앞까지만 본다.
    """
    m = RE_표제_줄.search(본문)
    if m and m.start() > 0:
        본문 = 본문[:m.start()]
    for 글, 근거 in ((조제목 or "", "조제목"), (본문[:_본문_주어_구간], "본문주어")):
        주, 창 = bool(RE_주관.search(글)), bool(RE_창업.search(글))
        if 주 and not 창:
            return "주관기관", 근거
        if 창 and not 주:
            return "창업기업", 근거
        if 주 and 창:
            return None, "미정"         # 두 주체가 함께 나오면 규칙으로 가르지 않는다
    return None, "미정"


def _적용대상_태깅(조문: list[dict], *, layer: str, 문서명: str, 기본값: str) -> list[str]:
    """조마다 a['적용대상'] 을 채우고 경고 문장을 돌려준다. None 인 조는 청크로 만들지 않는다.

    부칙·별표·붙임(조번호_int 가 없는 조)은 시행일·경과조치처럼 누구에게나 적용되므로 '공통' 이다.
    """
    if not _태깅_대상(layer, 문서명):
        for a in 조문:
            a["적용대상"] = 기본값
        return []
    절 = _절_상속(조문)
    for a in 조문:
        if a.get("조번호_int") is None:
            a["적용대상"] = "공통"
            continue
        값, 근거 = _조_주체(a.get("조제목"), a.get("본문") or "")
        if 근거 != "조제목" and 절.get(a["조번호"]) is not None:
            값 = 절[a["조번호"]]
        a["적용대상"] = 값
    분포 = Counter(a["적용대상"] or "미정" for a in 조문 if not a["삭제"])
    경고 = ["적용대상 조별 태깅: " + ", ".join(f"{k} {분포[k]}" for k in ("창업기업", "주관기관", "공통", "미정")
                                         if 분포[k])]
    if 분포["미정"]:
        경고.append(f"적용대상을 정하지 못한 조 {분포['미정']}건은 검색에서 제외 (조문으로는 저장)")
    return 경고


# 청크 분할
#
# 청크 형식은 기존 corpus.chunks 행과 같아야 한다(삭제된 stage2_chunk·stage2_embed, stage2_bm25.보충 과 같은
# 형식). text 는 원문 그대로(인용 검증 대상)이고, 임베딩·BM25 입력은 stage2_bm25._헤더 가 만드는
# 컨텍스트 헤더 + 줄바꿈 + text 다.
# 900토큰을 넘는 조는 항 -> 호 -> 목 -> 줄 단위로 나눈 뒤 예산 안에서 연속한 조각을 다시 붙인다.
# 조 경계는 넘지 않는다. 부속(별표·붙임 등) 가운데 박스표는 청크로 만들지 않고, 산문 부속은
# 검색 진입점이 아니라 참조 확장으로만 닿도록 retrieval_scope='폐포전용' 으로 둔다.

RE_첨부 = re.compile(r"^\s*(별표|별지|붙임|서식|첨부|별첨|부록|참고)")
RE_박스표 = re.compile(r"[┌│└├┬┼┐┘┴┤─]")
박스표_임계 = 10              # 산문에도 장식용 괘선이 한두 개 섞인다
_항기호 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
RE_항 = re.compile(f"(?=[{_항기호}])")
RE_호 = re.compile(r"(?=\n\s*\d+\.\s)")
RE_목 = re.compile(r"(?=\n\s*[가-힣]\.\s)")


def _청크_만들기(조문: list[dict], *, layer: str, 사업: list[str] | None,
             doc_id: str) -> tuple[list[dict], int]:
    """조 목록 -> (청크 목록, 박스표라 제외한 부속 수). 청크: 조(조문 인덱스), 항호, text, retrieval_scope, 입력.

    삭제 조, 빈 조, 적용대상을 정하지 못한 조는 청크로 만들지 않는다.
    """
    from stage2_bm25 import _장맵, _헤더

    장 = _장맵(조문)
    청크: list[dict] = []
    표_제외 = 0
    for i, a in enumerate(조문):
        if a["삭제"] or not (a["본문"] or "").strip() or a.get("적용대상") is None:
            continue
        부속 = bool(RE_첨부.match(a["조번호"] or ""))
        if 부속 and len(RE_박스표.findall(a["본문"])) >= 박스표_임계:
            표_제외 += 1
            continue
        머리 = _헤더(layer, 사업, doc_id, 장.get(a["조번호"], ""), a["조번호"], a.get("조제목"))
        for 항호, txt in _병합(_분할(a["본문"])):
            청크.append({"조": i, "항호": 항호, "text": txt,
                         "retrieval_scope": "폐포전용" if 부속 else "진입점",
                         "입력": f"{머리}\n{txt}"})
    return 청크, 표_제외


def _분할(본문: str) -> list[tuple[str | None, str]]:
    """조 본문 -> [(항호 라벨, 텍스트)]. 900토큰 이하면 조 전체가 한 조각이다."""
    본문 = 본문.strip()
    if not 본문:
        return []
    if _토큰수([본문])[0] <= 청크_최대토큰:
        return [(None, 본문)]

    def 쪼개기(pat, s: str) -> list[str]:
        parts = [p.strip() for p in pat.split(s) if p and p.strip()]
        return parts if len(parts) > 1 else []

    잎: list[tuple[str | None, str]] = []
    항들 = 쪼개기(RE_항, 본문) or [본문]
    for i, (항, n) in enumerate(zip(항들, _토큰수(항들))):
        라벨 = 항[0] if 항[:1] in _항기호 else (f"항{i+1}" if len(항들) > 1 else None)
        if n <= 청크_최대토큰:
            잎.append((라벨, 항))
            continue
        조각들 = 쪼개기(RE_호, 항) or 쪼개기(RE_목, 항) or [항]
        for j, (조각, m) in enumerate(zip(조각들, _토큰수(조각들))):
            꼬리 = f"{라벨 or ''}-{j+1}" if len(조각들) > 1 else 라벨
            if m <= 청크_최대토큰:
                잎.append((꼬리, 조각))
            else:
                for k, 토막 in enumerate(_강제분할(조각)):
                    잎.append((f"{꼬리 or ''}#{k+1}", 토막))
    return _재포장(잎) or [(None, 본문)]


def _재포장(잎: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    """연속한 조각을 토큰 예산까지 다시 붙인다. 항 단위로만 자르면 200자짜리 청크가 수십 개 생긴다.

    붙인 조각의 라벨은 '①~③' 처럼 범위로 적는다.
    """
    if len(잎) <= 1:
        return 잎
    묶음: list[list] = []                        # [[라벨들], [텍스트들], 토큰합]
    for (라벨, txt), n in zip(잎, _토큰수([t for _, t in 잎])):
        if 묶음 and 묶음[-1][2] + n <= 청크_최대토큰:
            묶음[-1][0].append(라벨)
            묶음[-1][1].append(txt)
            묶음[-1][2] += n
        else:
            묶음.append([[라벨], [txt], n])
    out = []
    for 라벨들, 텍스트들, _ in 묶음:
        유효 = [l for l in 라벨들 if l]
        라벨 = None if not 유효 else (유효[0] if len(유효) == 1 else f"{유효[0]}~{유효[-1]}")
        out.append((라벨, "\n".join(텍스트들)))
    return out


def _강제분할(s: str) -> list[str]:
    """구조가 없는 덩어리(표 잔재·긴 나열)를 줄 단위로 토큰 예산 이하로 자른다. 한 줄이 넘치면 글자 수로 자른다."""
    줄들 = s.split("\n")
    out, buf, n = [], [], 0
    for 줄, ln in zip(줄들, _토큰수(줄들)):
        if ln > 청크_최대토큰:
            if buf:
                out.append("\n".join(buf))
                buf, n = [], 0
            폭 = max(200, len(줄) * 청크_최대토큰 // max(ln, 1) - 100)
            out += [줄[i:i + 폭] for i in range(0, len(줄), 폭)]
            continue
        if n + ln > 청크_최대토큰 and buf:
            out.append("\n".join(buf))
            buf, n = [], 0
        buf.append(줄)
        n += ln
    if buf:
        out.append("\n".join(buf))
    return [x for x in (t.strip() for t in out) if x]


def _병합(조각들: list[tuple[str | None, str]]) -> list[tuple[str | None, str]]:
    """50자 미만 조각은 앞 조각에 붙인다. 호출 단위가 조 하나라 조 경계는 넘지 않는다."""
    out: list[list] = []
    for 라벨, txt in 조각들:
        if out and len(txt) < 청크_최소글자:
            out[-1][1] = out[-1][1] + "\n" + txt
        else:
            out.append([라벨, txt])
    return [(a, b) for a, b in out]


# 임베딩·BM25

def _토큰수(texts: list[str]) -> list[int]:
    """KURE-v1 토크나이저 기준 토큰 수 (특수 토큰 포함, 자르지 않음).

    판정 경로와 공유하는 모델의 토크나이저는 쓰지 않는다. truncation 설정을 바꾸면 동시에 도는
    쿼리 임베딩과 충돌하므로, 같은 파일에서 별도 인스턴스를 한 번 올린다.
    """
    global _토크나이저
    if not texts:
        return []
    if _토크나이저 is None:
        import retrieve
        from transformers import AutoTokenizer
        _토크나이저 = AutoTokenizer.from_pretrained(retrieve.모델().tokenizer.name_or_path)
    enc = _토크나이저(texts, add_special_tokens=True, truncation=False, padding=False,
                     return_attention_mask=False)
    return [len(x) for x in enc["input_ids"]]


def _임베딩(입력: list[str]) -> list[str]:
    """retrieve 가 상주시킨 KURE-v1 으로 임베딩한다. retrieve.임베딩 과 같은 정규화·리터럴 형식이다."""
    import retrieve

    벡터들 = retrieve.모델().encode(입력, batch_size=16, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False)
    if 벡터들.shape != (len(입력), 1024):
        raise _적재실패(f"임베딩 차원이 맞지 않습니다 {벡터들.shape}")
    return ["[" + ",".join(f"{x:.6f}" for x in v) + "]" for v in 벡터들]


def _bm25_색인(cur, doc_id: str) -> int:
    """문서의 청크를 BM25 색인(chunk_len·chunk_terms)에 넣는다. 넣은 청크 수를 돌려준다.

    활성화 트랜잭션에서 부른다. retrieve 의 BM25 는 chunk_len 전체로 문서 수와 평균 길이를 계산하므로,
    staged 단계에서 넣으면 활성화 전부터 active 청크의 점수가 바뀐다.
    입력은 stage2_bm25.보충 과 같다: 컨텍스트 헤더 + 줄바꿈 + text. 장 제목은 문서의 조 전체를 순서대로 보고
    정하므로 적재 때 임베딩 입력에 쓴 헤더와 같다. 이미 색인된 청크(보충 이 먼저 넣은 경우)는 건너뛴다.
    """
    from stage2_bm25 import _장맵, _헤더, 토큰화

    장 = _장맵([{"조번호": r[0], "본문": r[1]} for r in cur.execute(
        "SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id = %s ORDER BY article_id",
        (doc_id,)).fetchall()])
    행 = cur.execute(
        """SELECT c.chunk_id, c.layer, c.사업명, c.조번호, c.조제목, c.text
             FROM corpus.chunks c
            WHERE c.doc_id = %s
              AND NOT EXISTS (SELECT 1 FROM corpus.chunk_len l WHERE l.chunk_id = c.chunk_id)
            ORDER BY c.chunk_id""", (doc_id,)).fetchall()
    if not 행:
        return 0
    토큰들 = 토큰화([f"{_헤더(layer, 사업, doc_id, 장.get(조번호, ''), 조번호, 조제목)}\n{text}"
                   for _, layer, 사업, 조번호, 조제목, text in 행])
    with cur.copy("COPY corpus.chunk_len (chunk_id, dl) FROM STDIN") as cp:
        for r, 토큰 in zip(행, 토큰들):
            cp.write_row((r[0], len(토큰)))
    with cur.copy("COPY corpus.chunk_terms (chunk_id, term, tf) FROM STDIN") as cp:
        for r, 토큰 in zip(행, 토큰들):
            for term, tf in Counter(토큰).items():
                cp.write_row((r[0], term, tf))
    return len(행)


# 저장

def _저장(conn, 작업: dict, 적재본: dict) -> bool:
    """적재본을 한 트랜잭션으로 쓴다. documents·chunks 는 staged, index_target 은 FALSE 다.

    파싱 중에 업로드가 삭제됐으면 아무것도 쓰지 않고 False 를 돌려준다. 삭제 뒤 같은 doc_id 로
    다시 올린 업로드와 섞이지 않도록 저장경로까지 같은지 확인한다.
    """
    doc_id, layer = 작업["doc_id"], 작업["layer"]
    사업 = [작업["사업명"]] if 작업["사업명"] else None
    with conn.transaction():
        cur = conn.cursor()
        현재 = cur.execute("SELECT 상태, 저장경로 FROM corpus.ingest_jobs WHERE doc_id = %s FOR UPDATE",
                          (doc_id,)).fetchone()
        if not 현재 or 현재[0] != "parsing" or 현재[1] != 작업["저장경로"]:
            return False
        cur.execute(
            """INSERT INTO corpus.documents
                   (doc_id, layer, domain, version, 시행일, status, parse_quality, extraction,
                    src_path, index_target, retrieval_scope)
               VALUES (%s,%s,%s,%s,%s,'staged',%s,%s,%s,FALSE,'진입점')""",
            (doc_id, layer, DOMAIN, 작업["version"], 작업["시행일"], 적재본["품질"],
             적재본["추출방식"], f"admin_upload/{작업['원본파일명']}"))

        조_id: list[int] = []
        for a in 적재본["조문"]:
            조_id.append(cur.execute(
                """INSERT INTO corpus.doc_articles (doc_id, 조번호, 조제목, 조번호_int, 본문, 페이지, 삭제)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING article_id""",
                (doc_id, a["조번호"], a.get("조제목"), a.get("조번호_int"), a["본문"],
                 a.get("페이지"), a["삭제"])).fetchone()[0])

        for c in 적재본["청크"]:
            a = 적재본["조문"][c["조"]]
            cur.execute(
                """INSERT INTO corpus.chunks
                       (doc_id, article_id, layer, parse_quality, version, status, retrieval_scope,
                        조번호, 조제목, 항호, 페이지, 사업명, 적용대상, text, embedding)
                   VALUES (%s,%s,%s,%s,%s,'staged',%s,%s,%s,%s,%s,%s,%s,%s,
                           %s::extensions.vector(1024))""",
                (doc_id, 조_id[c["조"]], layer, 적재본["품질"], 작업["version"],
                 c["retrieval_scope"], a["조번호"], a.get("조제목"), c["항호"], a.get("페이지"),
                 사업, a["적용대상"], c["text"], c["embedding"]))
        # BM25 색인 행(chunk_len·chunk_terms)은 활성화 때 넣는다(_bm25_색인).

        cur.executemany(
            """INSERT INTO corpus.refs
                   (src_doc_id, src_조번호, src_layer, 참조문자열, 관계, dst_doc_id, dst_조번호,
                    해소상태, 보정근거)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (src_doc_id, src_조번호, 참조문자열) DO NOTHING""",
            _참조_만들기(cur, doc_id, 작업["문서명"], layer, 적재본["조문"]))

        cur.execute(
            """UPDATE corpus.ingest_jobs
                  SET 상태 = 'done', 분해전략 = %s, 조_건수 = %s, 경고 = %s, 저장경로 = NULL,
                      updated_at = now()
                WHERE doc_id = %s""",
            (적재본["전략"], len(적재본["조문"]), Jsonb(적재본["경고"]), doc_id))
    return True


def _참조_만들기(cur, doc_id: str, 문서명: str, layer: str, 조문: list[dict]) -> list[tuple]:
    """조별 참조 표기 -> corpus.refs 행. 문형 인식·해소는 build_refs 를 그대로 쓴다.

    대상 문서 이름표는 build_refs.load_corpus_names 와 같은 규칙(L1_ 접두·_YYYYMMDD 꼬리를 뗀 제명과
    doc_id 둘 다 등록)으로 만들되, active 문서와 이 문서만 넣는다. load_corpus_names 는 status 를
    보지 않아 staged·superseded 문서까지 대상이 되므로 쓰지 않는다.
    """
    import build_refs

    이름표: dict[str, str] = {}
    for (d,) in cur.execute("SELECT doc_id FROM corpus.documents "
                            "WHERE status = 'active' AND layer IN ('L1','L2') ORDER BY doc_id"):
        몸통 = re.sub(r"_\d{8}$", "", d[3:] if d.startswith("L1_") else d)
        이름표.setdefault(build_refs.norm_규범(몸통), d)
        이름표.setdefault(build_refs.norm_규범(d), d)
    이름표[build_refs.norm_규범(문서명)] = doc_id
    이름표[build_refs.norm_규범(doc_id)] = doc_id

    약칭표 = build_refs.수집_약칭(" ".join(a["본문"] or "" for a in 조문))
    동적 = build_refs._동적패턴(약칭표)
    행 = []
    for a in 조문:
        if a["삭제"]:
            continue                      # 효력이 없는 조는 참조의 출발점이 아니다
        for e in build_refs.scan(a["본문"], doc_id, 이름표, src_조번호=a["조번호"],
                                 약칭표=약칭표, 동적=동적):
            행.append((e["src_doc_id"], e["src_조번호"], layer, e["참조문자열"], e["관계"],
                       e["dst_doc_id"], e["dst_조번호"], e["해소상태"], e["보정근거"]))
    return 행


# 활성화·삭제

def 활성화(conn, doc_id: str, *, 대체_doc_id: str | None = None, 신규문서: bool = False,
         중복_허용: bool = False) -> dict:
    """staged 문서를 active 로 바꾸고 이전 판을 superseded 로 바꾼다. 한 트랜잭션이다.

    거부(요청거부): 파싱 중·실패, staged 가 아님, parse_quality 가 low, 조·청크가 없음, 임베딩 누락,
    이전 판을 정할 수 없음, 신규문서인데 비슷한 active 문서가 있음(중복_허용 이 아니면).
    이전 판은 chunks 를 지우고 doc_articles 는 남긴다(기존 superseded 문서와 같은 모양).
    retrieval_scope 는 이전 판의 값을 이어받는다. BM25 색인 행을 넣고 term_df 를 갱신한다.
    tenant.judge_cache 는 전부 지운다.
    """
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("SET LOCAL lock_timeout = '10s'")
        작업 = cur.execute(
            """SELECT layer, 문서키, 사업명, 상태 FROM corpus.ingest_jobs
                WHERE doc_id = %s FOR UPDATE""", (doc_id,)).fetchone()
        문서 = cur.execute(
            "SELECT status, parse_quality FROM corpus.documents WHERE doc_id = %s FOR UPDATE",
            (doc_id,)).fetchone()
        if 작업 is None and 문서 is None:
            raise 문서없음(f"문서를 찾을 수 없습니다: {doc_id}")
        if 작업 is None:
            raise 요청거부("규정 적재 API 로 올린 문서만 활성화할 수 있습니다.")
        layer, 키, 사업명, 작업상태 = 작업
        if 작업상태 == "parsing":
            raise 요청거부("아직 파싱 중입니다.")
        if 작업상태 == "failed" or 문서 is None:
            raise 요청거부("파싱에 실패한 문서는 활성화할 수 없습니다.")
        if 문서[0] != "staged":
            raise 요청거부(f"staged 문서만 활성화할 수 있습니다 (현재 {문서[0]}).")
        if 문서[1] != "high":
            raise 요청거부("parse_quality 가 low 인 문서는 활성화할 수 없습니다. 원본을 고쳐 다시 올리세요.")
        조수, 청크수, 임베딩없음 = cur.execute(
            """SELECT (SELECT count(*) FROM corpus.doc_articles WHERE doc_id = %(d)s AND NOT 삭제),
                      (SELECT count(*) FROM corpus.chunks WHERE doc_id = %(d)s),
                      (SELECT count(*) FROM corpus.chunks WHERE doc_id = %(d)s AND embedding IS NULL)""",
            {"d": doc_id}).fetchone()
        if not 조수 or not 청크수:
            raise 요청거부("조문이나 청크가 없는 문서는 활성화할 수 없습니다.")
        if 임베딩없음:
            raise 요청거부(f"임베딩이 없는 청크가 {임베딩없음}건 있습니다.")

        이전 = _이전판_결정(cur, doc_id, layer, 키, 사업명, 대체_doc_id, 신규문서)
        경고: list[str] = []
        if 이전 is None:
            # 신규문서로 올리면 비슷한 active 문서와 함께 검색된다. 그 문서가 사실 이전 판이면 옛 판이
            # 새 판이 금지한 것을 허용하는 채로 남으므로, 운영자가 중복_허용 으로 확인해야 진행한다.
            함께 = _이전판_후보(cur, layer, 키, 사업명)
            if 함께 and not 중복_허용:
                raise 요청거부(
                    f"같은 {'사업' if 사업명 else '이름'}의 active 문서가 있습니다. 이전 판이면 대체_doc_id 로 "
                    "지정하고, 함께 검색돼야 하는 별개 문서면 중복_허용=true 를 지정하세요.", 후보=함께)
            if 함께:
                경고.append(f"같은 {'사업' if 사업명 else '이름'}의 active 문서가 함께 검색됩니다: {', '.join(함께)}")
        범위 = 이전[1] if 이전 else "진입점"
        cur.execute("UPDATE corpus.documents SET status = 'active', index_target = TRUE, "
                    "retrieval_scope = %s WHERE doc_id = %s", (범위, doc_id))
        # 부속 청크는 항상 폐포전용이다. 문서가 폐포전용이면 나머지 청크도 폐포전용이 된다.
        cur.execute(
            """UPDATE corpus.chunks
                  SET status = 'active',
                      retrieval_scope = CASE WHEN %s = '폐포전용' THEN '폐포전용' ELSE retrieval_scope END
                WHERE doc_id = %s""", (범위, doc_id))

        지운_청크 = 0
        if 이전:
            cur.execute("UPDATE corpus.documents SET status = 'superseded', index_target = FALSE "
                        "WHERE doc_id = %s", (이전[0],))
            try:
                지운_청크 = cur.execute("DELETE FROM corpus.chunks WHERE doc_id = %s", (이전[0],)).rowcount
            except psycopg.errors.IntegrityError as e:
                # 로컬 DB 의 eval.golden_chunks 가 청크를 참조하면 FK(SET NULL)가 CHECK 에 걸린다.
                # 운영 DB 에는 eval 스키마가 없다. 트랜잭션 전체가 롤백되므로 아무것도 바뀌지 않는다.
                raise 요청거부(f"이전 판 청크를 {e.diag.schema_name}.{e.diag.table_name} 가 참조하고 있어 "
                           "지울 수 없습니다. eval.golden_chunks 라면 정답셋 청크 재매칭을 먼저 하세요.") from e
            경고 += _활성화_경고(cur, 이전[0])
        cur.execute("UPDATE corpus.ingest_jobs SET 대체한_doc_id = %s, updated_at = now() "
                    "WHERE doc_id = %s", (이전[0] if 이전 else None, doc_id))
        _bm25_색인(cur, doc_id)
        # 판정 캐시 키는 코퍼스 상태를 담지 않는다. 행 수가 바뀌지 않는 상태 전환도 있어
        # 설정 해시만으로는 무효화가 보장되지 않으므로 여기서 지운다(RLS 정책 ingest_cache_purge).
        캐시_삭제 = cur.execute("DELETE FROM tenant.judge_cache").rowcount
        # CONCURRENTLY 는 읽기를 막지 않는 락(EXCLUSIVE)만 잡아 검색 쿼리가 기다리지 않는다.
        # term_df(term) 고유 인덱스가 필요하다(db/migrations/2026-11-01_corpus_ingest.sql).
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY corpus.term_df")
    return {"doc_id": doc_id, "상태": "active", "대체한_doc_id": 이전[0] if 이전 else None,
            "retrieval_scope": 범위, "이전판_청크_삭제": 지운_청크, "판정캐시_삭제": 캐시_삭제,
            "경고": 경고}


def _이전판_결정(cur, doc_id: str, layer: str, 키: str, 사업명: str | None,
             대체_doc_id: str | None, 신규문서: bool) -> tuple[str, str] | None:
    """활성화로 superseded 가 될 이전 판 (doc_id, retrieval_scope). 없으면 None.

    자동 매칭은 이 API 로 올린 active 문서 가운데 layer·문서키·사업명이 같은 것만 본다.
    자동 매칭이 없으면 호출자가 대체_doc_id 나 신규문서=True 로 직접 정해야 한다. 잘못 고르면
    엉뚱한 문서가 검색에서 빠지거나 옛 판이 함께 남으므로 추측하지 않는다.
    """
    자동 = cur.execute(
        """SELECT d.doc_id, d.retrieval_scope
             FROM corpus.ingest_jobs j JOIN corpus.documents d USING (doc_id)
            WHERE d.status = 'active' AND j.layer = %s AND j.문서키 = %s
              AND j.사업명 IS NOT DISTINCT FROM %s AND j.doc_id <> %s
            ORDER BY d.doc_id FOR UPDATE OF d""", (layer, 키, 사업명, doc_id)).fetchall()
    if 대체_doc_id:
        if 신규문서:
            raise 요청거부("대체_doc_id 와 신규문서를 함께 지정할 수 없습니다.")
        row = cur.execute(
            "SELECT doc_id, retrieval_scope, layer, status FROM corpus.documents "
            "WHERE doc_id = %s FOR UPDATE", (대체_doc_id,)).fetchone()
        if not row or row[3] != "active":
            raise 요청거부(f"대체_doc_id 는 active 문서여야 합니다: {대체_doc_id}")
        if row[2] != layer:
            raise 요청거부(f"대체할 문서의 layer({row[2]})가 새 문서({layer})와 다릅니다.")
        if 자동 and row[0] not in {r[0] for r in 자동}:
            raise 요청거부("같은 문서의 active 판이 따로 있습니다.", 후보=[r[0] for r in 자동])
        if 사업명 and not cur.execute(
                """SELECT EXISTS (SELECT 1 FROM corpus.ingest_jobs WHERE doc_id = %(d)s AND 사업명 = %(s)s)
                       OR EXISTS (SELECT 1 FROM corpus.chunks WHERE doc_id = %(d)s AND %(s)s = ANY(사업명))""",
                {"d": row[0], "s": 사업명}).fetchone()[0]:
            raise 요청거부(f"대체할 문서가 사업 '{사업명}' 의 문서가 아닙니다.")
        return row[0], row[1]
    if 자동:
        if 신규문서:
            raise 요청거부("같은 문서의 active 판이 있어 신규문서로 활성화할 수 없습니다.",
                       후보=[r[0] for r in 자동])
        if len(자동) > 1:
            raise 요청거부("같은 문서의 active 판이 여럿입니다. 대체_doc_id 로 지정하세요.",
                       후보=[r[0] for r in 자동])
        return 자동[0][0], 자동[0][1]
    if 신규문서:
        return None
    raise 요청거부("이전 판을 자동으로 찾지 못했습니다. 대체할 문서가 있으면 대체_doc_id 를, "
               "처음 올리는 문서면 신규문서=true 를 지정하세요.",
               후보=_이전판_후보(cur, layer, 키, 사업명))


def _이전판_후보(cur, layer: str, 키: str, 사업명: str | None) -> list[str]:
    """이전 판일 수 있는 active 문서 (안내용). L2 는 같은 사업, L1 은 문서키가 겹치는 문서."""
    if 사업명:
        rows = cur.execute(
            """SELECT DISTINCT d.doc_id FROM corpus.documents d JOIN corpus.chunks c USING (doc_id)
                WHERE d.status = 'active' AND d.layer = %s AND %s = ANY(c.사업명)
                ORDER BY 1 LIMIT 10""", (layer, 사업명)).fetchall()
        return [r[0] for r in rows]
    rows = cur.execute("SELECT doc_id FROM corpus.documents WHERE status = 'active' AND layer = %s "
                       "ORDER BY doc_id", (layer,)).fetchall()
    후보 = []
    for (d,) in rows:
        k = 문서키(re.sub(r"^L\d_|_\d{8}$", "", d))
        if k and (k in 키 or 키 in k):
            후보.append(d)
    return 후보[:10]


def _활성화_경고(cur, 이전_doc_id: str) -> list[str]:
    """이전 판을 가리키는 룰 근거와 다른 문서의 참조 수. 활성화가 자동으로 고치지 않는 부분이다."""
    룰수, 참조수 = cur.execute(
        """SELECT (SELECT count(*) FROM corpus.rules
                    WHERE 근거 @> jsonb_build_array(jsonb_build_object('doc_id', %(d)s::text))),
                  (SELECT count(*) FROM corpus.refs WHERE dst_doc_id = %(d)s AND src_doc_id <> %(d)s)""",
        {"d": 이전_doc_id}).fetchone()
    경고 = []
    if 룰수:
        경고.append(f"이전 판을 근거로 가진 corpus.rules {룰수}건은 재검수가 필요합니다.")
    if 참조수:
        경고.append(f"다른 문서의 참조 {참조수}건이 이전 판을 가리킵니다 (참조를 다시 만들기 전까지 그대로 남습니다).")
    return 경고


def 삭제(conn, doc_id: str) -> dict:
    """staged·failed·parsing 업로드와 그 조문·청크·색인·참조를 지운다. active·superseded 는 거부한다."""
    with conn.transaction():
        cur = conn.cursor()
        작업 = cur.execute("SELECT 상태, 저장경로 FROM corpus.ingest_jobs WHERE doc_id = %s FOR UPDATE",
                          (doc_id,)).fetchone()
        문서 = cur.execute("SELECT status FROM corpus.documents WHERE doc_id = %s FOR UPDATE",
                          (doc_id,)).fetchone()
        if 작업 is None and 문서 is None:
            raise 문서없음(f"문서를 찾을 수 없습니다: {doc_id}")
        if 문서 and 문서[0] != "staged":
            raise 요청거부(f"{문서[0]} 문서는 삭제할 수 없습니다 (staged·failed·parsing 만).")
        이전상태 = 문서[0] if 문서 else 작업[0]
        청크수 = cur.execute("SELECT count(*) FROM corpus.chunks WHERE doc_id = %s",
                            (doc_id,)).fetchone()[0]
        # refs 에는 documents 로 가는 FK 가 없어 CASCADE 로 지워지지 않는다
        참조수 = cur.execute("DELETE FROM corpus.refs WHERE src_doc_id = %s", (doc_id,)).rowcount
        cur.execute("DELETE FROM corpus.documents WHERE doc_id = %s AND status = 'staged'", (doc_id,))
        cur.execute("DELETE FROM corpus.ingest_jobs WHERE doc_id = %s", (doc_id,))
    if 작업 and 작업[1]:
        Path(작업[1]).unlink(missing_ok=True)       # 서버 재시작으로 parsing 에 멈춘 업로드의 원본
    return {"doc_id": doc_id, "이전상태": 이전상태, "청크_삭제": 청크수, "참조_삭제": 참조수}


# 조회

def 문서_상태(conn, doc_id: str) -> dict:
    """업로드·문서 한 건의 상태, 품질, 개수, 경고, 조문 샘플. 이 API 로 올리지 않은 문서는 메타데이터가 비어 있다."""
    cur = conn.cursor()
    row = cur.execute(
        """SELECT j.doc_id AS 작업, j.상태 AS 작업상태, j.발행기관, j.문서명, j.사업명, j.적용대상,
                  j.원본파일명, j.파일형식, j.분해전략, j.경고, j.실패사유, j.대체한_doc_id,
                  j.created_at AS 등록일시, j.updated_at AS 갱신일시,
                  coalesce(j.layer, d.layer) AS layer, coalesce(j.시행일, d.시행일) AS 시행일,
                  coalesce(j.version, d.version) AS version,
                  d.status, d.parse_quality, d.retrieval_scope
             FROM (SELECT %s::text AS doc_id) k
             LEFT JOIN corpus.ingest_jobs j USING (doc_id)
             LEFT JOIN corpus.documents d USING (doc_id)""", (doc_id,)).fetchone()
    r = dict(zip([c.name for c in cur.description], row))
    if r["작업"] is None and r["status"] is None:
        raise 문서없음(f"문서를 찾을 수 없습니다: {doc_id}")
    조수, 삭제조수, 청크수, 임베딩없음, 참조수, 끊긴참조 = cur.execute(
        """SELECT (SELECT count(*) FROM corpus.doc_articles WHERE doc_id = %(d)s),
                  (SELECT count(*) FROM corpus.doc_articles WHERE doc_id = %(d)s AND 삭제),
                  (SELECT count(*) FROM corpus.chunks WHERE doc_id = %(d)s),
                  (SELECT count(*) FROM corpus.chunks WHERE doc_id = %(d)s AND embedding IS NULL),
                  (SELECT count(*) FROM corpus.refs WHERE src_doc_id = %(d)s),
                  (SELECT count(*) FROM corpus.refs WHERE src_doc_id = %(d)s
                      AND 해소상태 = 'dangling' AND 관계 <> '미규정위임')""",
        {"d": doc_id}).fetchone()
    샘플 = [{"조번호": 조, "조제목": 제목, "본문": 본문[:300]} for 조, 제목, 본문 in cur.execute(
        """SELECT 조번호, 조제목, 본문 FROM corpus.doc_articles
            WHERE doc_id = %s AND NOT 삭제 ORDER BY article_id LIMIT 3""", (doc_id,))]
    return {
        "doc_id": doc_id, "상태": _상태(r["작업상태"], r["status"]),
        "layer": r["layer"], "문서명": r["문서명"], "발행기관": r["발행기관"], "사업명": r["사업명"],
        "시행일": r["시행일"], "version": r["version"], "적용대상": r["적용대상"],
        "원본파일명": r["원본파일명"], "파일형식": r["파일형식"],
        "parse_quality": r["parse_quality"], "retrieval_scope": r["retrieval_scope"],
        "분해전략": r["분해전략"], "조_건수": 조수, "삭제조_건수": 삭제조수, "청크_건수": 청크수,
        "임베딩_누락": 임베딩없음, "참조_건수": 참조수, "끊긴참조_건수": 끊긴참조,
        "경고": r["경고"] or [], "실패사유": r["실패사유"], "대체한_doc_id": r["대체한_doc_id"],
        "샘플": 샘플, "등록일시": r["등록일시"], "갱신일시": r["갱신일시"],
    }


def 문서_목록(conn, 상태: str | None = None, limit: int = 100) -> list[dict]:
    """업로드와 L1·L2 문서 목록 (최근 등록순). 상태: parsing|staged|active|failed|superseded."""
    rows = conn.execute(
        """SELECT * FROM (
               SELECT coalesce(j.doc_id, d.doc_id) AS doc_id,
                      CASE WHEN j.상태 IN ('parsing','failed') THEN j.상태 ELSE d.status END AS 상태,
                      coalesce(j.layer, d.layer) AS layer, j.문서명, j.사업명,
                      coalesce(j.시행일, d.시행일) AS 시행일, d.parse_quality, j.실패사유,
                      coalesce(j.created_at, d.created_at) AS 등록일시
                 FROM corpus.ingest_jobs j
                 FULL JOIN corpus.documents d ON d.doc_id = j.doc_id
                WHERE j.doc_id IS NOT NULL OR d.layer IN ('L1','L2')) t
            WHERE %(s)s::text IS NULL OR 상태 = %(s)s
            ORDER BY 등록일시 DESC, doc_id LIMIT %(n)s""", {"s": 상태, "n": limit})
    이름 = [c.name for c in rows.description]
    return [dict(zip(이름, r)) for r in rows.fetchall()]


def _상태(작업상태: str | None, 문서상태: str | None) -> str | None:
    """파싱 중·실패는 ingest_jobs 가, 그 뒤는 documents.status 가 기준이다."""
    if 작업상태 in ("parsing", "failed"):
        return 작업상태
    return 문서상태
