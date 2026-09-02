# -*- coding: utf-8 -*-
"""L3 업로드 회귀 테스트 — `API_계약_v1.0.md` §7 대조.

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

import io          # noqa: E402
import zipfile     # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from server.main import app                # noqa: E402

client = TestClient(app)

# 🔴 2026-09-02 — 픽스처가 `b"dummy bytes"` 였다. 서버가 이제 파일 **내용**을 보므로
#    확장자에 맞는 진짜 헤더를 만들어 준다.
#
#    확장자만 믿으면 안 되는 **실물 증거**가 있다 — 코퍼스의
#    `민관공동창업자발굴육성(TIPS)….hwpx` 는 내부가 XLSX 다.
#    🔴 그리고 매직바이트만으로도 부족하다: HWPX·XLSX·DOCX 가 전부 ZIP 이라
#    앞 4바이트가 똑같이 `PK\x03\x04` 다. 서버는 ZIP 안의 항목 이름까지 본다
#    (`Contents/` = hwpx · `xl/` = xlsx). 픽스처도 그 수준이어야 202 가 난다.


def _zip(항목들: list[str]) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for e in 항목들:
            z.writestr(e, "x")
    return b.getvalue()


_OLE = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])   # 구형 HWP·DOC·XLS 공용

_본문표 = {
    "pdf":  b"%PDF-1.4" + bytes([0x0A]) + b"x" * 64,
    "hwpx": _zip(["mimetype", "version.xml", "Contents/header.xml"]),
    "hwp":  _OLE + b"x" * 64,
    # 🔴 위장 파일 — 확장자는 hwpx 인데 내용은 XLSX 다. 실물에 이런 게 있다
    "위장_xlsx": _zip(["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"]),
}
_본문 = _본문표["pdf"]


def _업로드(파일명: str, 본문: bytes | None = None):
    확장 = 파일명.rsplit(".", 1)[-1].lower() if "." in 파일명 else ""
    if 본문 is None:
        본문 = _본문표.get(확장, _본문)
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
    """🔴 크기 검사가 내용 검사보다 **먼저** 와야 한다.

    순서가 뒤집히면 30MB 짜리 더미가 413 이 아니라 415 로 나간다 — 사용자에게
    「파일이 크다」 대신 「형식이 틀렸다」고 말하는 셈이라 고칠 방향을 잘못 알려준다.
    """
    r = _업로드("규정.pdf", b"0" * (30 * 1024 * 1024 + 1))
    assert r.status_code == 413


# ════════════════════════════════════════════════════════════════════
# 🔴 확장자가 거짓말하는 경우 — 2026-09-02 신설
# ════════════════════════════════════════════════════════════════════

def test_내용이_확장자와_다르면_415():
    """실물에 있는 사고다 — `….hwpx` 인데 내부가 XLSX.

    🔴 **매직바이트만으로는 못 잡는다.** 둘 다 `PK\x03\x04` 로 시작한다.
    이 테스트가 초록이면 서버가 ZIP 안까지 봤다는 뜻이다.
    """
    r = _업로드("규정.hwpx", _본문표["위장_xlsx"])
    assert r.status_code == 415, r.text
    assert "xlsx" in r.json()["오류"], r.text


def test_더미_바이트는_415():
    """`b"dummy bytes"` 처럼 아무 형식도 아닌 것은 통과하면 안 된다.
    통과시키면 파서가 나중에 조용히 실패한다 — 202 를 주고 「분석 중」에 갇힌다."""
    r = _업로드("규정.pdf", b"dummy bytes")
    assert r.status_code == 415, r.text


def test_pdf_자리에_zip_을_넣으면_415():
    r = _업로드("규정.pdf", _본문표["hwpx"])
    assert r.status_code == 415, r.text


if __name__ == "__main__":
    for _이름, _fn in sorted(globals().items()):
        if _이름.startswith("test_"):
            _fn()
            print(f"  ok  {_이름}")
    print("전부 통과")
