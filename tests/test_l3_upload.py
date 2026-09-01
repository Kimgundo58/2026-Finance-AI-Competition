# -*- coding: utf-8 -*-
"""L3 업로드 회귀 테스트 — `프론트_API_계약_v1.0.md` §7 대조.

🔴 MOCK 모드(기본, DB 없이 돈다)만 검증한다. `_실_업로드`/`_실_상태` 의 DB 경로는
   `SUDDOE_MOCK=0` + 실 DSN 이 있어야 도는데, 그건 이 테스트의 책임이 아니다
   (계약·확장자 통과 조건이 이 파일의 검증 대상이다).

    pytest tests/test_l3_upload.py -q
    python tests/test_l3_upload.py            (pytest 없이도 돈다)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from server.main import app                # noqa: E402

client = TestClient(app)

_본문 = b"dummy bytes"


def _업로드(파일명: str, 본문: bytes = _본문):
    return client.post("/api/l3/upload",
                       files={"파일": (파일명, 본문, "application/octet-stream")},
                       data={"org_id": "org-test-1"})


def test_pdf_hwpx_hwp_202():
    for 파일명 in ("규정.pdf", "규정.hwpx", "규정.hwp"):
        r = _업로드(파일명)
        assert r.status_code == 202, (파일명, r.text)
        j = r.json()
        assert j["확장자"] == 파일명.rsplit(".", 1)[-1]
        assert j["상태"] == "파싱대기"


def test_docx_doc_415():
    for 파일명 in ("규정.docx", "규정.doc"):
        r = _업로드(파일명)
        assert r.status_code == 415, (파일명, r.text)


def test_알수없는_확장자_415():
    r = _업로드("규정.txt")
    assert r.status_code == 415


def test_빈_파일_400():
    r = _업로드("규정.pdf", b"")
    assert r.status_code == 400


def test_30MB_초과_413():
    r = _업로드("규정.pdf", b"0" * (30 * 1024 * 1024 + 1))
    assert r.status_code == 413


if __name__ == "__main__":
    for _이름, _fn in sorted(globals().items()):
        if _이름.startswith("test_"):
            _fn()
            print(f"  ok  {_이름}")
    print("전부 통과")
