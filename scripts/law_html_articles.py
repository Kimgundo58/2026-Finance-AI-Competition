#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""국가법령정보센터 본문 HTML(lsInfoR.do · admRulInfoR.do) -> 복원 조문 JSON (docs/5_룰/5-7c_수집과_현행판_확인.md).

OPEN API(DRF)는 신청자 이메일 ID(OC)를 보내야 해서, 현행 판 정렬(P0-7)은 공개 본문 페이지를 원본으로 삼는다.
결과 JSON 은 scripts/corpus_restored_load.py 의 입력이다. 형식은 text_extract.extract_xml 과 같다:
조번호 "제N조" · "제N조의M", 조제목, 본문(줄 나눔은 항 · 호 · 목). 부칙은 넣지 않는다(XML 경로와 같다).
별표는 --옛판-별표 로 넘긴 옛 판 본문과 개정 표기가 같으면 옛 본문을 쓰고, 다르면 --별표-글 파일의 글을 쓴다.

실행: PYTHONIOENCODING=utf-8 python scripts/law_html_articles.py --html <본문.html> --out <조문.json> [--대조 <옛 doc_id>]
받은 파일은 데이터로만 읽는다.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

RE_블록 = re.compile(r'<a name="[^"]*"[^>]*></a>\s*<div class="pgroup"[^>]*>(.*?)(?=<a name="[^"]*"[^>]*></a>\s*<div class="pgroup"|<div id="arDivArea"|<a name="byl"|\Z)', re.S)
RE_문단 = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
RE_태그 = re.compile(r"<[^>]+>")
# 표 · 수식은 그림이고 글은 대체 글(alt)에 있다. 누름 단추 그림(/images/)은 뺀다. 대체 글이 없으면 빠졌음을 남긴다.
RE_그림 = re.compile(r'<img\b(?![^>]*(?:/images/|btn_))[^>]*>', re.S)
RE_대체글 = re.compile(r'alt="([^"]*)"', re.S)
RE_조머리 = re.compile(r"^제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*(?:\(((?:[^()]|\([^()]*\))*)\))?")
RE_별표 = re.compile(r"\[(별표|별지|서식)\s*(\d+)(?:의\s*(\d+))?\]\s*([^<\n]*)")
RE_날짜표기 = re.compile(r"<([가-힣]*)\s*(\d{4}\.[\d.,\s]*?)\s*>")
# 페이지에만 있는 조 끝 안내 줄. XML 조문에는 없다(조문참고자료 칸). [시행일: …] 은 효력 정보라 남긴다.
RE_안내줄 = re.compile(r"^(?:\[(?:전문개정|본조신설|제목개정|조제목개정|종전|제\d+조)[^\]]*\]\s*)+$")
항기호 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _그림글(m: re.Match) -> str:
    대체 = RE_대체글.search(m.group(0))
    글 = 대체.group(1) if 대체 else ""
    if not 글.strip() or re.fullmatch(r"img\d+", 글.strip()):
        return " [그림] "
    글 = _수식글(re.sub(r"@@/?LATEX@@", "", html.unescape(글))).strip()
    return "\n" + html.escape(글, quote=False) + "\n"     # _글 이 태그를 지운 뒤 다시 푼다. 수식의 < 가 태그로 지워지지 않게 한다


def _수식글(t: str) -> str:
    """대체 글의 LaTeX 를 사람이 읽는 글로 바꾼다. 인용 원문이 화면에 그대로 나간다. 옛 XML 도 수식을 글로 적었다."""
    t = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", t)
    while True:                                 # 안쪽 분수부터
        n = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1) / (\2)", t)
        if n == t:
            break
        t = n
    t = re.sub(r"root\\\{\s*([^{}\\]*?)\s*\\\}\s*of\\\{\s*([^{}\\]*?)\s*\\\}", r"(\2)의 \1제곱근", t)
    for 옛, 새 in ((r"\times", "×"), (r"\div", "÷"), (r"\left", ""), (r"\right", ""), (r"\ldotp", "."),
                  (r"\{", "{"), (r"\}", "}")):
        t = t.replace(옛, 새)
    return t


def _글(조각: str) -> str:
    """HTML 문단 -> XML 경로와 같은 표기. 둥근 따옴표와 개정 날짜 공백은 페이지 표기라 XML 표기로 바꾼다."""
    조각 = RE_그림.sub(_그림글, 조각)
    t = re.sub(r"[ \t\u00a0]+", " ", html.unescape(RE_태그.sub("", 조각))).strip()
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    return RE_날짜표기.sub(lambda m: "<" + (m.group(1) + " " if m.group(1) else "") + ",".join(
        re.sub(r"\.\s*$", "", re.sub(r"\.\s+", ".", d.strip())) for d in m.group(2).split(",")) + ">", t)


