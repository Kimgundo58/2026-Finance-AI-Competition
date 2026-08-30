# -*- coding: utf-8 -*-
"""Stage 0-T : 부속표 추출 -> `_tables.json`.

**텍스트 추출과 분리된 경로다.** 같은 PDF 를 두 번 읽는다.
비목 카탈로그 · 증빙 매핑 · 한도가 전부 본문이 아니라 뒤쪽 부속표(`[참고N]` / `[붙임N]`)에
있는데, 텍스트로만 뽑으면 셀 경계가 사라져 행이 뒤섞인다. 실측: 예비창업 붙임2 를 raw text
로 읽으면 "비목 / 정의 / 증빙 / 유의사항" 4열이 한 줄로 붙어 어느 값이 어느 비목인지
복원할 수 없다.

산출은 Stage 1(룰 컴파일)의 입력이다. Stage 1 보다 먼저 서야 한다.

실행:
    python scripts/extract_tables.py                전체 (L2)
    python scripts/extract_tables.py --doc 예비창업   일부
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pdfplumber  # noqa: E402
from stage0_run import 대상수집  # noqa: E402  라우팅을 한 군데서만 정의한다
from stage0_articles import RE_ATTACH as RE_SEC  # noqa: E402

OUT = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

# 섹션 헤더 정규식은 Stage 0 것을 그대로 쓴다. 따로 쓰면 두 곳이 어긋난다 —
# 실측으로 겪었다: 여기에 줄머리 앵커 없는 사본을 두었더니 본문 속 참조
# (`[붙임 2]에서 정하는 바에 따른다`)를 헤더로 잡아 예비창업 p2~4 의 본문 표가
# 붙임1/붙임2 로 잘못 태깅됐다. 실제 붙임은 p6 부터다.
_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)}

# pdfplumber 기본 전략은 괘선(lines)이다. 창진원 부속표는 괘선이 있어 이걸로 잡힌다.
# 괘선 없는 표까지 text 전략으로 긁으면 본문 단락이 표로 잡혀 오탐이 폭증한다 —
# 실측에서 한 문서에 표 300개가 나왔다. lines 로 고정한다.
TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "intersection_tolerance": 5,
}


def _label(kind: str, raw: str, seq: int) -> str:
    s = "".join(_CIRCLED.get(c, c) for c in raw).strip()
    d = re.search(r"\d+", s)
    if d:
        return f"{kind}{d.group(0)}"
    # 번호가 없으면 문서 안 등장 순서를 붙인다. 페이지 번호를 쓰면
    # `붙임59` 같은 라벨이 나온다 (TIPS 실측) — 59쪽에 있다는 뜻이지 붙임 59가 아니다.
    return f"{kind}{s}" if s else f"{kind}#{seq}"


# 줄 어디에 있든 찾되, **앞 문맥**으로 헤더와 본문 참조를 가른다.
# 줄머리 앵커만으로는 안 된다 — 2단 조판 문서는 좌우 컬럼이 한 줄로 읽혀
# 헤더 앞에 다른 컬럼 텍스트가 붙는다.
#   실측(창업중심대학 p7): `부칙 < 2022. 8. 22. > [참고1] 창업중심대학(주관기관) 사업비 …`
#   -> 줄머리 앵커로는 참고1 이 통째로 사라졌다.
RE_SEC_LINE = re.compile(
    r"[\[【<]\s*(붙임|별표|별지|서식|참고|별첨)\s*([^\]】>\n]{0,12}?)\s*[\]】>]"
    r"[ \t]*(?![,、。·;:])")

# 헤더 앞에 올 수 있는 것: 아무것도 없거나, 조판 잔재(부칙 날짜 `>`, 쪽번호 `- 15 -`).
# 본문 참조 앞에는 한글 어절이나 인용부호·쉼표가 온다.
#   `…세부사항은 ‘[붙임 1] 주관기관 …해설표’에서 정하는 바에`   -> 인용부호
#   `집행기준을 따르고, [참고2] 창업중심대학 사업비 집행 증빙서류` -> 쉼표
#   `따라 [참고4] 사업비 집행 증빙서류 외에 추가로`              -> 한글
RE_HEAD_OK = re.compile(r"[>)\]\d.\-–—]$")


def _is_header_context(line: str, start: int) -> bool:
    prefix = line[:start].rstrip()
    return not prefix or bool(RE_HEAD_OK.search(prefix))


# 컬럼 점프로 볼 가로 간격(pt). 단어 사이 공백은 2~8pt, 단 사이는 30pt 이상이다.
COLUMN_GAP = 20.0


def _headers_on_page(page, seq_start: int) -> list[tuple[float, str]]:
    """(y좌표, 라벨) 목록. 한 쪽에 섹션이 둘 이상 시작할 수 있다.

    페이지 단위로 첫 매치만 잡으면 그 뒤 섹션이 통째로 앞 섹션에 흡수된다 —
    실측: 초기창업 붙임1, 창업중심대학 참고1, 초격차 참고1 이 이 방식으로 사라졌다.

    판별은 **단어 x좌표**로 한다. 문자열 앞 문맥만 보면 2단 조판에서 좌우 컬럼이
    한 줄로 읽히는 경우를 못 가른다. 실측 2건:
        초기창업 p6  `…지침 제65조(권리 의무 이전)에 【붙임 1】주관기관 사업비 비목 해설표`
        초격차  p5  `[참고4] 주관기관 사업비 보조세목 정의 [참고5] 초격차 창업기업 …`
    둘 다 헤더 앞이 한글이라 문맥 규칙이 막았는데, 실제로는 **다른 컬럼의 첫 단어**다.
    앞 단어와의 가로 간격이 컬럼 폭만큼 벌어지면 줄머리로 본다.
    """
    words = page.extract_words(keep_blank_chars=False)
    lines: dict[int, list[dict]] = {}
    for w in words:
        lines.setdefault(round(w["top"] / 3.0), []).append(w)

    out: list[tuple[float, str]] = []
    for key in sorted(lines):
        ws = sorted(lines[key], key=lambda w: w["x0"])
        for i, w in enumerate(ws):
            if not w["text"][:1] in "[【<":
                continue                       # 인용부호가 붙은 `‘[붙임` 은 여기서 걸린다
            tail = " ".join(x["text"] for x in ws[i:i + 5])
            m = RE_SEC_LINE.match(tail)
            if not m:
                continue
            if i and (w["x0"] - ws[i - 1]["x1"]) < COLUMN_GAP:
                continue                       # 같은 컬럼 안에서 이어 쓴 본문 참조
            out.append((w["top"], _label(m.group(1), m.group(2) or "",
                                         seq_start + len(out))))
    return sorted(out)


def _clean_cell(c) -> str:
    if c is None:
        return ""
    # 셀 안 줄바꿈은 값의 일부가 아니라 조판이다. 공백으로 눕힌다.
    return re.sub(r"\s+", " ", str(c)).strip()


def _clean_table(rows) -> list[list[str]]:
    out = []
    for r in rows or []:
        cells = [_clean_cell(c) for c in r]
        if any(cells):
            out.append(cells)
    return out


def _is_meaningful(rows: list[list[str]]) -> bool:
    """머리글만 있거나 1열짜리는 표가 아니다."""
    if len(rows) < 2:
        return False
    ncol = max(len(r) for r in rows)
    if ncol < 2:
        return False
    filled = sum(1 for r in rows for c in r if c)
    return filled >= 4


def _merge_continuation(tables: list[dict]) -> list[dict]:
    """페이지를 넘어 이어지는 표를 잇는다.

    부속표는 대부분 여러 쪽에 걸친다. 쪽마다 끊어 두면 "비목 하나 = 행 하나" 가 깨져
    Stage 1 이 비목과 값을 짝지을 수 없다.
    이음 조건: 바로 다음 쪽 / 열 수 동일 / 그 쪽에서 새 섹션 헤더가 시작되지 않았다.
    """
    merged: list[dict] = []
    for t in tables:
        if merged:
            prev = merged[-1]
            same_doc = prev["doc_id"] == t["doc_id"]
            next_page = t["페이지"] == prev["페이지"] + 1
            same_cols = prev["열"] == t["열"]
            same_sec = prev["섹션"] == t["섹션"]
            if same_doc and next_page and same_cols and same_sec and not t["섹션시작"]:
                prev["행"].extend(t["행"])
                prev["페이지_끝"] = t["페이지"]
                prev["이어붙임"] = prev.get("이어붙임", 1) + 1
                continue
        merged.append(t)
    return merged


# 표 추출 대상은 **룰 소스가 되는 문서**뿐이다.
#   실측으로 배웠다: 대상을 L2 전체(38건)로 두었더니
#     TIPS 별지서식 -> 표 469개   (양식이라 입력칸 격자가 전부 표로 잡힌다)
#     모집공고       -> 표 31개    (선정 절차·일정표. 비목 규정이 아니다)
#   표가 많은 게 문제가 아니라 **룰이 아닌 것이 룰 소스에 섞이는 것**이 문제다.
포함_키워드 = ("세부관리기준", "관리기준", "운영지침", "통합관리지침")
제외_키워드 = ("별지서식", "모집공고", "공고문", "현황", "서식", "신청서")


def is_rule_source(doc_id: str) -> bool:
    if any(k in doc_id for k in 제외_키워드):
        return False
    return any(k in doc_id for k in 포함_키워드)


def run(doc_filter: str | None, all_docs: bool) -> None:
    targets, _ = 대상수집("L2")
    targets = [t for t in targets if t["path"].suffix.lower() == ".pdf"]
    if not all_docs:
        targets = [t for t in targets if is_rule_source(t["doc_id"])]
    if doc_filter:
        targets = [t for t in targets if doc_filter in t["doc_id"]]
    print(f"대상 {len(targets)}건\n")

    all_tables: list[dict] = []
    per_doc = Counter()
    fails: list[dict] = []

    for i, t in enumerate(targets, 1):
        p: Path = t["path"]
        doc_id = t["doc_id"]
        cur_sec = None
        seq = 0
        try:
            with pdfplumber.open(p) as pdf:
                for pno, page in enumerate(pdf.pages):
                    heads = _headers_on_page(page, seq)
                    seq += len(heads)
                    # 표는 좌표로 찾아야 헤더와 상하를 비교할 수 있다.
                    # find_tables() 가 bbox 를, .extract() 가 셀 값을 준다.
                    for found in page.find_tables(TABLE_SETTINGS):
                        rows = _clean_table(found.extract())
                        if not _is_meaningful(rows):
                            continue
                        top = found.bbox[1]
                        # 이 표 **위에** 있는 가장 가까운 헤더가 이 표의 섹션이다.
                        above = [h for h in heads if h[0] <= top]
                        started = False
                        if above:
                            cur_sec = above[-1][1]
                            started = True
                        all_tables.append({
                            "doc_id": doc_id,
                            "섹션": cur_sec,
                            "섹션시작": started,
                            "페이지": pno,
                            "페이지_끝": pno,
                            "열": max(len(r) for r in rows),
                            "행수": len(rows),
                            "행": rows,
                        })
                    # 표 아래에서 시작한 섹션은 다음 쪽으로 넘긴다
                    if heads:
                        cur_sec = heads[-1][1]
        except Exception as e:                            # noqa: BLE001
            fails.append({"doc_id": doc_id, "오류": f"{type(e).__name__}: {e}"[:160]})
            print(f"[{i:>2}/{len(targets)}] !! {doc_id[:50]}  {type(e).__name__}")
            continue
        n = sum(1 for x in all_tables if x["doc_id"] == doc_id)
        per_doc[doc_id] = n
        print(f"[{i:>2}/{len(targets)}] {doc_id[:48]:<50} 표 {n}")

    merged = _merge_continuation(all_tables)
    doc = {
        "생성": "scripts/extract_tables.py",
        "설정": TABLE_SETTINGS,
        "주의": ("괘선(lines) 전략 고정. 괘선 없는 표는 잡히지 않는다. "
                 "다단·4분면 조판 문서는 셀 좌표가 뒤섞여 열 수가 어긋날 수 있다 — "
                 "`열` 분포가 문서 내에서 들쭉날쭉하면 그 문서를 의심할 것."),
        "요약": {
            "문서": len(targets), "표_원본": len(all_tables), "표_병합후": len(merged),
            "실패": len(fails),
            "문서별": dict(per_doc),
        },
        "실패": fails,
        "tables": merged,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"표 {len(all_tables)} -> 병합 후 {len(merged)}  ·  실패 {len(fails)}")
    print(f"-> {OUT.relative_to(ROOT)}  ({OUT.stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc")
    ap.add_argument("--all", action="store_true",
                    help="룰 소스 필터를 끄고 L2 전체를 훑는다 (진단용)")
    a = ap.parse_args()
    run(a.doc, a.all)
