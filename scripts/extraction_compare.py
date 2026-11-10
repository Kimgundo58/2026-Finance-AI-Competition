#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""두 독립 추출(추출_A.md · 추출_B.md)을 비교 키로 맞춰 비교.md 를 쓴다 (docs/5_룰/5-7b_추출_에이전트_지침.md).

규범 비교 키: (조문 ID, 종류, 정규화 수치[값 · 단위 · 연산 · 분모], 대상 집합). 순번은 쓰지 않는다.
개념 비교 키: (종류, 정식명 정규화). 관계 비교 키: (src, 관계, dst).
출력: 일치 · A만 · B만 · 값이 다른 것. 일치율 = 일치 / (일치 + A만 + B만). 불일치는 전부 검증 에이전트에게 넘긴다.
발췌 검사: 모든 발췌가 정규화한 입력 원문의 부분 문자열인지. 실패는 그 행을 `발췌오류` 로 표시한다.
DB · LLM 을 쓰지 않는다.

실행: PYTHONIOENCODING=utf-8 python scripts/extraction_compare.py <묶음 폴더>
      python scripts/extraction_compare.py --self-test
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ontology_norm import 발췌_정규화, 규범ID_풀이  # noqa: E402

_RE_블록 = re.compile(r"^###\s+(조:\S.*?)\s*$", re.M)
_RE_라벨 = re.compile(r"^(원문|개념|관계|규범|모르는 것|용어 후보|검수)\s*:\s*(.*)$")
_RE_발췌 = re.compile(r'"((?:[^"\\]|\\.)*)"|“([^”]*)”')
_RE_관계 = re.compile(r"^(?P<src>\S.*?)\s+-(?P<관계>[^\s>-]+)->\s+(?P<dst>.+?)(?:\s+\[(?P<연결종류>[^\]]+)\])?(?:\s+근거\s+(?P<근거>.*))?$")


def _블록들(글: str) -> dict[str, dict]:
    """추출표 문서 → {조문 ID: {라벨: [줄…]}}. 라벨 없이 들여 쓴 줄은 앞 라벨의 이어지는 줄이다."""
    자리 = [(m.start(), m.end(), m.group(1).strip()) for m in _RE_블록.finditer(글)]
    out: dict[str, dict] = {}
    for i, (_시작, 끝, 조문) in enumerate(자리):
        본문 = 글[끝:자리[i + 1][0]] if i + 1 < len(자리) else 글[끝:]
        칸: dict[str, list[str]] = {}
        현재 = None
        for 줄 in 본문.splitlines():
            if not 줄.strip():
                continue
            m = _RE_라벨.match(줄.strip()) if not 줄.startswith((" ", "\t")) else None
            if m:
                현재 = m.group(1)
                칸.setdefault(현재, [])
                if m.group(2).strip():
                    칸[현재].append(m.group(2).strip())
            elif 현재:
                칸[현재].append(줄.strip())
        out[조문] = 칸
    return out


def _개념_키(항목: str) -> tuple[str, str] | None:
    """`세목:창업/지급수수료/멘토링비 (기존)` → ('세목', '멘토링비'). 비교는 종류와 정식명으로 한다."""
    s = re.sub(r"\s*\((?:기존|신규)\)\s*$", "", 항목.strip())
    if ":" not in s:
        return None
    종류, 경로 = s.split(":", 1)
    이름 = 경로.split("@", 1)[0].rsplit("/", 1)[-1]
    return 종류.strip(), re.sub(r"\s+", "", 발췌_정규화(이름))


def _노드_키(s: str) -> str:
    """관계 · 대상의 노드를 비교용으로 고른다. `세목:…/멘토링비` 같은 생략 표기와 전체 ID 를 같게 본다."""
    s = re.sub(r"\s*\((?:기존|신규|조문 미특정)\)\s*$", "", s.strip())
    if s.startswith("조:") or ":" not in s:
        return re.sub(r"\s+", "", 발췌_정규화(s))
    k = _개념_키(s)
    return f"{k[0]}:{k[1]}" if k else s


def _발췌들(글: str) -> list[str]:
    return [(a or b) for a, b in _RE_발췌.findall(글) if (a or b).strip()]


