# -*- coding: utf-8 -*-
"""L3 업로드 → 파싱 → 상태 전이 회귀.   **실 DB 로 태운다.**

    PYTHONIOENCODING=utf-8 python -m pytest tests/test_l3_파싱전이.py -q

🔴 **왜 이 파일이 따로 있나**
`test_l3_upload.py` 는 목 전용이라 「접수까지」만 본다. 그런데 2026-09-02 까지
이 서비스의 제일 큰 구멍이 **접수 다음**에 있었다:

    「접수했습니다」 202 → 행만 생기고 → 파일은 버려지고 → 영원히 「분석 중」

415(확장자 거부)는 실패라고 말해주는데 202 는 **성공했다고 말하고 아무 일도 안 했다.**
RAG 축이 「① L3 먼저」로 시작하는데 L3 가 들어올 길이 없던 것이다.
그 전이가 **실제로 일어나는지**는 실 DB 로만 확인된다 — 그래서 이 파일이 있다.

■ 이 파일이 잠그는 것
  ① 업로드한 원본이 디스크에 **실제로 남는가**
  ② 배경 파싱이 `파싱품질='대기'` 를 **pass/fail 로 덮는가** (대기에 갇히지 않는가)
  ③ `_실_상태()` 의 파생 규칙이 그 값을 「완료/실패」로 **바꿔 내보내는가**
  ④ `l3_articles.org_id` 가 채워지는가 — 🔴 비면 그 문서가 **아무에게도 안 보인다**

■ 픽스처는 자기가 만든 doc_id 로만 지운다. 남의 행은 건드리지 않는다.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient          # noqa: E402

from server import routes_l3                       # noqa: E402
from server._common import DSN                     # noqa: E402
from server.main import app                        # noqa: E402

실DB = True                                         # conftest 가 MOCK=False 로 세운다

client = TestClient(app)


def _접속():
    import psycopg
    return psycopg.connect(DSN, connect_timeout=5)


def _쓸만한_기관() -> str | None:
    """L3 문서를 이미 가진 기관 하나. 없으면 None."""
    try:
        with _접속() as c:
            r = c.execute(
                "SELECT org_id FROM tenant.orgs "
                "WHERE org_id IN (SELECT org_id FROM tenant.l3_documents) LIMIT 1"
            ).fetchone()
            return str(r[0]) if r else None
    except Exception:                                          # noqa: BLE001
        return None


_기관 = _쓸만한_기관()
pytestmark = pytest.mark.skipif(
    _기관 is None, reason="tenant DB 미기동 또는 L3 문서를 가진 기관이 없다")


def _진짜_hwpx() -> bytes | None:
    """코퍼스의 실제 hwpx 하나. 🔴 가짜(빈 Contents/)로는 이 전이를 못 잰다 —
    파서가 열어보고 fail 을 내므로 「완료」 경로가 안 밟힌다."""
    for p in ROOT.rglob("*.hwpx"):
        if "archive" in p.parts:
            continue
        본문 = p.read_bytes()
        if routes_l3.실제형식(본문) == "hwpx":
            return 본문
    return None


@pytest.fixture
def 정리():
    """만든 doc_id 만 회수한다 — 행·조문·디스크 파일 셋 다."""
    만든: list[str] = []
    yield 만든
    if not 만든:
        return
    with _접속() as conn:
        for d in 만든:
            conn.execute("DELETE FROM tenant.l3_articles WHERE doc_id = %s", (d,))
            conn.execute("DELETE FROM tenant.l3_documents WHERE doc_id = %s", (d,))
        conn.commit()
    for d in 만든:
        for 확장 in ("hwpx", "hwp", "pdf"):
            routes_l3.원본경로(d, 확장).unlink(missing_ok=True)


def _올리기(파일명: str, 본문: bytes):
    return client.post("/api/l3/upload",
                       files={"파일": (파일명, 본문, "application/octet-stream")},
                       data={"org_id": _기관})


def test_업로드한_원본이_디스크에_남는다(정리):
    """🔴 2026-09-02 까지 `_실_업로드()` 는 파일 바이트를 받아놓고 «버렸다».
    행만 만들고 202 를 줬다 — 파일이 없으니 파서가 붙어도 할 일이 없었다."""
    본문 = _진짜_hwpx()
    if 본문 is None:
        pytest.skip("코퍼스에 실제 hwpx 가 없다")
    r = _올리기("전이시험.hwpx", 본문)
    assert r.status_code == 202, r.text
    doc_id = r.json()["doc_id"]
    정리.append(doc_id)

    경로 = routes_l3.원본경로(doc_id, "hwpx")
    assert 경로.exists(), f"업로드한 원본이 없다: {경로}"
    assert 경로.read_bytes() == 본문, "저장된 바이트가 올린 것과 다르다"


def test_파싱이_대기를_안_남긴다(정리):
    """🔴 이 파일의 핵심. `파싱품질='대기'` 로 남으면 사용자는 영원히 「분석 중」이다.

    성공이든 실패든 **결론이 나야 한다.** 「아직 모른다」가 남는 게 제일 나쁘다 —
    415 는 실패라고 말해주는데 202 는 성공했다고 말하고 아무 일도 안 하기 때문이다.
    """
    본문 = _진짜_hwpx()
    if 본문 is None:
        pytest.skip("코퍼스에 실제 hwpx 가 없다")
    r = _올리기("전이시험.hwpx", 본문)
    assert r.status_code == 202, r.text
    doc_id = r.json()["doc_id"]
    정리.append(doc_id)

    with _접속() as conn:
        품질 = conn.execute(
            'SELECT "파싱품질" FROM tenant.l3_documents WHERE doc_id = %s',
            (doc_id,)).fetchone()[0]
    assert 품질 != "대기", (
        "배경 파싱이 끝났는데 파싱품질이 '대기' 다 — 사용자는 영원히 「분석 중」을 본다")
    assert 품질 in ("pass", "warn", "fail"), f"모르는 파싱품질: {품질!r}"


def test_상태가_파싱대기에서_넘어간다(정리):
    """`_실_상태()` 의 파생 규칙이 실제로 「완료/실패」를 내보내는지.

    이 규칙 자체는 안 바뀌었지만, **파싱품질이 안 바뀌면 이 규칙도 무의미**했다 —
    입력이 늘 '대기' 라 출력이 늘 「파싱대기」였다.
    """
    본문 = _진짜_hwpx()
    if 본문 is None:
        pytest.skip("코퍼스에 실제 hwpx 가 없다")
    r = _올리기("전이시험.hwpx", 본문)
    assert r.status_code == 202, r.text
    doc_id = r.json()["doc_id"]
    정리.append(doc_id)

    s = client.get(f"/api/l3/{doc_id}", params={"org_id": _기관})
    assert s.status_code == 200, s.text
    상태 = s.json()["상태"]
    assert 상태 in ("완료", "실패"), (
        f"업로드 후 상태가 「{상태}」다 — 「파싱대기」에 머물면 온보딩 스피너가 안 멈춘다")


def test_조문에_org_id_가_반드시_채워진다(정리):
    """🔴 `l3_articles.org_id` 는 판정 검색이 기관을 못 넘게 하는 축이다.

    비면 «격리가 뚫리는» 게 아니라 **그 문서가 아무에게도 안 보인다** —
    `l3_load` 가 `a.org_id AND d.org_id` 를 양쪽 다 걸기 때문이다. 조용히 사라진다.
    """
    본문 = _진짜_hwpx()
    if 본문 is None:
        pytest.skip("코퍼스에 실제 hwpx 가 없다")
    r = _올리기("전이시험.hwpx", 본문)
    assert r.status_code == 202, r.text
    doc_id = r.json()["doc_id"]
    정리.append(doc_id)

    with _접속() as conn:
        전체, 빈칸 = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE org_id IS NULL) "
            "FROM tenant.l3_articles WHERE doc_id = %s", (doc_id,)).fetchone()
    if 전체 == 0:
        pytest.skip("이 문서에서 조문이 안 나왔다 (파싱 실패 경로) — org_id 를 잴 대상이 없다")
    assert 빈칸 == 0, f"조문 {전체}건 중 {빈칸}건에 org_id 가 없다 — 그 문서는 아무에게도 안 보인다"


def test_위장_파일은_애초에_안_들어온다(정리):
    """확장자만 hwpx 이고 내용이 XLSX 인 파일. 실물 코퍼스에 존재한다.

    🔴 **매직바이트로는 못 잡는다** — HWPX·XLSX 가 둘 다 `PK\x03\x04` 로 시작한다.
    415 로 막히므로 `doc_id` 자체가 안 생기고, 따라서 「파싱대기」에 앉을 일도 없다.
    """
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for e in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"):
            z.writestr(e, "x")
    r = _올리기("위장.hwpx", b.getvalue())
    assert r.status_code == 415, r.text
    assert "xlsx" in r.json()["오류"], r.text
