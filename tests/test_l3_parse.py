# -*- coding: utf-8 -*-
"""L3 파싱 회귀 — `scripts/l3_parse.py`.

🔴 실 DB 를 쓴다(파싱은 DB 없이 검증할 수 없다 — 조문을 넣고 다시 읽어야 한다).
   9개 세션이 같은 Postgres 를 본다 — **이 테스트가 만든 행만** 지운다.
   전역 `count(*)` 로 단언하지 않는다 (2026-09-02 오전 사고 두 건이 그것이었다).

   pytest tests/test_l3_parse.py -q
   python tests/test_l3_parse.py            (pytest 없이도 돈다)
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402

import l3_parse  # noqa: E402

DSN = l3_parse.DSN

# 진짜 규정 PDF 하나를 성공 경로 픽스처로 쓴다(3.6KB, 조 구조가 실재한다) —
# 가짜 바이트를 만들지 않는다. 이 파일은 읽기만 하고 옮기지 않는다.
_성공_PDF = ROOT / "2026_Finance_DATA_FOR_RAG/중기부/공공재정부정청구금지및부정이익환수등에관한법률_제8조.pdf"


def _org_생성(cur) -> str:
    org_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO tenant.orgs (org_id, \"기관명\", \"사업명\", \"주소\", \"부서\") "
        "VALUES (%s,%s,%s,%s,%s)",
        (org_id, "_테스트_l3_parse용_임시기관", ["예비창업패키지"], None, None))
    return org_id


def _doc_생성(cur, org_id: str, 원본파일명: str, extraction: str = "native") -> str:
    row = cur.execute(
        "INSERT INTO tenant.l3_documents "
        " (org_id, \"원본파일명\", status, extraction, \"파싱품질\", \"dangling수\") "
        "VALUES (%s,%s,'active',%s,'대기',0) RETURNING doc_id",
        (org_id, 원본파일명, extraction)).fetchone()
    return str(row[0])


def _정리(conn, org_ids: list[str], 파일들: list[Path]) -> None:
    """이 테스트가 만든 것만 지운다 — org_id 로 좁혀서 지우지, 전역으로 안 지운다."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenant.l3_documents WHERE org_id = ANY(%s)", (org_ids,))
        cur.execute("DELETE FROM tenant.orgs WHERE org_id = ANY(%s)", (org_ids,))
    conn.commit()
    for f in 파일들:
        f.unlink(missing_ok=True)


def test_성공_경로_실_pdf_로_조문이_들어가고_파싱품질이_pass_또는_warn() -> None:
    assert _성공_PDF.exists(), f"픽스처 PDF 가 없다: {_성공_PDF}"
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        org_id = _org_생성(cur)
        doc_id = _doc_생성(cur, org_id, "테스트_공공재정부정청구금지법_제8조.pdf")
        conn.commit()

        경로 = l3_parse.원본경로(doc_id, "pdf")
        l3_parse.L3_저장소.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_성공_PDF, 경로)
        try:
            r = l3_parse.파싱(cur, doc_id)
            conn.commit()

            assert r["파싱품질"] in ("pass", "warn"), r
            assert r["조_건수"] > 0, r

            # DB 에 실제로 들어갔는지 + org_id 가 채워졌는지(BE 지적 — 격리 축이다)
            품질, dang = cur.execute(
                "SELECT \"파싱품질\", \"dangling수\" FROM tenant.l3_documents "
                "WHERE doc_id=%s", (doc_id,)).fetchone()
            assert 품질 == r["파싱품질"]
            assert dang == r["dangling수"]

            건수, org수 = cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE org_id=%s) "
                "  FROM tenant.l3_articles WHERE doc_id=%s",
                (org_id, doc_id)).fetchone()
            assert 건수 > 0 and 건수 == org수, "org_id 가 안 채워진 행이 있다 — 격리가 샌다"
        finally:
            _정리(conn, [org_id], [경로])


def test_파일_없으면_파싱품질이_fail_이고_대기로_안_남는다() -> None:
    """🔴 「접수했습니다」→ 영원히 「분석 중」을 막는 자리. fail 도 값이다."""
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        org_id = _org_생성(cur)
        doc_id = _doc_생성(cur, org_id, "존재하지않는파일.pdf")
        conn.commit()
        try:
            r = l3_parse.파싱(cur, doc_id)
            conn.commit()
            assert r["파싱품질"] == "fail"
            assert r["ok"] is False

            품질 = cur.execute(
                "SELECT \"파싱품질\" FROM tenant.l3_documents WHERE doc_id=%s",
                (doc_id,)).fetchone()[0]
            assert 품질 == "fail"
            assert 품질 != "대기"
        finally:
            _정리(conn, [org_id], [])


def test_조_0건이면_pass가_아니라_fail이다() -> None:
    """🔴 오늘 세 번 물린 자리 — 추출은 «성공」해도 조가 0개면 실패로 닫는다."""
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()
        org_id = _org_생성(cur)
        doc_id = _doc_생성(cur, org_id, "테스트_빈문서.pdf")
        conn.commit()

        경로 = l3_parse.원본경로(doc_id, "pdf")
        l3_parse.L3_저장소.mkdir(parents=True, exist_ok=True)
        # 조문 패턴이 전혀 없는 최소 PDF — split_articles 의 5단 전부가 0건이어야 한다.
        # (paragraph 폴백까지 가도 100자 미만 문단은 안 남는다)
        경로.write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
                          b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                          b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                          b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
                          b"trailer<</Root 1 0 R>>")
        try:
            r = l3_parse.파싱(cur, doc_id)
            conn.commit()
            assert r["파싱품질"] == "fail", r
            assert r["조_건수"] == 0
        finally:
            _정리(conn, [org_id], [경로])


def test_조1건이면_validate의_V2가_이미_잡아서_warn이다() -> None:
    """🔴 「조 1건 + 본문 수천 자」를 잡는 별도 게이트를 만들어봤다가 뺐다 —
    `stage0_articles.validate()` 의 V2(조 5건 미만이면 무조건 발동)가 조 1건인
    모든 경우를 이미 잡고 있어서 별도 코드가 죽은 분기였다. 그 사실 자체를
    회귀 테스트로 남긴다: 조 1건이면 flags 가 뭐가 됐든 V2 가 이미 차 있어야 한다."""
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from stage0_articles import validate

    v = validate([{"조번호": "단락001", "본문": "x" * 5000}], "paragraph")
    assert any("V2" in f for f in v["flags"]), v["flags"]

    품질, flags = l3_parse._품질판정([{"조번호": "단락001"}], validate_flags=v["flags"])
    assert 품질 == "warn", (품질, flags)

    # 대조군 — flags 가 정말 비어 있으면(조 5건 이상 + 구조 튼튼) pass 로 나간다
    품질2, flags2 = l3_parse._품질판정([{"조번호": f"제{i}조"} for i in range(1, 6)], [])
    assert 품질2 == "pass", (품질2, flags2)


if __name__ == "__main__":
    test_성공_경로_실_pdf_로_조문이_들어가고_파싱품질이_pass_또는_warn()
    test_파일_없으면_파싱품질이_fail_이고_대기로_안_남는다()
    test_조_0건이면_pass가_아니라_fail이다()
    test_조1건이면_validate의_V2가_이미_잡아서_warn이다()
    print("✔ 전부 통과")