def _규범_해석(줄: str, 조문: str) -> dict | None:
    """`규범:… | 대상 … | 값 300000 원 | 위반연산 초과 | 분모 인,일 | 근거 "…"` 를 칸으로 푼다."""
    조각 = [c.strip() for c in 줄.split(" | ")]
    if not 조각 or not 조각[0].startswith("규범:"):
        return None
    칸: dict[str, str] = {}
    for c in 조각[1:]:
        k, _, v = c.partition(" ")
        칸[k] = v.strip()
    try:
        종류 = 규범ID_풀이(조각[0])["종류"]
    except ValueError:
        return None
    대상 = sorted({_노드_키(x) for x in re.split(r"[,·]\s*", 칸.get("대상", "")) if x.strip()})
    값, 단위 = None, None
    if 칸.get("값"):
        m = re.match(r"([\d,.]+)\s*(\S*)", 칸["값"])
        if m:
            값, 단위 = m.group(1).replace(",", ""), m.group(2) or None
    연산 = 칸.get("위반연산") or 칸.get("발동연산")
    분모 = sorted(x.strip() for x in re.split(r"[,·]", 칸.get("분모", "")) if x.strip())
    return {"id": 조각[0], "조문": 조문, "종류": 종류, "대상": 대상, "값": 값, "단위": 단위, "연산": 연산,
            "분모": 분모, "줄": 줄, "발췌": _발췌들(줄)}


def 해석(글: str) -> dict:
    """추출표 문서 → {블록, 개념{키: 줄}, 관계{키: 줄}, 규범[…], 발췌[(조문, 칸, 발췌)]}."""
    블록 = _블록들(글)
    개념: dict[tuple, str] = {}
    관계: dict[tuple, str] = {}
    규범: list[dict] = []
    발췌: list[tuple[str, str, str]] = []
    for 조문, 칸 in 블록.items():
        for 줄 in 칸.get("개념", []):
            if "해당 없음" in 줄:
                continue
            for 항목 in re.split(r"\s+·\s+", 줄):
                k = _개념_키(항목)
                if k:
                    개념.setdefault(k, 항목.strip())
        for 줄 in 칸.get("관계", []):
            if "해당 없음" in 줄:
                continue
            m = _RE_관계.match(줄)
            if not m:
                continue
            관계.setdefault((_노드_키(m["src"]), m["관계"], _노드_키(m["dst"])), 줄)
            for e in _발췌들(m["근거"] or ""):
                발췌.append((조문, "관계", e))
        for 줄 in 칸.get("규범", []):
            n = _규범_해석(줄, 조문)
            if n:
                규범.append(n)
                for e in n["발췌"]:
                    발췌.append((조문, "규범", e))
    return {"블록": 블록, "개념": 개념, "관계": 관계, "규범": 규범, "발췌": 발췌}


def _규범_키(n: dict) -> tuple:
    return (n["조문"], n["종류"], n["값"], n["단위"], n["연산"], tuple(n["분모"]), tuple(n["대상"]))


def _규범_느슨한키(n: dict) -> tuple:
    """수치만 다른 것을 찾는 키: (조문, 종류, 대상)."""
    return (n["조문"], n["종류"], tuple(n["대상"]))


def 비교(a: dict, b: dict) -> dict:
    """비교 키로 맞춘다. 같은 키가 여러 번이면 개수까지 맞춘다(한 항의 같은 규범 두 번은 불일치)."""
    from collections import defaultdict
    표a, 표b = defaultdict(list), defaultdict(list)
    for n in a["규범"]:
        표a[_규범_키(n)].append(n)
    for n in b["규범"]:
        표b[_규범_키(n)].append(n)
    일치, a만, b만 = [], [], []
    for k in sorted(set(표a) | set(표b), key=str):
        x, y = 표a.get(k, []), 표b.get(k, [])
        m = min(len(x), len(y))
        일치 += x[:m]
        a만 += x[m:]
        b만 += y[m:]
    # 수치만 다른 것은 A만 · B만 에서 따로 뽑아 값이 다른 것으로 보인다(일치율 분모에는 그대로 남는다).
    값다름: list[tuple[dict, dict]] = []
    b남은 = list(b만)
    for n in a만:
        짝 = next((m for m in b남은 if _규범_느슨한키(m) == _규범_느슨한키(n)), None)
        if 짝:
            값다름.append((n, 짝))
            b남은.remove(짝)

    def 집합비교(표1: dict, 표2: dict) -> tuple[list, list, list]:
        return (sorted(set(표1) & set(표2), key=str), sorted(set(표1) - set(표2), key=str),
                sorted(set(표2) - set(표1), key=str))

    개념 = 집합비교(a["개념"], b["개념"])
    관계 = 집합비교(a["관계"], b["관계"])
    분모 = lambda 일, 에이, 비: (len(일) / (len(일) + len(에이) + len(비))) if (일 or 에이 or 비) else 1.0  # noqa: E731
    return {"규범": {"일치": 일치, "A만": a만, "B만": b만, "값다름": 값다름, "일치율": 분모(일치, a만, b만)},
            "개념": {"일치": 개념[0], "A만": 개념[1], "B만": 개념[2], "일치율": 분모(*개념)},
            "관계": {"일치": 관계[0], "A만": 관계[1], "B만": 관계[2], "일치율": 분모(*관계)}}