def 조문_읽기(본문html: str) -> tuple[list[dict], int]:
    """(조 목록, 페이지의 조 체크박스 수). 체크박스 수는 조 수 대조에 쓴다(검증 ④)."""
    끝 = 본문html.find('<div id="arDivArea"')
    구역 = 본문html[:끝] if 끝 > 0 else 본문html
    체크 = len(re.findall(r'name="joNoList"', 구역))
    out: list[dict] = []
    for m in RE_블록.finditer(구역):
        블록 = m.group(1)
        if 'name="joNoList"' not in 블록:       # 장 · 절 제목
            continue
        i = 블록.find('class="lawcon"')
        줄 = [t for t in (_글(p) for p in RE_문단.findall(블록[i:])) if t and not RE_안내줄.match(t)]
        if not 줄:
            continue
        머리 = RE_조머리.match(줄[0])
        if not 머리:
            raise ValueError(f"조 머리를 읽지 못했다: {줄[0][:60]}")
        번호, 가지, 제목 = 머리.group(1), 머리.group(2), (머리.group(3) or "").strip()
        # 제목 바로 뒤에 ① 이 오면 XML 처럼 줄을 나눈다
        첫줄 = 줄[0]
        뒤 = 첫줄[머리.end():].lstrip()
        if 뒤[:1] in 항기호:
            줄[0:1] = [첫줄[:머리.end()].strip(), 뒤]
        out.append({"조번호": f"제{번호}조" + (f"의{가지}" if 가지 else ""), "조제목": 제목,
                    "본문": "\n".join(줄), "페이지": None})
    return out, 체크


def 별표_목록(본문html: str) -> list[dict]:
    """문서 끝 별표 · 서식 목록: 종류, 번호, 제목, 첨부 flSeq(hwp · pdf)."""
    i = 본문html.find('<a name="byl"')
    if i < 0:
        return []
    out, 본 = [], set()
    for m in re.finditer(r'<a name="J\d+"[^>]*></a>(.*?)(?=<a name="J\d+"|\Z)', 본문html[i:], re.S):
        조각 = m.group(1)
        제 = RE_별표.search(html.unescape(RE_태그.sub(" ", 조각)))
        if not 제:
            continue
        키 = (제.group(1), 제.group(2), 제.group(3))
        if 키 in 본:
            continue
        본.add(키)
        첨부 = {}
        for 링크 in re.finditer(r'href="flDownload\.do\?([^"]+)"[^>]*>\s*<img[^>]*alt="(HWP|PDF)[^"]*"', 조각):
            첨부.setdefault(링크.group(2).lower(), html.unescape(링크.group(1)))
        out.append({"종류": 제.group(1), "번호": 제.group(2), "가지": 제.group(3),
                    "제목": 제.group(4).strip(), "첨부": 첨부})
    return out


def 대조(조문: list[dict], 옛: dict[str, tuple[str, str]]) -> dict:
    """조번호별 공백 제거 비교. 옛: {조번호: (조제목, 본문)}."""
    def 키(s: str) -> str:
        return re.sub(r"\s+", "", s or "")
    새 = {a["조번호"]: a for a in 조문}
    같음, 다름 = [], []
    for 번호, a in 새.items():
        if 번호 in 옛:
            (같음 if 키(a["본문"]) == 키(옛[번호][1]) else 다름).append(번호)
    return {"같음": 같음, "다름": 다름, "새 판에만": [n for n in 새 if n not in 옛],
            "옛 판에만": [n for n in 옛 if n not in 새 and not n.startswith(("별표", "별지", "서식"))]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--대조", help="옛 판 doc_id (DB 읽기)")
    a = ap.parse_args()
    s = Path(a.html).read_text(encoding="utf-8")
    조문, 체크 = 조문_읽기(s)
    if len(조문) != 체크:
        print(f"경고: 조 체크박스 {체크}개, 읽은 조 {len(조문)}개")
    out = {"메타": {"원본": Path(a.html).name, "머리": (re.search(r"\[시행[^\]]+\]\s*\[[^\]]+\]", s) or [""])[0]},
           "조문": 조문}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"조 {len(조문)} · 체크박스 {체크} · 별표 {len(별표_목록(s))}")
    if a.대조:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _lib import db
        with db.connect(connect_timeout=5) as c:
            옛 = {r[0]: (r[1], r[2]) for r in c.execute(
                "SELECT 조번호, 조제목, 본문 FROM corpus.doc_articles WHERE doc_id = %s", (a.대조,)).fetchall()}
        결과 = 대조(조문, 옛)
        print(json.dumps({k: (len(v) if k == "같음" else v) for k, v in 결과.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
