# -*- coding: utf-8 -*-
"""hwpx 파서 + stage0 디스패처 회귀 테스트.

    pytest tests/test_hwpx_extract.py -q

■ 무엇을 지키나
  ① 진짜 hwpx(실물 픽스처)가 `stage0_extract.extract()` 경로에서 죽지 않고 조문이 나온다
  ② 문단·표 셀 경계가 줄바꿈으로 살아 있다 (합성 픽스처로 정확히 검증)
  ③ 🔴 확장자 위장 — `.hwpx` 인데 내부가 XLSX 인 실물(TIPS 2026)은
     빈 문자열이 아니라 정체를 밝힌 `NotHwpxError` 로 실패한다
  ④ `.hwp` 확장자에 zip(hwpx) 내용물이 와도 내용물로 갈라 보낸다

실물 픽스처가 없으면 그 테스트만 skip 한다. 합성 픽스처 테스트는 항상 돈다.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import hwpx_extract as hx            # noqa: E402
import stage0_extract as s0          # noqa: E402

실물_HWPX = ROOT / "2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).hwpx"
실물_위장XLSX = ROOT / "2026_Finance_DATA_FOR_RAG/창진원/민관공동창업자발굴육성(TIPS)/2026/TIPS 운영사 현황(2026년).hwpx"

_NS = ('xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
       'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"')


def _p(*runs: str) -> str:
    return "<hp:p>" + "".join(f"<hp:run><hp:t>{r}</hp:t></hp:run>" for r in runs) + "</hp:p>"


def _cell(*paras: str) -> str:
    return "<hp:tc><hp:subList>" + "".join(_p(x) for x in paras) + "</hp:subList></hp:tc>"


def _hwpx(tmp_path: Path, section_xml: str, name: str = "합성.hwpx", mimetype: str | None = hx.HWPX_MIMETYPE) -> Path:
    f = tmp_path / name
    with zipfile.ZipFile(f, "w") as z:
        if mimetype is not None:
            z.writestr("mimetype", mimetype)
        z.writestr("Contents/section0.xml",
                   f'<?xml version="1.0" encoding="UTF-8"?><hs:sec {_NS}>{section_xml}</hs:sec>')
    return f


# ── ② 경계 규칙 (합성) ──────────────────────────────────────────────
def test_문단과_표셀_경계가_줄바꿈이다(tmp_path):
    # 실물 구조: 표(hp:tbl)는 hp:t 가 아니라 hp:run 의 직접 자식이다
    표 = ("<hp:tbl><hp:tr>" + _cell("총 사업비") + _cell("정부지원", "자기부담") + "</hp:tr>"
          "<hp:tr>" + _cell("100%") + _cell("70%") + "</hp:tr></hp:tbl>")
    xml = (
        _p("제1조(목적) 이 기준은")
        + "<hp:p><hp:run><hp:t>앞 문단</hp:t></hp:run>"
          f"<hp:run>{표}</hp:run>"
          "<hp:run><hp:t>뒤 문단</hp:t></hp:run></hp:p>"
        + _p("줄1<hp:lineBreak/>줄2<hp:tab/>탭뒤<hp:fwSpace/>공백뒤")
    )
    text = hx.extract(_hwpx(tmp_path, xml))
    assert text.split("\n") == [
        "제1조(목적) 이 기준은",
        "앞 문단",
        "총 사업비",
        "정부지원", "자기부담",          # 셀 안 문단 둘 → 줄 둘
        "100%", "70%",
        "뒤 문단",
        "줄1",
        "줄2\t탭뒤 공백뒤",
    ]


def test_빈문단은_줄을_만들지_않는다(tmp_path):
    text = hx.extract(_hwpx(tmp_path, _p("") + _p("A") + "<hp:p><hp:run/></hp:p>" + _p("B")))
    assert text == "A\nB"


def test_섹션은_번호순으로_이어붙는다(tmp_path):
    f = tmp_path / "다중.hwpx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("mimetype", hx.HWPX_MIMETYPE)
        for i, body in ((10, "열째"), (0, "첫째"), (2, "셋째")):
            z.writestr(f"Contents/section{i}.xml", f'<hs:sec {_NS}>{_p(body)}</hs:sec>')
    assert hx.extract(f) == "첫째\n셋째\n열째"


# ── ③ 확장자 위장 방어 ──────────────────────────────────────────────
def _fake_zip(tmp_path: Path, name: str, entries: dict[str, str]) -> Path:
    f = tmp_path / name
    with zipfile.ZipFile(f, "w") as z:
        for k, v in entries.items():
            z.writestr(k, v)
    return f


@pytest.mark.parametrize("entries, 정체", [
    ({"[Content_Types].xml": "<x/>", "xl/workbook.xml": "<x/>", "xl/sharedStrings.xml": "<x/>"}, "XLSX"),
    ({"[Content_Types].xml": "<x/>", "word/document.xml": "<x/>"}, "DOCX"),
    ({"[Content_Types].xml": "<x/>", "ppt/presentation.xml": "<x/>"}, "PPTX"),
    ({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": "<x/>"}, "ODF"),
    ({"README.txt": "hi"}, "hwpx 아님"),
])
def test_위장_zip은_정체를_밝히며_실패한다(tmp_path, entries, 정체):
    f = _fake_zip(tmp_path, "위장.hwpx", entries)
    with pytest.raises(hx.NotHwpxError) as ei:
        hx.extract(f)
    assert 정체 in str(ei.value) and "위장.hwpx" in str(ei.value)


def test_zip_아닌_hwpx는_매직바이트로_정체를_밝힌다(tmp_path):
    for head, 정체 in ((b"\xd0\xcf\x11\xe0" + b"\0" * 32, "OLE2"),
                       (b"%PDF-1.4\n", "PDF"),
                       (b"<?xml version='1.0'?><HWPML/>", "XML"),
                       (b"\x00\x01garbage", "정체 불명")):
        f = tmp_path / "가짜.hwpx"
        f.write_bytes(head)
        with pytest.raises(hx.NotHwpxError, match=정체):
            hx.extract(f)


def test_mimetype_없어도_section_xml이_있으면_hwpx다(tmp_path):
    assert hx.extract(_hwpx(tmp_path, _p("본문"), mimetype=None)) == "본문"


def test_hwpx_인데_section이_없으면_실패한다(tmp_path):
    f = _fake_zip(tmp_path, "빈.hwpx", {"mimetype": hx.HWPX_MIMETYPE, "Contents/header.xml": "<x/>"})
    with pytest.raises(hx.NotHwpxError, match="section"):
        hx.extract(f)


# ── ④ stage0 디스패처 — 확장자가 아니라 내용물 ─────────────────────────
def test_stage0는_hwp_확장자여도_zip이면_hwpx로_보낸다(tmp_path):
    f = _hwpx(tmp_path, _p("제1조(목적) 내용"), name="확장자만_hwp.hwp")
    kind, (text, offs) = s0.extract(f)
    assert kind == "text" and text == "제1조(목적) 내용" and offs == {}


def test_stage0는_위장_xlsx를_조용히_삼키지_않는다(tmp_path):
    f = _fake_zip(tmp_path, "위장.hwpx", {"xl/workbook.xml": "<x/>"})
    with pytest.raises(hx.NotHwpxError, match="XLSX"):
        s0.extract(f)


# ── ① 실물 픽스처 ───────────────────────────────────────────────────
@pytest.mark.skipif(not 실물_HWPX.exists(), reason="실물 hwpx 픽스처 없음")
def test_실물_hwpx가_stage0_경로에서_조문을_낸다():
    import re
    kind, (text, _) = s0.extract(실물_HWPX)
    assert kind == "text"
    assert "제1조(목적)" in text
    assert len(re.findall(r"^제\d+조\(", text, re.M)) >= 20      # 실측 31개(2026-09-02)
    # 표 (표-1) 의 셀이 각각 한 줄로 살아 있다
    lines = text.split("\n")
    i = lines.index("총 사업비")
    assert lines[i:i + 3] == ["총 사업비", "정부지원사업비", "창업기업등 자기부담사업비"]


@pytest.mark.skipif(not 실물_위장XLSX.exists(), reason="실물 위장 XLSX 픽스처 없음")
def test_실물_TIPS_hwpx는_XLSX라고_밝히며_실패한다():
    assert hx.sniff(실물_위장XLSX).startswith("XLSX")
    with pytest.raises(hx.NotHwpxError, match=r"XLSX \(xl/workbook\.xml\)"):
        s0.extract(실물_위장XLSX)
