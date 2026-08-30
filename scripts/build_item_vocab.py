# -*- coding: utf-8 -*-
"""Stage 1-a : 비목 어휘집 -> `_비목_어휘집.json`.

`rule_base.md` §1-b 의 산출물. **canonical enum 의 정본**이다.

(1) 정규화 출력의 `비목후보`, `rules.비목`, `item_alias.비목`, 골든셋의 비목이
같은 폐쇄 목록을 쓰지 않으면 시스템이 닫히지 않는다. `guided_json` enum 의 원천이기도 하다.

2층 구조다.

    기간(基幹)   L1 통합관리지침 제14차의 법정 비목        전 사업 공통
    사업별 확장  L2 세부관리기준 [붙임2] 비목 해설표       사업 스코프 한정

**별칭은 여기 넣지 않는다.** 맥북 -> 기계장치 매핑은 `item_alias` 의 몫이다.
어휘집은 목적지 목록일 뿐이다.

실행:
    python scripts/build_item_vocab.py
    python scripts/build_item_vocab.py --check   공유받은 종합표와 커버리지만 대조
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "2026_Finance_DATA_FOR_RAG"
OUT = DATA / "_비목_어휘집.json"

ART = DATA / "_stage0_articles.json"
TBL = DATA / "_tables.json"
SHARED = ROOT / "공유받은 파일" / "비목명 종합.csv"

# 통합관리지침 제14차. 법정 비목이 실린 조 범위는 rule_base.md §1-b 가 제36~45조로 특정한다.
지침_DOC = "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"

# 사업 폴더명 -> 사업명. Stage 0 doc_id 에서 사업을 역산한다.
사업_키워드 = [
    ("예비창업", "예비창업패키지"), ("초기창업", "초기창업패키지"),
    ("재도전", "재도전성공패키지"), ("창업도약", "창업도약패키지"),
    ("창업중심대학", "창업중심대학"), ("초격차", "초격차 스타트업 프로젝트"),
    ("모두의", "모두의 창업 프로젝트"), ("팁스", "민관공동창업자발굴육성(TIPS)"),
    ("TIPS", "민관공동창업자발굴육성(TIPS)"),
]

# 표기 흔들림을 하나로 모은다. 조판 때문에 셀 안에서 단어가 갈라진다
#   `기계장치 (공구·기 구, 비품, SW 등)` · `외주 용역비` · `창업 활동비`
def norm(name: str) -> str:
    s = re.sub(r"\s+", "", name or "")
    s = re.sub(r"[()（）].*$", "", s)          # 괄호 이하 절단
    s = s.replace("·", "").replace("‧", "").replace("․", "")
    return s


def 사업_of(doc_id: str) -> str | None:
    for k, v in 사업_키워드:
        if k in doc_id:
            return v
    return None


# ── 1층: 통합관리지침 법정 비목 ──────────────────────────────────
def 기간어휘(arts: dict) -> list[dict]:
    doc = arts.get(지침_DOC)
    if not doc:
        print(f"!! 지침 문서를 못 찾았다: {지침_DOC}")
        return []
    out = []
    for a in doc["articles"]:
        n = a.get("조번호_int")
        if n is None or not (36 <= n <= 45):
            continue
        if a.get("삭제"):
            continue
        title = (a.get("조제목") or "").strip()
        if not title:
            continue
        out.append({
            "비목": title, "정규": norm(title), "층": "기간",
            "사업스코프": [],                      # 빈 배열 = 전 사업 공통
            "근거": {"doc_id": 지침_DOC, "조번호": a["조번호"]},
        })
    return out


# ── 2층: L2 부속표의 비목 해설표 ────────────────────────────────
머리글 = {"비목", "구분", "구 분", "세목", "내용", "정의", "증빙 서류", "증빙서류",
          "유의 사항", "유의사항", "항목", "합계", "소계", "비 목", "세 목",
          # 비목이 아니라 표의 마지막 묶음 행. 실측에서 비목으로 새어 나왔다.
          "기타", "기 타", "공통", "공 통", "주요내용", "주요 내용", "비고"}
# `공통(기타)` 처럼 괄호가 붙은 변형까지 잡는다.
RE_비목아님 = re.compile(r"^(기\s*타|공\s*통|주요\s*내용|비\s*고|소\s*계|합\s*계)")


def is_비목표(t: dict) -> bool:
    """이 표가 비목 해설표인가.

    🔴 섹션이 `붙임N`/`참고N` 이라는 것만으로는 안 된다. 같은 섹션에 서식·점검표·현황표가
    섞여 있어서, 1열을 무조건 비목으로 삼으면 `대표자명`·`사업장주소`·`연번` 같은
    **서식 필드명이 비목이 된다**. 실측: 그렇게 뽑았더니 비목이 208개가 나왔다.

    비목 해설표의 서명(署名)은 세 가지가 함께 나타나는 것이다:
      머리글에 `비목` + 셀 어딘가에 `정의` + `증빙`
    실측: 469개 표 중 18개만 이 조건을 만족한다.
    """
    if not t["행"]:
        return False
    head = " ".join(t["행"][0])
    flat = " ".join(c for r in t["행"] for c in r)
    return "비목" in head and "정의" in flat and "증빙" in flat


def 사업확장(tables: dict) -> list[dict]:
    """비목 해설표의 비목 열을 읽는다. 정의/증빙/유의 행은 그 열이 비어 있다."""
    out = []
    for t in tables.get("tables", []):
        if not is_비목표(t):
            continue
        biz = 사업_of(t["doc_id"])
        if not biz:
            continue
        sec = t.get("섹션") or "부속표"
        # 비목 열의 위치. `구분 | 비목 | 세목 | 내용` 처럼 앞에 다른 열이 올 수 있다.
        head = t["행"][0]
        try:
            col = next(i for i, c in enumerate(head) if "비목" in c)
        except StopIteration:
            col = 0
        for r in t["행"][1:]:
            if col >= len(r):
                continue
            cell = (r[col] or "").strip()
            if not cell or cell in 머리글 or RE_비목아님.match(cell):
                continue
            if len(cell) > 30 or len(cell) < 2:
                continue
            out.append({
                "비목": cell, "정규": norm(cell), "층": "사업별",
                "사업스코프": [biz],
                "근거": {"doc_id": t["doc_id"], "조번호": sec},
            })
    return out


def 병합(rows: list[dict]) -> list[dict]:
    """정규명이 같으면 한 항목. 사업스코프는 합집합, 근거는 목록으로 쌓는다."""
    by: dict[str, dict] = {}
    for r in rows:
        k = r["정규"]
        if k not in by:
            by[k] = {"비목": r["비목"], "정규": k, "층": r["층"],
                     "사업스코프": [], "근거": [], "표기변형": set()}
        e = by[k]
        e["표기변형"].add(r["비목"])
        if r["층"] == "기간":
            e["층"] = "기간"                        # 기간이 사업별을 이긴다
            e["비목"] = r["비목"]                   # 법정 명칭을 대표로
        for s in r["사업스코프"]:
            if s not in e["사업스코프"]:
                e["사업스코프"].append(s)
        if r["근거"] not in e["근거"]:
            e["근거"].append(r["근거"])
    out = []
    for e in by.values():
        e["표기변형"] = sorted(e["표기변형"])
        if e["층"] == "기간":
            e["사업스코프"] = []                    # 전 사업 공통
        out.append(e)
    return sorted(out, key=lambda x: (x["층"] != "기간", x["정규"]))


# ── 커버리지 대조 (공유받은 종합표) ─────────────────────────────
def 대조(vocab: list[dict]) -> dict:
    """팀 반입 종합표와 대조해 결손을 드러낸다.

    종합표는 21개 판본을 사람이 대조한 2차 편집물이라 **판정 근거로는 못 쓴다**
    (근거 조번호가 없다). 여기서는 커버리지 체크리스트로만 쓴다 —
    "우리가 원문에서 못 뽑은 비목이 무엇인가" 를 알려준다.
    """
    if not SHARED.exists():
        return {"상태": "공유받은 파일 없음"}
    raw = SHARED.read_bytes()
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:  # noqa: BLE001
            continue
    rows = list(csv.DictReader(io.StringIO(txt)))
    ours = {v["정규"] for v in vocab}
    theirs = {}
    for r in rows:
        nm = (r.get("비목 이름") or "").strip()
        if not nm:
            continue
        # `지급수수료 - 멘토링비` 는 세목이다. 비목 축으로 접는다.
        base = nm.split(" - ")[0].split("-")[0].strip()
        theirs.setdefault(norm(base), set()).add(nm)
    only_theirs = sorted(k for k in theirs if k not in ours)
    only_ours = sorted(k for k in ours if k not in theirs)
    return {
        "종합표_행": len(rows),
        "종합표_비목축": len(theirs),
        "우리_비목": len(ours),
        "우리에게_없음": only_theirs,
        "종합표에_없음": only_ours,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    arts = json.loads(ART.read_text(encoding="utf-8")) if ART.exists() else {}
    tables = json.loads(TBL.read_text(encoding="utf-8")) if TBL.exists() else {}

    base = 기간어휘(arts)
    ext = 사업확장(tables)
    vocab = 병합(base + ext)

    chk = 대조(vocab)
    doc = {
        "생성": "scripts/build_item_vocab.py",
        "사양": "rule_base.md §1-b",
        "주의": ("`층=기간` 은 전 사업 공통(사업스코프 빈 배열). `층=사업별` 은 해당 사업만. "
                 "별칭(맥북->기계장치)은 여기가 아니라 item_alias 의 몫이다."),
        "요약": {
            "총": len(vocab),
            "기간": sum(1 for v in vocab if v["층"] == "기간"),
            "사업별": sum(1 for v in vocab if v["층"] == "사업별"),
        },
        "커버리지_대조": chk,
        "vocab": vocab,
    }
    if not args.check:
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"비목 {len(vocab)}  (기간 {doc['요약']['기간']} / 사업별 {doc['요약']['사업별']})")
    for v in vocab:
        scope = ",".join(v["사업스코프"]) or "전사업"
        print(f"  [{v['층']:<3}] {v['비목'][:26]:<28} {scope[:44]:<46} 근거 {len(v['근거'])}")
    print()
    print("커버리지 대조:", json.dumps({k: v for k, v in chk.items() if k != "우리에게_없음"},
                                      ensure_ascii=False))
    if chk.get("우리에게_없음"):
        print("  원문에서 못 뽑은 비목:", ", ".join(chk["우리에게_없음"][:30]))
    if not args.check:
        print(f"\n-> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
