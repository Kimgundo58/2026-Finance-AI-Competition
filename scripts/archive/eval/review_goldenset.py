# -*- coding: utf-8 -*-
"""정답셋 기계 검수 — `_골든셋_검수.json`.

`LLM.md` §5 의 정답셋은 전량 `verified=false` 다. 사람 검수 전에 **기계가 확정할 수 있는
것부터 걷어낸다.** 사람이 봐야 하는 것과 기계가 이미 아는 것을 섞어두면 검수가 안 끝난다.

기계가 확정하는 것 (사람 판단 불필요):
    C1  `정답_근거[].doc` 이 규정 모음에 실재하는가
    C2  조번호가 그 문서에 실재하는가
    C3  🔴 `근거_원문` 이 규정 모음 원문에 실재하는가  ← 정답지 오염의 유일한 기계 검출 지점
    C4  스키마 — 판정/대상 enum, 필수 필드
    C5  근거 결손 — 조번호·근거_원문 null

기계가 못 하는 것 (사람 몫으로 남긴다):
    원문이 그 판정을 실제로 지지하는가 (포섭). `rule_base.md` §6 —
    "LLM 에게 검수를 맡기지 않는다. 룰을 뽑은 것과 같은 종류의 실수를 반복한다."

C3 는 조번호를 신뢰하지 않는다. **문서 전문에서 원문을 찾아 실제 조번호를 역산**하고
정답셋이 적어둔 조번호와 대조한다 — 조번호 오기와 원문 날조가 한 번에 잡힌다.

실행:
    python scripts/archive/eval/review_goldenset.py
    python scripts/archive/eval/review_goldenset.py --brief    콘솔 요약만 (파일 안 씀)
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
DATA = ROOT / "2026_Finance_DATA_FOR_RAG"
ART = DATA / "_stage0_articles.json"
SETS = [
    ("본세트", DATA / "_골든셋" / "_골든셋_초안.json"),
    ("적대적", DATA / "_골든셋_스테이징" / "적대적세트_초안.json"),
]
OUT = DATA / "_골든셋_스테이징" / "_골든셋_검수.json"

판정_ENUM = {"가능", "조건부", "불가", "판단불가"}
대상_ENUM = {"창업기업", "주관기관", "공통"}

# 정답셋 `doc` 표기 -> 규정 모음 doc_id. 표기 흔들림은 데이터가 아니라 오기다.
DOC_ALIAS = {
    "모두의창업 세부관리기준": "모두의 창업 프로젝트 세부관리기준(개정본)",
}


def 정규(s: str) -> str:
    """비교용 정규화. 조판·인용 부호 차이는 불일치가 아니다."""
    if not s:
        return ""
    s = re.sub(r"\s+", "", s)
    for ch in "“”\"'‘’＂":
        s = s.replace(ch, "")
    s = s.replace("·", "").replace("‧", "").replace("․", "")
    s = s.replace("（", "(").replace("）", ")")
    return s


def 조_of(조번호: str | None) -> str | None:
    """`제22조②3호` · `제22조(사업비 집행) 제1호` -> `제22조`. 조가 아니면 None."""
    if not 조번호:
        return None
    m = re.search(r"제\s*(\d+)\s*조", 조번호)
    return f"제{m.group(1)}조" if m else None


def 단편들(원문: str) -> list[str]:
    """정답셋은 `…` 로 중간을 생략하고 `/` 로 두 조항을 잇는다. 각각을 따로 찾는다."""
    parts = re.split(r"…+|\.{3,}|\s/\s", 원문)
    return [p for p in (정규(x) for x in parts) if len(p) >= 8]


# 페이지 꼬리말이 문장 한가운데 박혀 있다 (`예외적- 17 -으로 구매가능하다`).
# 연속 매칭이 깨지므로 **창 커버리지**로 잰다 — 날조와 조판 파손을 가른다.
창 = 16
보폭 = 4


def 커버리지(단편: str, 본문: str) -> float:
    if len(단편) <= 창:
        return 1.0 if 단편 in 본문 else 0.0
    ws = [단편[i:i + 창] for i in range(0, len(단편) - 창 + 1, 보폭)]
    return sum(1 for w in ws if w in 본문) / len(ws)


def 문서찾기(arts: dict, name: str) -> tuple[str | None, str]:
    if not name:
        return None, "doc 없음"
    if name in arts:
        return name, "정확"
    alias = DOC_ALIAS.get(name)
    if alias and alias in arts:
        return alias, f"별칭 -> {alias}"
    key = 정규(name)
    for k in arts:
        if 정규(k) == key:
            return k, f"정규화 일치 -> {k}"
    for k in arts:
        if key in 정규(k) or 정규(k) in key:
            return k, f"부분 일치 -> {k}"
    return None, "코퍼스에 없음"


def 원문검증(doc: dict, 표: list, 원문: str) -> dict:
    """문서 전문(조문 + 표)에서 원문을 찾는다. 조번호를 신뢰하지 않고 역산한다.

    `근거_원문` 이 붙임·별표 표에만 있는 경우가 있다 — 그건 정답셋 결함이 아니라
    **표가 아직 조문으로 안 들어온 것**이다 (`RAG.md` §5 미결 #3). 둘을 갈라서 보고한다.
    """
    frs = 단편들(원문)
    if not frs:
        return {"상태": "비교불가", "사유": "원문이 짧거나 없음"}
    조본문 = [(a["조번호"], 정규(a.get("본문") or "")) for a in doc["articles"]]
    표본문 = [(f"표:{s}", b) for s, b in 표]
    전체 = " ".join(b for _, b in 조본문)
    표전체 = " ".join(b for _, b in 표본문)

    결과 = []
    for f in frs:
        c조, c표 = 커버리지(f, 전체), 커버리지(f, 표전체)
        위치 = sorted({조 for 조, b in 조본문 if 커버리지(f, b) >= 0.9})
        결과.append({"단편": f[:50], "조문커버": round(c조, 2),
                     "표커버": round(c표, 2), "조": 위치})

    최저 = min(max(r["조문커버"], r["표커버"]) for r in 결과)
    조집합 = sorted({c for r in 결과 for c in r["조"]})
    표만 = all(r["표커버"] > r["조문커버"] for r in 결과)

    if 최저 >= 0.95:
        상태 = "일치(표)" if 표만 else "일치"
    elif 최저 >= 0.6:
        상태 = "조판파손" if not 표만 else "일치(표·조판파손)"
    else:
        상태 = "🔴 미발견"
    return {"상태": 상태, "최저커버": round(최저, 2), "실제_조": 조집합, "단편별": 결과}


def 검수1(item: dict, arts: dict, tbls: dict, 세트: str) -> dict:
    no = item.get("no")
    r = {"세트": 세트, "no": no, "질문": (item.get("질문") or "")[:44],
         "판정": item.get("정답_판정"), "대상": item.get("대상"),
         "평가범위": item.get("평가범위"), "경고": [], "등급": None}

    # C4 스키마
    if item.get("정답_판정") not in 판정_ENUM:
        r["경고"].append(f"C4 판정 enum 위반: {item.get('정답_판정')!r}")
    if item.get("대상") not in 대상_ENUM:
        r["경고"].append(f"C4 대상 enum 위반: {item.get('대상')!r}")
    if not (item.get("질문") or "").strip():
        r["경고"].append("C4 질문 비어 있음")

    근거 = item.get("정답_근거") or []
    원문 = item.get("근거_원문")

    # C5 근거 결손
    if not 근거:
        r["경고"].append("C5 정답_근거 배열이 비었다")
    조없음 = [g for g in 근거 if not g.get("조번호")]
    if 조없음:
        r["경고"].append(f"C5 조번호 null ({len(조없음)}/{len(근거)}건)")
    if not 원문:
        r["경고"].append("C5 근거_원문 null")

    r["근거검증"] = []
    for g in 근거:
        doc_id, how = 문서찾기(arts, g.get("doc"))
        one = {"doc": g.get("doc"), "조번호": g.get("조번호"), "해소": how}
        if doc_id is None:
            r["경고"].append(f"C1 문서 미실재: {g.get('doc')!r}")
            r["근거검증"].append(one)
            continue
        if how != "정확":
            r["경고"].append(f"C1 doc 표기 불일치 — {how}")
        doc = arts[doc_id]
        조번호집합 = {a["조번호"] for a in doc["articles"]}
        조 = 조_of(g.get("조번호"))
        if 조:
            one["조_실재"] = 조 in 조번호집합
            if 조 not in 조번호집합:
                r["경고"].append(f"C2 조 미실재: {doc_id} {조}")
        elif g.get("조번호"):
            # 붙임/별지/표 계열 — Stage 0 가 독립 조로 분리했는지 본다
            raw = g["조번호"]
            sec = next((s for s in 조번호집합 if 정규(s) and 정규(s) in 정규(raw)), None)
            one["섹션_실재"] = sec
            if sec is None:
                r["경고"].append(f"C2 조/섹션으로 해소 안 됨: {raw!r}")
        # 근거가 여러 개면 원문도 근거별로 붙는다 (`apply_goldenset_fixes.py` 가 쪼갠다).
        # 근거별 원문이 있으면 그것을, 없으면 문항 공통 원문을 쓴다.
        이_원문 = g.get("원문") or 원문
        if 이_원문:
            v = 원문검증(doc, tbls.get(doc_id, []), 이_원문)
            one["원문검증"] = v
            s = v["상태"]
            if s == "🔴 미발견":
                r["경고"].append(f"C3 🔴 근거_원문이 코퍼스에 없다 (최고커버 {v['최저커버']})")
            elif s == "조판파손":
                r["경고"].append(f"C6 원문은 있으나 코퍼스가 조판 파손 (커버 {v['최저커버']})")
            elif s.startswith("일치(표"):
                r["경고"].append("C7 원문이 표에만 있다 — 조문 미적재")
            if s.startswith("일치") and 조 and v["실제_조"] and 조 not in v["실제_조"]:
                r["경고"].append(f"C3 조번호 오기: 적힌 {조} / 실제 {v['실제_조']}")
        r["근거검증"].append(one)

    치명 = [w for w in r["경고"]
            if "🔴" in w or w.startswith(("C1", "C2")) or "조번호 오기" in w]
    if not r["경고"]:
        r["등급"] = "A 기계검증 통과"
    elif 치명:
        r["등급"] = "C 근거 결함"
    else:
        r["등급"] = "B 경미"
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--fixed", action="store_true",
                    help="원본 대신 `_골든셋_수정본.json` 을 검수한다 (수정 효과 확인)")
    args = ap.parse_args()

    sets, out = SETS, OUT
    if args.fixed:
        sets = [("수정본", DATA / "_골든셋_스테이징" / "_골든셋_수정본.json")]
        out = DATA / "_골든셋_스테이징" / "_골든셋_검수_수정본.json"

    arts = json.loads(ART.read_text(encoding="utf-8"))
    tbls: dict[str, list] = {}
    tpath = DATA / "_tables.json"
    if tpath.exists():
        for t in json.loads(tpath.read_text(encoding="utf-8")).get("tables", []):
            flat = 정규(" ".join(c for row in t.get("행", []) for c in row))
            tbls.setdefault(t["doc_id"], []).append((t.get("섹션"), flat))

    결과 = []
    for 세트, path in sets:
        if not path.exists():
            print(f"!! 없음: {path}")
            continue
        gs = json.loads(path.read_text(encoding="utf-8"))
        for it in gs["문항"]:
            결과.append(검수1(it, arts, tbls, it.get("_세트") or 세트))

    등급 = {}
    for r in 결과:
        등급[r["등급"]] = 등급.get(r["등급"], 0) + 1
    코드 = {}
    for r in 결과:
        for w in r["경고"]:
            c = w.split()[0]
            코드[c] = 코드.get(c, 0) + 1

    print(f"검수 {len(결과)}문항")
    for k in sorted(등급):
        print(f"  {k:<16} {등급[k]}")
    print("\n경고 코드별:")
    for k in sorted(코드):
        print(f"  {k}  {코드[k]}")

    print("\n등급 C (근거 결함) 전량:")
    for r in 결과:
        if r["등급"] != "C 근거 결함":
            continue
        print(f"  [{r['세트']}/{r['no']}] {r['질문']}")
        for w in r["경고"]:
            print(f"        - {w}")

    if not args.brief:
        out.write_text(json.dumps({
            "생성": "scripts/archive/eval/review_goldenset.py",
            "성격": "기계 검수 결과. verified 를 바꾸지 않는다 — 사람 검수의 입력이다",
            "요약": {"문항": len(결과), "등급별": 등급, "경고코드별": 코드},
            "결과": 결과,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n-> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