def 발췌_검사(추출: dict, 입력원문: str) -> list[tuple[str, str, str]]:
    """입력 원문의 부분 문자열이 아닌 발췌. 입력 원문의 조문 머리말은 대조 대상에 넣지 않는다."""
    원문 = 발췌_정규화(re.sub(r"^#{1,6}\s+조:\S.*$", "", 입력원문, flags=re.M))
    return [(조, 칸, e) for 조, 칸, e in 추출["발췌"] if 발췌_정규화(e.replace('\\"', '"')) not in 원문]


def 누락_블록(추출: dict, 입력원문: str) -> list[str]:
    """입력에 있는데 출력에 블록이 없는 조문 ID(추출자는 모든 조문에 블록을 써야 한다)."""
    입력 = {m.group(1).strip() for m in _RE_블록.finditer(입력원문)}
    return sorted(입력 - set(추출["블록"]))


def 보고서(a: dict, b: dict, r: dict, 오류a: list, 오류b: list, 누락a: list, 누락b: list) -> str:
    줄 = ["# 비교 결과", "",
          "| 구분 | 일치 | A만 | B만 | 일치율 |", "|---|---|---|---|---|"]
    for 이름 in ("규범", "개념", "관계"):
        x = r[이름]
        줄.append(f"| {이름} | {len(x['일치'])} | {len(x['A만'])} | {len(x['B만'])} | {x['일치율']:.1%} |")
    줄 += ["", f"입력 조문 중 블록 누락: A {len(누락a)}, B {len(누락b)}",
           f"발췌오류: A {len(오류a)}, B {len(오류b)}", ""]
    for 이름, 목록 in (("A 블록 누락", 누락a), ("B 블록 누락", 누락b)):
        if 목록:
            줄 += [f"## {이름}", *[f"- {x}" for x in 목록], ""]
    for 이름, 목록 in (("A 발췌오류", 오류a), ("B 발췌오류", 오류b)):
        if 목록:
            줄 += [f"## {이름}", *[f"- 발췌오류 {조} ({칸}): {e}" for 조, 칸, e in 목록], ""]
    if r["규범"]["값다름"]:
        줄 += ["## 규범: 값이 다른 것"]
        for x, y in r["규범"]["값다름"]:
            줄 += [f"- A: {x['줄']}", f"  B: {y['줄']}"]
        줄.append("")
    짝 = {id(x) for p in r["규범"]["값다름"] for x in p}
    for 이름, 키 in (("규범: A만", "A만"), ("규범: B만", "B만")):
        목록 = [n for n in r["규범"][키] if id(n) not in 짝]
        if 목록:
            줄 += [f"## {이름}", *[f"- {n['줄']}" for n in 목록], ""]
    for 구분, 표a, 표b in (("개념", a["개념"], b["개념"]), ("관계", a["관계"], b["관계"])):
        if r[구분]["A만"]:
            줄 += [f"## {구분}: A만", *[f"- {표a[k]}" for k in r[구분]["A만"]], ""]
        if r[구분]["B만"]:
            줄 += [f"## {구분}: B만", *[f"- {표b[k]}" for k in r[구분]["B만"]], ""]
    if r["규범"]["일치"]:
        줄 += ["## 규범: 일치", *[f"- {n['줄']}" for n in r["규범"]["일치"]], ""]
    return "\n".join(줄) + "\n"


def 실행(폴더: Path) -> dict:
    입력 = (폴더 / "입력_원문.md").read_text(encoding="utf-8")
    a = 해석((폴더 / "추출_A.md").read_text(encoding="utf-8"))
    b = 해석((폴더 / "추출_B.md").read_text(encoding="utf-8"))
    r = 비교(a, b)
    오류a, 오류b = 발췌_검사(a, 입력), 발췌_검사(b, 입력)
    누락a, 누락b = 누락_블록(a, 입력), 누락_블록(b, 입력)
    (폴더 / "비교.md").write_text(보고서(a, b, r, 오류a, 오류b, 누락a, 누락b), encoding="utf-8")
    return {"규범일치율": r["규범"]["일치율"], "개념일치율": r["개념"]["일치율"], "관계일치율": r["관계"]["일치율"],
            "규범": {k: len(r["규범"][k]) for k in ("일치", "A만", "B만", "값다름")},
            "발췌오류": len(오류a) + len(오류b), "블록누락": len(누락a) + len(누락b)}


# ---------------------------------------------------------------- self-test

