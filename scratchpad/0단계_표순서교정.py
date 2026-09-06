# -*- coding: utf-8 -*-
"""4분면 조판 문서의 표를 «읽는 순서» 로 다시 담는다 — `_tables.json` 병합 (중앙 ai-4a).

발견(2026-09-06 실측). `extract_tables.py` 는 `page.find_tables()` 가 주는 순서 그대로
담는데, 그 순서는 «읽는 순서» 가 아니다.
    초격차 제10차 p3 (595x841, 4분면 조판) find_tables 순서
        [327, 36] 우상   <- 먼저 나온다
        [ 29, 81] 좌상
        [ 29,457] 좌하
        [327,457] 우하
    -> `_tables.json` 의 참고2 는 「외주용역비 꼬리 + 기계장치」가 「재료비 + 외주용역비」
       «앞» 에 온다. 마크다운으로 이어 붙이면 비목 순서가 뒤집힌 표가 된다

`_tables.json` 은 bbox 를 안 남기므로 저장값만으로는 되돌릴 수 없다 — PDF 를 다시 열어
(y분면, x분면, y, x) 로 정렬해 그 문서 항목만 갈아끼운다. 섹션 라벨링·셀 정리·이어붙임은
`extract_tables` 의 함수를 «그대로 import» 해서 쓴다 (두 곳이 어긋나면 라벨이 달라진다).

🔴 검산은 «행수 보존» 이다. 정렬만 바꿨으면 문서별 총 행수가 한 줄도 안 변해야 한다.
   변하면 정렬이 아니라 추출이 달라진 것이므로 중단한다.

실행:  PYTHONIOENCODING=utf-8 python scratchpad/0단계_표순서교정.py           # dry-run
       PYTHONIOENCODING=utf-8 python scratchpad/0단계_표순서교정.py --write
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "archive" / "indexing"))

import pdfplumber                                   # noqa: E402

# 🔴 import 부작용 하나를 막는다 (첫 실행에서 실측).
#    `extract_tables` 와 그가 import 하는 `stage0_run` 이 «둘 다»
#        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, …)
#    를 한다. 두 번째 래퍼가 sys.stdout 을 차지하면 첫 번째 래퍼가 참조를 잃고 GC 되며
#    **진짜 stdout 의 버퍼를 닫는다** -> 이 파일의 print 가
#    `ValueError: I/O operation on closed file` 로 죽는다.
#    (extract_tables 를 스크립트로 직접 돌릴 때는 안 터지므로 그쪽 버그로는 안 보인다)
#    처방: import 동안 만들어지는 래퍼를 전부 붙잡아 둔다. 붙잡혀 있으면 GC 가 안 돈다.
_래퍼보관: list = []
_원래_TIW = io.TextIOWrapper


class _붙잡는_TIW(_원래_TIW):                             # noqa: N801
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _래퍼보관.append(self)


_진짜_stdout = sys.stdout
io.TextIOWrapper = _붙잡는_TIW
try:
    import extract_tables as ET                      # noqa: E402
finally:
    io.TextIOWrapper = _원래_TIW
    sys.stdout = _진짜_stdout

TABLES = ROOT / "2026_Finance_DATA_FOR_RAG" / "_tables.json"

# 대상 = 이번 판에 갈아끼우는 조를 가진 문서만. 나머지는 손대지 않는다.
문서들 = {
    "초격차 스타트업 프로젝트 세부관리기준(제10차)":
        "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초격차 스타트업 프로젝트/초격차 스타트업 프로젝트 세부관리기준(제10차).pdf",
    "예비창업패키지 세부관리기준(2025년)":
        "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/예비창업패키지/예비창업패키지 세부관리기준(2025년).pdf",
    "초기창업패키지 세부관리기준(2025년)":
        "_hwp변환/2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지/초기창업패키지 세부관리기준(2025년).pdf",
    "모두의 창업 프로젝트 세부관리기준(개정본)":
        "2026_Finance_DATA_FOR_RAG/창진원/모두의 창업 (일반-기술)/모두의 창업 프로젝트 세부관리기준(개정본).pdf",
    "창업중심대학 세부관리기준2025년 개정":
        "2026_Finance_DATA_FOR_RAG/창진원/창업중심대학/창업중심대학 세부관리기준2025년 개정.pdf",
}


def 읽는순서(page, tables):
    """4분면/2단 조판 대응 — (y분면, x분면, y, x). 단분면 문서면 (y, x) 정렬과 같다."""
    hb, wb = page.height / 2.0, page.width / 2.0
    return sorted(tables, key=lambda t: (int(t.bbox[1] // hb), int(t.bbox[0] // wb),
                                         round(t.bbox[1]), round(t.bbox[0])))


def 훑기(doc_id: str, path: Path) -> list[dict]:
    out: list[dict] = []
    cur_sec, seq = None, 0
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages):
            heads = ET._headers_on_page(page, seq)
            seq += len(heads)
            for t in 읽는순서(page, page.find_tables(ET.TABLE_SETTINGS)):
                rows = ET._clean_table(t.extract())
                if not ET._is_meaningful(rows):
                    continue
                above = [h for h in heads if h[0] <= t.bbox[1]]
                started = bool(above)
                if above:
                    cur_sec = above[-1][1]
                out.append({"doc_id": doc_id, "섹션": cur_sec, "섹션시작": started,
                            "페이지": pno, "페이지_끝": pno,
                            "열": max(len(r) for r in rows), "행수": len(rows), "행": rows})
            if heads:
                cur_sec = heads[-1][1]
    return ET._merge_continuation(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    doc = json.loads(TABLES.read_text(encoding="utf-8"))
    새전체: list[dict] = []
    for doc_id, rel in 문서들.items():
        p = ROOT / rel
        if not p.exists():
            print("원본 없음 — 중단: " + rel)
            return 1
        옛 = [t for t in doc["tables"] if t.get("doc_id") == doc_id]
        새 = 훑기(doc_id, p)
        옛행 = sum(t["행수"] for t in 옛)
        새행 = sum(t["행수"] for t in 새)
        순서바뀜 = [t["행"] for t in 옛] != [t["행"] for t in 새]
        print("{:<36} 표 {:3d}->{:3d}  행 {:4d}->{:4d}  순서변경 {}".format(
            doc_id[:34], len(옛), len(새), 옛행, 새행, "예" if 순서바뀜 else "아니오"))
        if 새행 != 옛행:
            print("   행수가 달라졌다 — 정렬만 바꾼 게 아니다. 중단한다")
            return 1
        새전체 += 새

    if not a.write:
        print("dry-run — _tables.json 을 쓰지 않았다.")
        return 0
    doc["tables"] = [t for t in doc["tables"] if t.get("doc_id") not in 문서들] + 새전체
    doc["주의"] = doc.get("주의", "") + (
        " · 2026-09-06 scratchpad/0단계_표순서교정.py 로 5문서를 읽는 순서(4분면)로 재정렬.")
    TABLES.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    print("_tables.json 갱신 — 표 {}".format(len(doc["tables"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
