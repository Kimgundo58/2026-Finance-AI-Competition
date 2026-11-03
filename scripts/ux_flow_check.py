# -*- coding: utf-8 -*-
"""사용자 흐름 점검: L3 업로드 → 파싱 → 저장 → 판정 반영을 실제 API 로 따라가며 확인한다.

사용자가 본 장애(기관 자체규정을 올렸는데 판정은 다른 기관 자체규정 기준으로 나옴)를 다시 만들지 않으려고 둔다.
FastAPI TestClient 로 서버를 프로세스 안에서 띄우고 로컬 DB 에 비특권 롤(suddoe_app, RLS 적용)로 접속한다.
데모 세션 → 업로드 → 백그라운드 파싱 → 상태 조회 → /api/l3/current → /api/judge 까지 실제 경로를 거친다.
판정은 orchestrate.판정 을 dry 로 바꿔 프롬프트 구성까지만 실행한다(LLM 호출 0회, 판정 기록 없음).
검색은 실제로 실행하므로 임베딩 모델 로드에 15초 안팎이 걸린다. 만든 데모 기관은 끝나면 지운다.

점검 항목
  1  게스트 업로드는 401 이다 (조용히 받아들이지 않는다)
  2  업로드 → 202, 상태 '파싱대기'
  3  파싱 후 상태 '완료', 조 건수가 픽스처와 같다
  4  /api/l3/current 에 업로드한 문서가 조 건수와 함께 나온다
  5  판정 파이프라인이 이 기관으로 L3 를 읽고, 읽은 조문이 전부 업로드한 문서의 것이다
  6  판정 프롬프트에 업로드한 문서의 조문이 들어가고, 인용 후보(문장맵)의 L3 조문도 전부 업로드한 문서의 것이다
  7  다른 기관(L3 없음)의 판정에는 이 기관 L3 가 섞이지 않는다
  8  L3 가 없는 기관이 텍스트 없는 PDF 를 올리면 '실패'(실패사유 스캔본)로 끝나고, 적용 중인 규정이 없다고 안내한다
  9  live 응답에 mock 데이터(server/mock_data)가 섞이지 않는다
  10 이미 L3 가 있는 기관이 새 문서를 올리면 판정은 새 문서만 따른다 (기관당 한 문서)
  11 교체 뒤 /api/l3/current 는 새 문서를 맨 앞에 두고 이전 문서는 빼며, 상태 메시지가 교체를 알린다
  12 적용 중인 문서가 있을 때 새 업로드가 실패하면 '실패' 와 함께 기존 문서를 계속 쓴다고 안내하고, 판정도 기존 문서를 읽는다
  13 실패를 거듭해도 /api/l3/current 의 실패 문서는 1건이다
  14 파싱이 끊겨 'pending' 으로 오래 남은 문서는 '실패'(실패사유 중단)로 보이고, 다음 실패 업로드 때 목록에서 빠진다
  15 10분 넘게 걸린 파싱이 그사이 '중단'으로 정리됐다가 성공하면 다시 적용 문서가 된다 (적용 문서 0건이 되지 않는다)
  16 OCR 이 설정되면 스캔본 PDF 를 OCR 로 읽어 '완료'(warn, 추출방식 ocr)로 등록하고 판정이 그 조문을 읽는다.
     이 조문을 인용한 '가능' 은 신뢰등급 B 로 조건부가 된다. CLOVA OCR 호출만 가짜로 바꾼다(네트워크·과금 없음)
  17 OCR 이 없을 때 쪽번호만 텍스트 레이어에 있는 스캔본은 조문 1건짜리로 저장되지 않고 '실패'(스캔본)가 된다
  18 PUT /api/profile 로 저장한 협약 기간·금액이 GET 과 판정 프롬프트 B5 에 나오고, 다시 저장해도 행은 1개다
     (게스트 PUT 은 401, 시작일 > 종료일은 422, 값을 바꾸면 캐시가 아니라 새 값으로 판정한다)

실행:
  PYTHONIOENCODING=utf-8 python scripts/ux_flow_check.py           # 점검 실행
  PYTHONIOENCODING=utf-8 python scripts/ux_flow_check.py --hook    # Claude Code Stop 훅용 (stdin JSON)
종료 코드: 0 통과, 1 점검 실패, 3 실행 환경 없음(로컬 DB 꺼짐 등). --hook 은 아래 _훅() 설명을 본다.
환경변수: UX_CHECK_APP_DSN(기본 suddoe_app 로컬 DSN), UX_CHECK_OWNER_DSN(정리용, 기본 postgres 로컬 DSN).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
APP_DSN = os.environ.get("UX_CHECK_APP_DSN",
                         "postgresql://suddoe_app:devpw@localhost:5432/suddoe")
OWNER_DSN = os.environ.get("UX_CHECK_OWNER_DSN",
                           "postgresql://postgres:devpw@localhost:5432/suddoe")

종료_통과, 종료_실패, 종료_환경없음 = 0, 1, 3

# ── 픽스처 ──────────────────────────────────────────────────────────
# 다른 기관 문서와 겹치지 않는 표식 문자열을 넣어, 판정 프롬프트에 이 문서가 들어갔는지 확인한다.
표식 = "흐름점검기관"
픽스처_파일명 = f"{표식}_사업비_집행지침.hwpx"
픽스처_문단 = [
    f"{표식} 창업사업단 사업비 집행지침",
    "제1장 총칙",
    f"제1조(목적) 이 지침은 {표식} 창업사업단이 지원하는 창업기업의 사업비 집행 기준과 절차를 정하는 것을 목적으로 한다.",
    f"제2조(적용 범위) 이 지침은 {표식} 창업사업단과 협약을 체결한 창업기업이 집행하는 모든 사업비에 적용한다.",
    "제2장 사업비 집행",
    "제3조(재료비) ① 재료비는 시제품 제작에 직접 사용하는 재료와 부품 구입에 한하여 집행할 수 있다. "
    "② 1회 구매 금액이 3,210,000원을 넘는 경우 사업단의 사전 승인을 받아야 한다.",
    "제4조(외주용역비) 외주용역비는 창업기업이 직접 수행하기 어려운 시제품 설계와 제작을 외부에 맡기는 경우에 "
    "집행하며 계약서와 결과보고서를 갖추어야 한다.",
    "제5조(지급수수료) 지급수수료는 사업 수행에 필요한 은행 수수료와 특허 출원 대리 수수료 등에 집행하며 "
    "영수증 등 증빙을 갖추어야 한다.",
    "제6조(증빙) 모든 사업비 집행은 세금계산서 또는 카드매출전표 등 적격증빙을 갖추어야 하며 "
    "증빙이 없는 지출은 인정하지 않는다.",
]
픽스처_조_건수 = 6
교체_표식 = "교체점검기관"
교체_파일명 = f"{교체_표식}_사업비_집행지침.hwpx"
# 실패해도 훅을 막지 않고 [알려진 문제] 로만 보이는 항목 {점검 이름: 사유}. 고치면 여기서 지워 다시 차단 대상으로 만든다.
알려진_문제: dict[str, str] = {}
판정_요청 = {
    "사업명": "예비창업패키지",
    "확정비목": "재료비",
    "정규화": {"질문원문": "시제품 제작용 3D프린터 필라멘트 재료 구매 40만원",
             "품목": "3D프린터 필라멘트", "용도": "시제품 제작", "금액": 400000,
             "비목후보": [{"비목": "재료비", "점수": 0.9}]},
}


def _hwpx(문단들: list[str]) -> bytes:
    """문단 목록으로 최소 HWPX(zip + Contents/section0.xml)를 만든다."""
    p = "".join(f"<hp:p><hp:run><hp:t>{escape(t)}</hp:t></hp:run></hp:p>" for t in 문단들)
    섹션 = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
          'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">' + p + "</hs:sec>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip")      # 무압축, 첫 항목
        z.writestr("Contents/section0.xml", 섹션, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def _빈_pdf(쪽번호: str | None = None) -> bytes:
    """텍스트가 없는 1쪽 PDF(스캔본과 같은 경우). 쪽번호 를 주면 그 글자만 텍스트 레이어에 넣는다."""
    if 쪽번호 is None:
        객체 = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
              b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>"]
    else:
        내용 = f"BT /F1 10 Tf 290 30 Td ({쪽번호}) Tj ET".encode()
        객체 = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
              b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]"
              b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
              f"<</Length {len(내용)}>>\nstream\n".encode() + 내용 + b"\nendstream\n",
              b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, 위치 = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(객체, 1):
        위치.append(len(out))
        out += f"{i} 0 obj".encode() + o + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(객체) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{p:010d} 00000 n \n".encode() for p in 위치)
    out += f"trailer<</Size {len(객체) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


# ── 점검 실행 ───────────────────────────────────────────────────────

class 점검결과:
    def __init__(self) -> None:
        self.항목: list[tuple[str, bool, str]] = []

    def 기록(self, 이름: str, ok: bool, 상세: str = "") -> bool:
        self.항목.append((이름, ok, 상세))
        return ok

    @property
    def 실패(self) -> list[tuple[str, bool, str]]:
        return [h for h in self.항목 if not h[1] and h[0] not in 알려진_문제]

    def 출력(self) -> str:
        줄 = []
        for 이름, ok, 상세 in self.항목:
            if 이름 in 알려진_문제:
                표시 = "통과 — 알려진_문제에서 지울 것" if ok else "알려진 문제"
                상세 = f"{알려진_문제[이름]} · {상세}"
            else:
                표시 = "통과" if ok else "실패"
            줄.append(f"  [{표시}] {이름}" + (f" — {상세}" if 상세 else ""))
        return "\n".join(줄)


def _db_접속가능() -> str | None:
    """앱 롤·소유자 롤로 접속되는지 본다. 안 되면 사유 문자열."""
    import psycopg
    for 이름, dsn in (("앱 롤(UX_CHECK_APP_DSN)", APP_DSN), ("소유자 롤(UX_CHECK_OWNER_DSN)", OWNER_DSN)):
        try:
            psycopg.connect(dsn, connect_timeout=3).close()
        except Exception as e:                                   # noqa: BLE001
            return f"로컬 DB 에 {이름}로 접속하지 못했다 ({type(e).__name__}). cd db && docker compose up -d"
    return None


def _환경_준비(l3_dir: str) -> None:
    """서버 모듈을 import 하기 전에 live 모드 환경을 맞춘다. 운영 값은 건드리지 않는다."""
    os.environ.update({
        "SUDDOE_MOCK": "0",
        "SUDDOE_DSN": APP_DSN,
        "SUDDOE_WARMUP_ON_START": "0",
        "SUDDOE_L3_DIR": l3_dir,             # 업로드 원본을 레포 밖 임시 폴더에 둔다
        "SUDDOE_ALLOW_ORG_PARAM": "0",       # 운영과 같게: 토큰 없는 업로드는 거부
    })
    os.environ.setdefault("SUDDOE_DEMO_SECRET", secrets.token_hex(32))
    os.environ.setdefault("SUDDOE_SLUG_SECRET", secrets.token_hex(32))
    # OCR 은 점검 16 에서만 가짜로 켠다. 실제 키가 있어도 다른 점검은 OCR 없는 경로를 본다(과금 방지)
    for k in ("SUDDOE_JWKS_URL", "SUDDOE_ADMIN_TOKEN", "SUDDOE_OCR_URL", "SUDDOE_OCR_SECRET"):
        os.environ.pop(k, None)
    for p in (str(ROOT / "scripts"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _정리(org_ids: list[str]) -> None:
    """점검에서 만든 데모 기관과 하위 행을 소유자 롤로 지운다(decisions 는 SET NULL 이라 먼저 지운다)."""
    if not org_ids:
        return
    import psycopg
    with psycopg.connect(OWNER_DSN, connect_timeout=5) as c:
        c.execute("DELETE FROM tenant.incidents WHERE org_id = ANY(%s::uuid[])", (org_ids,))
        c.execute("DELETE FROM tenant.decisions WHERE org_id = ANY(%s::uuid[])", (org_ids,))
        c.execute("DELETE FROM tenant.orgs WHERE org_id = ANY(%s::uuid[])", (org_ids,))


def _점검(결과: 점검결과) -> None:
    from fastapi.testclient import TestClient

    import l3_load
    import orchestrate
    from server import mock_data
    from server.main import app

    # 판정 파이프라인 계측: L3 조회·프롬프트를 기록하고, 판정은 dry 로 바꾸고, LLM 호출은 막는다
    캡처: dict[str, list] = {"l3": [], "프롬프트": [], "판정": []}
    원래_조문조회, 원래_프롬프트, 원래_판정 = (
        l3_load.조문_조회, orchestrate.프롬프트_구성, orchestrate.판정)

    def 조문조회_기록(cur, org_id, 비목=None):
        rows = 원래_조문조회(cur, org_id, 비목=비목)
        캡처["l3"].append((str(org_id), rows))
        return rows

    def 프롬프트_기록(*a, **k):
        out = 원래_프롬프트(*a, **k)
        캡처["프롬프트"].append(out)
        return out

    def 판정_dry(질문, **k):
        k.update(dry=True, 기록=False)
        r = 원래_판정(질문, **k)
        캡처["판정"].append((k.get("org_id"), r))
        return r

    def llm_금지(*_a, **_k):
        raise orchestrate.LLM실패("UX 흐름 점검은 LLM 을 호출하지 않는다")

    l3_load.조문_조회, orchestrate.프롬프트_구성, orchestrate.판정 = (
        조문조회_기록, 프롬프트_기록, 판정_dry)
    orchestrate.llm_호출 = llm_금지

    client = TestClient(app, raise_server_exceptions=False)
    만든_기관: list[str] = []
    mock_흔적 = (mock_data.mock_L3["파일명"], "l3-mock-")

    def 데모세션() -> tuple[dict, str]:
        r = client.post("/api/demo/session")
        if r.status_code != 200:
            raise RuntimeError(f"데모 세션 발급 실패 {r.status_code}: {r.text[:200]}")
        토큰 = r.json()["access_token"]
        import jwt
        org = jwt.decode(토큰, options={"verify_signature": False})["org"]
        만든_기관.append(org)
        return {"Authorization": f"Bearer {토큰}"}, org

    def 판정(헤더: dict) -> tuple[str, list, list]:
        for v in 캡처.values():
            v.clear()
        r = client.post("/api/judge", json=판정_요청, headers=헤더)
        return r.text, list(캡처["l3"]), list(캡처["프롬프트"])

    def 업로드(헤더: dict, 파일명: str, 본문: bytes) -> tuple[int, str | None, dict]:
        """업로드하고 (응답 코드, doc_id, 파싱 뒤 상태 JSON) 을 돌려준다. TestClient 는 파싱이 끝난 뒤 돌아온다."""
        r = client.post("/api/l3/upload", headers=헤더,
                        files={"파일": (파일명, 본문, "application/octet-stream")})
        doc = r.json().get("doc_id") if r.status_code == 202 else None
        return r.status_code, doc, (client.get(f"/api/l3/{doc}", headers=헤더).json() if doc else {})

    def 현재문서(헤더: dict) -> list[dict]:
        return client.get("/api/l3/current", headers=헤더).json().get("문서", [])

    def 조문(doc: str | None) -> set[str]:
        if not doc:
            return set()
        import psycopg
        with psycopg.connect(OWNER_DSN, connect_timeout=5) as c:
            return {str(x) for (x,) in c.execute(
                "SELECT article_id FROM tenant.l3_articles WHERE doc_id = %s", (doc,))}

    try:
        헤더, org = 데모세션()
        파일 = {"파일": (픽스처_파일명, _hwpx(픽스처_문단), "application/octet-stream")}

        r = client.post("/api/l3/upload", files=파일)
        결과.기록("1 게스트 업로드는 401", r.status_code == 401,
               f"실제 {r.status_code} (게스트 업로드를 받으면 어느 기관의 자체규정인지 알 수 없다)"
               if r.status_code != 401 else "")

        r = client.post("/api/l3/upload", files=파일, headers=헤더)
        접수 = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        doc_id = 접수.get("doc_id")
        if not 결과.기록("2 업로드 202 · 파싱대기",
                      r.status_code == 202 and 접수.get("상태") == "파싱대기" and bool(doc_id),
                      f"{r.status_code} {r.text[:200]}"):
            return

        # TestClient 는 백그라운드 작업(파싱)이 끝난 뒤에 돌아온다. 그래도 상태는 한 번 더 확인한다
        상태 = client.get(f"/api/l3/{doc_id}", headers=헤더).json()
        결과.기록("3 파싱 후 '완료' · 조 건수 일치",
               상태.get("상태") == "완료" and 상태.get("조_건수") == 픽스처_조_건수,
               f"상태={상태.get('상태')} 조_건수={상태.get('조_건수')} (기대 {픽스처_조_건수}) "
               f"파싱품질={상태.get('파싱품질')} 메시지={상태.get('메시지')}")

        현재 = 현재문서(헤더)
        이것 = [d for d in 현재 if d.get("doc_id") == doc_id]
        결과.기록("4 /api/l3/current 에 업로드 문서 표시",
               len(이것) == 1 and 이것[0].get("조_건수") == 픽스처_조_건수,
               f"current={[(d.get('원본파일명'), d.get('조_건수')) for d in 현재]}")

        import psycopg
        업로드_조문 = 조문(doc_id)

        본문, l3조회, 프롬프트 = 판정(헤더)
        이_기관_조회 = [rows for o, rows in l3조회 if o == org]
        읽은_조문 = {str(a["article_id"]) for rows in 이_기관_조회 for a in rows}
        결과.기록("5 판정이 이 기관 L3 를 읽음 · 전부 업로드 문서 조문",
               bool(이_기관_조회) and bool(읽은_조문) and 읽은_조문 <= 업로드_조문,
               f"L3 조회 org={[o for o, _ in l3조회]} 읽은 조문 {len(읽은_조문)}건 · "
               f"업로드 문서 밖 {len(읽은_조문 - 업로드_조문)}건")

        if 프롬프트:
            텍스트, 문장맵, _ = 프롬프트[-1]
            l3_인용후보 = {str(v[1]) for v in 문장맵.values() if v[0] == "l3"}
            결과.기록("6 판정 프롬프트에 업로드 L3 포함 · 인용 후보도 업로드 문서뿐",
                   표식 in 텍스트 and bool(l3_인용후보) and l3_인용후보 <= 업로드_조문,
                   f"표식 포함={표식 in 텍스트} L3 인용 후보 {len(l3_인용후보)}건 · "
                   f"업로드 문서 밖 {len(l3_인용후보 - 업로드_조문)}건")
        else:
            결과.기록("6 판정 프롬프트에 업로드 L3 포함 · 인용 후보도 업로드 문서뿐", False,
                   "프롬프트 구성 전에 끝났다 (게이트 A/B 또는 오류) — 응답 앞부분: " + 본문[:300])

        mock_섞임 = [m for m in mock_흔적 if m in 본문 or m in json.dumps(현재, ensure_ascii=False)]
        결과.기록("9 live 응답에 mock 데이터 없음", not mock_섞임, f"섞인 값 {mock_섞임}")

        헤더2, org2 = 데모세션()
        본문2, l3조회2, 프롬프트2 = 판정(헤더2)
        다른기관_L3 = [a for o, rows in l3조회2 for a in rows]
        유출됨 = (표식 in 본문2) or any(표식 in p[0] for p in 프롬프트2) or bool(다른기관_L3)
        결과.기록("7 다른 기관 판정에 이 기관 L3 가 섞이지 않음", not 유출됨,
               f"다른 기관 L3 조회 {len(다른기관_L3)}건 · 표식 노출={유출됨}")

        def 실패_목록(h: dict) -> list[dict]:
            return [d for d in 현재문서(h) if d.get("파싱품질") == "fail"]

        # L3 가 없는 기관(org2)이 스캔본을 올린 경우: 실패로 끝나고, 판정은 상위 규정만 쓴다고 알려야 한다
        코드, _, 빈_상태 = 업로드(헤더2, "스캔본.pdf", _빈_pdf())
        결과.기록("8 L3 없는 기관의 스캔본 업로드 → '실패' · 적용 규정 없음 안내",
               빈_상태.get("상태") == "실패" and 빈_상태.get("실패사유") == "스캔본"
               and "적용 중인 기관 규정이 없어" in (빈_상태.get("메시지") or "")
               and not 빈_상태.get("적용중_문서"),
               f"업로드 {코드} · 상태={빈_상태.get('상태')} 실패사유={빈_상태.get('실패사유')} "
               f"적용중_문서={빈_상태.get('적용중_문서')} 메시지={빈_상태.get('메시지')}")

        # 이미 L3 가 있는 기관(예: QA 계정의 경상국립대 기관)이 새 규정을 올린 경우. 판정은 새 문서만 따라야 한다
        새_문단 = [p.replace(표식, 교체_표식) for p in 픽스처_문단]
        코드, 새_doc, 새_상태 = 업로드(헤더, 교체_파일명, _hwpx(새_문단))
        새_조문 = 조문(새_doc)
        _, l3조회3, _ = 판정(헤더)
        읽은3 = {str(a["article_id"]) for o, rows in l3조회3 if o == org for a in rows}
        결과.기록("10 새 L3 를 올리면 판정은 새 문서만 따름 (이전 문서 조문 미사용)",
               bool(새_조문) and bool(읽은3) and 읽은3 <= 새_조문,
               f"업로드 {코드} · 새 문서 조문 {len(새_조문)}건 · 판정이 읽은 조문 {len(읽은3)}건 중 "
               f"이전 문서 조문 {len(읽은3 & 업로드_조문)}건")

        현재3 = 현재문서(헤더)
        이전_상태 = client.get(f"/api/l3/{doc_id}", headers=헤더).json()
        결과.기록("11 교체 뒤 current 맨 앞은 새 문서 · 이전 문서 빠짐 · 메시지로 교체 안내",
               bool(현재3) and 현재3[0].get("doc_id") == 새_doc
               and all(d.get("doc_id") != doc_id for d in 현재3)
               and "이전 규정을 대체해" in (새_상태.get("메시지") or "")
               and "교체되어" in (이전_상태.get("메시지") or ""),
               f"current={[(d.get('원본파일명'), d.get('파싱품질')) for d in 현재3]} · "
               f"새 문서 메시지={새_상태.get('메시지')} · 이전 문서 메시지={이전_상태.get('메시지')}")

        # 적용 중인 문서(새 문서)가 있는 상태에서 다음 업로드가 실패한 경우: 기존 문서를 계속 써야 한다
        코드, _, 실패_상태 = 업로드(헤더, "스캔본.pdf", _빈_pdf())
        _, l3조회4, _ = 판정(헤더)
        읽은4 = {str(a["article_id"]) for o, rows in l3조회4 if o == org for a in rows}
        결과.기록("12 적용 문서가 있을 때 업로드 실패 → '실패' · 기존 문서 계속 사용 안내 · 판정도 기존 문서",
               실패_상태.get("상태") == "실패" and 실패_상태.get("적용중_문서") == 교체_파일명
               and f"「{교체_파일명}」" in (실패_상태.get("메시지") or "")
               and bool(읽은4) and 읽은4 <= 새_조문,
               f"업로드 {코드} · 상태={실패_상태.get('상태')} 적용중_문서={실패_상태.get('적용중_문서')} "
               f"메시지={실패_상태.get('메시지')} · 판정이 읽은 조문 {len(읽은4)}건 중 "
               f"적용 문서 밖 {len(읽은4 - 새_조문)}건")

        업로드(헤더, "스캔본2.pdf", _빈_pdf())
        현재5, 실패5 = 현재문서(헤더), 실패_목록(헤더)
        결과.기록("13 실패를 거듭해도 current 의 실패 문서는 1건 · 적용 문서는 그대로 맨 앞",
               len(실패5) == 1 and 실패5[0].get("원본파일명") == "스캔본2.pdf"
               and bool(현재5) and 현재5[0].get("doc_id") == 새_doc,
               f"current={[(d.get('원본파일명'), d.get('파싱품질')) for d in 현재5]}")

        # 서버 재시작 등으로 파싱이 끊겨 'pending' 으로 남은 문서. 소유자 롤로 1시간 전 행을 만든다
        with psycopg.connect(OWNER_DSN, connect_timeout=5) as c:
            끊긴_doc = str(c.execute(
                "INSERT INTO tenant.l3_documents (org_id, \"원본파일명\", status, extraction, "
                " \"파싱품질\", \"미해결참조수\", created_at) "
                "VALUES (%s, '끊긴파싱.hwpx', 'active', 'hwpx', 'pending', 0, now() - interval '1 hour') "
                "RETURNING doc_id", (org2,)).fetchone()[0])
        끊긴_상태 = client.get(f"/api/l3/{끊긴_doc}", headers=헤더2).json()
        업로드(헤더2, "스캔본3.pdf", _빈_pdf())
        실패6 = 실패_목록(헤더2)
        결과.기록("14 끊긴 'pending' 은 '실패'(중단)로 보이고 다음 실패 업로드 때 current 에서 빠짐",
               끊긴_상태.get("상태") == "실패" and 끊긴_상태.get("실패사유") == "중단"
               and [d.get("원본파일명") for d in 실패6] == ["스캔본3.pdf"],
               f"끊긴 문서 상태={끊긴_상태.get('상태')} 실패사유={끊긴_상태.get('실패사유')} · "
               f"current 실패 문서={[d.get('원본파일명') for d in 실패6]}")

        # 느린 파싱: 업로드 뒤 10분 넘게 파싱 중이던 문서(P)가, 그사이 다른 업로드가 실패하면서 '중단'으로 보고
        # superseded 됐다가 뒤늦게 성공한 경우. P 가 다시 적용 문서가 되고 이전 문서는 내려가야 한다(0건 금지).
        # 시각은 소유자 롤로 맞추고, 파싱은 서버의 백그라운드 작업 함수를 그대로 부른다.
        import l3_parse
        from server import routes_l3
        _, 이전_doc, _ = 업로드(헤더2, "느린파싱기관_이전지침.hwpx", _hwpx(픽스처_문단))
        느린_문단 = [p.replace(표식, "느린파싱기관") for p in 픽스처_문단]
        with psycopg.connect(OWNER_DSN, connect_timeout=5) as c:
            c.execute("UPDATE tenant.l3_documents SET created_at = now() - interval '30 minutes' "
                      "WHERE doc_id = %s", (이전_doc,))
            느린_doc = str(c.execute(
                "INSERT INTO tenant.l3_documents (org_id, \"원본파일명\", status, extraction, "
                " \"파싱품질\", \"미해결참조수\", created_at) "
                "VALUES (%s, '느린파싱기관_새지침.hwpx', 'active', 'hwpx', 'pending', 0, "
                "        now() - interval '15 minutes') RETURNING doc_id", (org2,)).fetchone()[0])
        l3_parse.원본경로(느린_doc, "hwpx").write_bytes(_hwpx(느린_문단))
        업로드(헤더2, "스캔본4.pdf", _빈_pdf())       # 실패 정리가 P 를 '중단'으로 보고 superseded 로 바꾼다
        routes_l3.백그라운드_파싱(느린_doc, org2)     # 그 뒤 P 파싱이 끝난다
        느린_조문 = 조문(느린_doc)
        현재7 = 현재문서(헤더2)
        적용7 = [d for d in 현재7 if d.get("파싱품질") in ("pass", "warn")]
        _, l3조회7, _ = 판정(헤더2)
        읽은7 = {str(a["article_id"]) for o, rows in l3조회7 if o == org2 for a in rows}
        결과.기록("15 '중단'으로 정리된 느린 파싱이 성공하면 다시 적용 문서 · 이전 문서는 내려감 · 판정도 이 문서",
               bool(느린_조문) and [d.get("doc_id") for d in 적용7] == [느린_doc]
               and bool(현재7) and 현재7[0].get("doc_id") == 느린_doc
               and bool(읽은7) and 읽은7 <= 느린_조문,
               f"current={[(d.get('원본파일명'), d.get('파싱품질')) for d in 현재7]} · "
               f"판정이 읽은 조문 {len(읽은7)}건 중 느린 문서 밖 {len(읽은7 - 느린_조문)}건")

        # 스캔본 OCR. CLOVA OCR 호출(clova_ocr._쪽_인식)만 가짜로 바꾸고 렌더링·분할·저장·판정은 실제로 거친다
        import clova_ocr
        import llm_validate
        from llm_schema import 인용
        ocr_문단 = [p.replace(표식, "문자인식기관") for p in 픽스처_문단]
        원래_설정됨, 원래_쪽인식 = clova_ocr.설정됨, clova_ocr._쪽_인식
        clova_ocr.설정됨 = lambda: True
        clova_ocr._쪽_인식 = lambda _c, _이미지, _쪽: {
            "inferResult": "SUCCESS", "fields": [{"inferText": p, "lineBreak": True} for p in ocr_문단]}
        try:
            헤더3, org3 = 데모세션()
            코드, ocr_doc, ocr_상태 = 업로드(헤더3, "스캔본_OCR.pdf", _빈_pdf())
        finally:
            clova_ocr.설정됨, clova_ocr._쪽_인식 = 원래_설정됨, 원래_쪽인식
        ocr_조문 = 조문(ocr_doc)
        현재8 = 현재문서(헤더3)
        _, l3조회8, _ = 판정(헤더3)
        읽은8 = {str(a["article_id"]) for o, rows in l3조회8 if o == org3 for a in rows}
        # 인용 단계: 이 문서 조문의 extraction 이 'ocr' 로 읽히고, 이를 인용한 '가능' 은 조건부가 된다.
        # 인용 계층에 L1 을 넣어 L3 단독 하향(L3_ONLY_DOWNGRADE)과 섞이지 않게 한다
        메타 = llm_validate.문장ID_메타({"S01": ("l3", int(min(ocr_조문, key=int)), None)},
                                    org_id=org3) if ocr_조문 else {}
        기록 = llm_validate._검증기록()
        인용들 = [인용(문장ID="S01", extraction=메타.get("S01", {}).get("extraction"))]
        등급 = llm_validate._신뢰등급(인용들, None, 기록)
        하향, _ = llm_validate._판정_하향("가능", 등급, 인용들, ["L1", "L3"], None, None, 기록)
        결과.기록("16 OCR 설정 시 스캔본 → OCR 로 '완료'(warn·ocr) · 판정이 읽음 · 인용한 '가능' 은 조건부",
               ocr_상태.get("상태") == "완료" and ocr_상태.get("조_건수") == 픽스처_조_건수
               and ocr_상태.get("파싱품질") == "warn" and ocr_상태.get("추출방식") == "ocr"
               and "OCR 로 읽었습니다" in (ocr_상태.get("메시지") or "")
               and bool(현재8) and 현재8[0].get("doc_id") == ocr_doc and 현재8[0].get("추출방식") == "ocr"
               and bool(읽은8) and 읽은8 <= ocr_조문
               and 등급 == "B" and 하향 == "조건부" and "B_GRADE_DOWNGRADE" in 기록.코드,
               f"업로드 {코드} · 상태={ocr_상태.get('상태')} 조={ocr_상태.get('조_건수')} "
               f"품질={ocr_상태.get('파싱품질')} 추출방식={ocr_상태.get('추출방식')} · "
               f"판정이 읽은 조문 {len(읽은8)}건 · 인용 extraction={메타.get('S01', {}).get('extraction')} "
               f"등급={등급} 가능→{하향} · 메시지={ocr_상태.get('메시지')}")

        # 쪽번호만 텍스트 레이어에 있는 스캔본(OCR 없음): 쪽번호를 조문 1건으로 저장하면 안 된다(백로그 L3 업로드)
        코드, 쪽번호_doc, 쪽번호_상태 = 업로드(헤더3, "쪽번호만_있는_스캔본.pdf", _빈_pdf("- 1 -"))
        결과.기록("17 쪽번호만 있는 스캔본(OCR 없음) → '실패'(스캔본) · 조문 저장 없음 · 기존 문서 유지",
               쪽번호_상태.get("상태") == "실패" and 쪽번호_상태.get("실패사유") == "스캔본"
               and not 조문(쪽번호_doc) and 쪽번호_상태.get("적용중_문서") == "스캔본_OCR.pdf",
               f"업로드 {코드} · 상태={쪽번호_상태.get('상태')} 실패사유={쪽번호_상태.get('실패사유')} "
               f"조문 {len(조문(쪽번호_doc))}건 · 적용중_문서={쪽번호_상태.get('적용중_문서')}")

        # 마이페이지 협약 4칸 → PUT /api/profile → tenant.profile_agreements → 판정 프롬프트 B5.
        # 두 번 저장해 두 번째 값이 B5 에 나오는지 본다(첫 판정 결과가 캐시로 답하면 프롬프트가 안 만들어진다)
        def 협약(시작, 종료, 정부, 자기) -> dict:
            return {"협약": {"협약시작일": 시작, "협약종료일": 종료,
                           "정부지원_현금": 정부, "자기부담_현금": 자기}}

        def b5(프롬프트들: list) -> str:
            텍스트 = 프롬프트들[-1][0] if 프롬프트들 else ""
            i = 텍스트.find("## B5.")
            return 텍스트[i:텍스트.find("\n## ", i + 1)] if i >= 0 else ""

        게스트_put = client.put("/api/profile", json=협약("2026-04-02", "2026-11-29", 43210000, 1234567))
        거꾸로 = client.put("/api/profile", headers=헤더2,
                         json=협약("2026-12-01", "2026-04-01", 1, 1))
        저장들, b5들 = [], []
        for 값 in (("2026-04-02", "2026-11-29", 43210000, 1234567),
                   ("2026-05-03", "2026-10-30", 51234000, 2345678)):
            r = client.put("/api/profile", headers=헤더2, json=협약(*값))
            저장들.append((r.status_code, r.json() if r.status_code == 200 else r.text[:200]))
            _, _, 프롬프트9 = 판정(헤더2)
            b5들.append(b5(프롬프트9))
        읽음 = client.get("/api/profile", headers=헤더2).json().get("협약", {})
        with psycopg.connect(OWNER_DSN, connect_timeout=5) as c:
            행수 = c.execute("SELECT count(*) FROM tenant.profile_agreements WHERE org_id = %s",
                           (org2,)).fetchone()[0]
        기대 = [("협약시작일: 2026-04-02", "협약종료일: 2026-11-29", "정부지원(현금): 43210000",
                 "자기부담(현금): 1234567"),
                ("협약시작일: 2026-05-03", "협약종료일: 2026-10-30", "정부지원(현금): 51234000",
                 "자기부담(현금): 2345678")]
        결과.기록("18 PUT /api/profile 협약값 → GET · 판정 프롬프트 B5 반영 · 행 1개 · 게스트 401 · 역순 기간 422",
               게스트_put.status_code == 401 and 거꾸로.status_code == 422
               and all(s == 200 and isinstance(j, dict) and j.get("저장") is True for s, j in 저장들)
               and 읽음 == {"협약시작일": "2026-05-03", "협약종료일": "2026-10-30",
                           "정부지원_현금": 51234000.0, "자기부담_현금": 2345678.0}
               and 행수 == 1 and all(all(x in b for x in 기대[i]) for i, b in enumerate(b5들)),
               f"게스트 {게스트_put.status_code} · 역순 {거꾸로.status_code} · 저장 {[s for s, _ in 저장들]} · "
               f"GET {읽음} · 행 {행수}개 · B5 {[b.replace(chr(10), ' / ') for b in b5들]}")
    finally:
        l3_load.조문_조회, orchestrate.프롬프트_구성, orchestrate.판정 = (
            원래_조문조회, 원래_프롬프트, 원래_판정)
        try:
            _정리(만든_기관)
        except Exception as e:                                   # noqa: BLE001
            결과.기록("정리(데모 기관 삭제)", False, f"{type(e).__name__}: {e}")


def 실행() -> tuple[int, str]:
    """점검을 실행해 (종료 코드, 보고문) 을 돌려준다."""
    사유 = _db_접속가능()
    if 사유:
        return 종료_환경없음, 사유
    결과 = 점검결과()
    with tempfile.TemporaryDirectory(prefix="ux_flow_l3_") as d:
        _환경_준비(d)
        t = time.time()
        try:
            _점검(결과)
        except Exception as e:                                   # noqa: BLE001
            결과.기록("점검 실행", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
    보고 = f"UX 흐름 점검 (L3 업로드 → 파싱 → 판정 반영) {time.time() - t:.0f}초\n{결과.출력()}"
    return (종료_실패 if 결과.실패 else 종료_통과), 보고


# ── Claude Code Stop 훅 ─────────────────────────────────────────────
# 흐름에 영향을 주는 코드(server·scripts·db)가 바뀐 상태로 Claude 가 턴을 끝내려 할 때 점검을 실행한다.
# 같은 변경 상태로 이미 통과했으면 다시 돌리지 않는다. 실패하면 exit 2 로 종료를 막고 실패 내용을
# Claude 에게 넘긴다(Stop 훅 규약). 같은 상태로 한 번 막은 뒤에도 그대로면 무한 반복을 피하려고 통과시키고
# 경고만 띄운다. DB 가 꺼져 있으면 막지 않고 경고만 띄운다.

_감시_경로 = ("server", "scripts", "db/init", "db/migrations")
_감시_확장자 = (".py", ".sql")
_상태파일 = ROOT / ".claude" / "ux_flow_check_state.json"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def _변경_해시() -> str | None:
    """감시 경로의 커밋되지 않은 변경 내용의 해시. 변경이 없으면 None."""
    diff = _git("diff", "HEAD", "--", *_감시_경로)
    새파일 = [f for f in _git("ls-files", "--others", "--exclude-standard", "--", *_감시_경로).splitlines()
           if f.endswith(_감시_확장자)]
    diff = "\n".join(줄 for 줄 in diff.split("\ndiff --git ")
                   if any(줄.split("\n", 1)[0].rstrip().endswith(x) for x in _감시_확장자))
    if not diff.strip() and not 새파일:
        return None
    h = hashlib.sha256(diff.encode("utf-8", "replace"))
    for f in sorted(새파일):
        h.update(f.encode())
        h.update((ROOT / f).read_bytes())
    return h.hexdigest()


def _상태_읽기() -> dict:
    try:
        return json.loads(_상태파일.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _상태_쓰기(변경해시: str, 판정: str) -> None:
    try:
        _상태파일.parent.mkdir(exist_ok=True)
        _상태파일.write_text(json.dumps({"변경해시": 변경해시, "결과": 판정}), encoding="utf-8")
    except OSError:
        pass


def _훅() -> int:
    try:
        입력 = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        입력 = {}
    변경해시 = _변경_해시()
    if 변경해시 is None:
        return 0
    이전 = _상태_읽기()
    if 이전.get("변경해시") == 변경해시 and 이전.get("결과") == "통과":
        return 0
    if 입력.get("stop_hook_active") and 이전.get("변경해시") == 변경해시 and 이전.get("결과") == "실패":
        print(json.dumps({"systemMessage": "UX 흐름 점검 실패가 아직 남아 있습니다. "
                          "python scripts/ux_flow_check.py 로 확인하세요."}, ensure_ascii=False))
        return 0

    try:
        완료 = subprocess.run([sys.executable, str(Path(__file__).resolve())], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=280)
        코드, 보고 = 완료.returncode, (완료.stdout + 완료.stderr).strip()
    except subprocess.TimeoutExpired:
        코드, 보고 = 종료_환경없음, "UX 흐름 점검이 280초 안에 끝나지 않았다"

    if 코드 == 종료_통과:
        _상태_쓰기(변경해시, "통과")
        return 0
    if 코드 == 종료_환경없음:
        print(json.dumps({"systemMessage": f"UX 흐름 점검을 건너뛰었습니다: {보고[-300:]}"},
                         ensure_ascii=False))
        return 0
    _상태_쓰기(변경해시, "실패")
    sys.stderr.write(
        "코드 변경 뒤 UX 흐름 점검(L3 업로드 → 파싱 → 판정 반영)이 실패했다. "
        "원인을 고치고 다시 확인한다. 점검 자체가 틀렸다고 판단되면 그 근거를 사용자에게 설명한다.\n"
        + 보고[-4000:] + "\n")
    return 2


def main() -> None:
    if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hook", action="store_true", help="Claude Code Stop 훅으로 실행 (stdin JSON)")
    a = ap.parse_args()
    if a.hook:
        sys.exit(_훅())
    코드, 보고 = 실행()
    print(보고)
    sys.exit(코드)


if __name__ == "__main__":
    main()