_입력 = """### 조:모두의창업#제36조①
“멘토링비”는 1인 1일 최대 30만원 한도(시간당 10만원
초과 불가)이며, 12만 5천원을 초과하는 경우 공제 후 집행하여야 한다.
- 14 -
"""
_추출A = """### 조:모두의창업#제36조①
원문: "멘토링비"는
개념: 세목:창업/지급수수료/멘토링비 (기존) · 주체:멘토 (신규) · 시간:1인1일 (신규)
관계: 세목:창업/지급수수료/멘토링비 -상위-> 비목:창업/지급수수료 [원문명시] 근거 조:모두의창업#제36조① "“멘토링비”는"
규범: 규범:모두의창업/제36조①/한도/1 | 대상 세목:창업/지급수수료/멘토링비 | 값 300000 원 | 위반연산 초과 | 분모 인,일 | 부가세 정의없음 | 근거 "1인 1일 최대 30만원 한도"
      규범:모두의창업/제36조①/한도/2 | 대상 세목:창업/지급수수료/멘토링비 | 값 100000 원 | 위반연산 초과 | 분모 시간 | 근거 "시간당 10만원 초과 불가"
      규범:모두의창업/제36조①/임계값/1 | 대상 세목:창업/지급수수료/멘토링비 | 값 125000 원 | 발동연산 초과 | 결과 의무/1 | 근거 "12만 5천원을 초과하는 경우"
모르는 것: 부가세 기준
용어 후보: 공제
"""
# B: 한도/1 · 한도/2 는 순번이 바뀌어도 일치, 임계값 값이 다르고(120000), 주체:멘토 대신 주체:책임멘토, 없는 발췌 1건
_추출B = """### 조:모두의창업#제36조①
원문: "멘토링비"는
개념: 세목:…/멘토링비 (신규) · 주체:책임멘토 (신규) · 시간:1인1일 (신규)
관계: 세목:…/멘토링비 -상위-> 비목:…/지급수수료 [원문명시] 근거 조:모두의창업#제36조① "“멘토링비”는"
규범: 규범:모두의창업/제36조①/한도/2 | 대상 세목:…/멘토링비 | 값 300,000 원 | 위반연산 초과 | 분모 일,인 | 근거 "1인 1일 최대 30만원 한도"
      규범:모두의창업/제36조①/한도/1 | 대상 세목:…/멘토링비 | 값 100000 원 | 위반연산 초과 | 분모 시간 | 근거 "시간당 10만원 초과 불가"
      규범:모두의창업/제36조①/임계값/1 | 대상 세목:…/멘토링비 | 값 120000 원 | 발동연산 초과 | 근거 "12만원을 초과"
모르는 것: 없음
용어 후보: 공제
"""


def _자체시험() -> int:
    a, b = 해석(_추출A), 해석(_추출B)
    r = 비교(a, b)
    오류a, 오류b = 발췌_검사(a, _입력), 발췌_검사(b, _입력)
    검사 = [
        ("규범 일치 2건(순번 무시 · 분모 순서 무시 · 천 단위 쉼표)", len(r["규범"]["일치"]) == 2),
        ("규범 A만 1건", len(r["규범"]["A만"]) == 1),
        ("규범 B만 1건", len(r["규범"]["B만"]) == 1),
        ("값이 다른 것 1건", len(r["규범"]["값다름"]) == 1),
        ("규범 일치율 = 2/4", abs(r["규범"]["일치율"] - 0.5) < 1e-9),
        ("개념 일치 2 · A만 1 · B만 1", (len(r["개념"]["일치"]), len(r["개념"]["A만"]), len(r["개념"]["B만"])) == (2, 1, 1)),
        ("관계 일치(생략 표기와 전체 ID 를 같게 본다)", len(r["관계"]["일치"]) == 1),
        ("A 발췌 전부 통과(굽은 따옴표 · 줄바꿈 정규화)", 오류a == []),
        ("B 없는 발췌 1건 발췌오류", len(오류b) == 1 and "12만원을 초과" in 오류b[0][2]),
        ("블록 누락 없음", 누락_블록(a, _입력) == []),
        ("블록 누락 잡음", 누락_블록(해석(""), _입력) == ["조:모두의창업#제36조①"]),
        ("페이지 표식 제거", 발췌_정규화("가 - 14 - 나") == "가 나"),
    ]
    실패 = [이름 for 이름, 통과 in 검사 if not 통과]
    for 이름, 통과 in 검사:
        print(("ok  " if 통과 else "FAIL"), 이름)
    print(f"selftest: {len(검사) - len(실패)}/{len(검사)} 통과")
    return 1 if 실패 else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("폴더", nargs="?", help="ontology/추출/<사업>/<묶음> 폴더")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _자체시험()
    if not args.폴더:
        ap.error("묶음 폴더 또는 --self-test 가 필요하다")
    print(실행(Path(args.폴더)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
