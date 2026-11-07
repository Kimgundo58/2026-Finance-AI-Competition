#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""확정 추출표(확정.md) → 원천 JSON (docs/5_룰/5-7d_확정표_형식.md · 5-8b_원천_JSON_형식.md).

검증자가 확정.md 에 적은 값을 옮기기만 한다. LLM · DB 를 쓰지 않고 판단하지 않는다.
옮길 수 없는 줄이 하나라도 있으면 전부 `조문 ID 줄 N: 이유` 로 내고 종료 코드 1 이며 파일을 하나도 고치지 않는다.
결과는 ontology_check.py(검사) · ontology_load.py(적재)가 읽는다.

실행: PYTHONIOENCODING=utf-8 python scripts/extraction_to_json.py <묶음 폴더> [--쓰기] [--재추출] [--원천 ontology]
      python scripts/extraction_to_json.py --self-test
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ontology_check import (  # noqa: E402
    검수단계_값, 결과종류_값, 계층_값, 공통_파일, 관계_값, 규범종류_값, 누적범위_값, 단위_값, 부가세_값, 분모_값, 연결종류_값,
    우선순위범위_값, 우선순위출처_값, 원천_기본폴더, 종류_최상위, 지역_값)
from ontology_norm import 개념ID_풀이, 규범ID_풀이, 모든비목, 발췌_정규화, 조문ID_풀이  # noqa: E402

판정_값 = ("일치", "A", "B", "혼합", "수정")
제외_종류 = ("주체", "시간", "절차", "증빙")          # 묶음 안에서 참조되지 않으면 넣지 않는 신규 개념 종류(5-12)
_라벨 = re.compile(r"^(원문결함|원문|개념|관계|규범|모르는 것|용어 후보|대상 밖)\s*:\s*(.*)$")
_블록머리 = re.compile(r"^###\s+(조:\S.*?)\s*$")       # `조:지침#부칙 제1조` 처럼 ID 안에 공백이 있다
_따옴표 = re.compile(r'"(?:[^"\\]|\\.)*"')
_근거항목 = re.compile(r'\s*(?:(조:[^"]*?)\s+)?"((?:[^"\\]|\\.)*)"\s*')
_관계머리 = re.compile(r"^(?P<src>\S.*?)\s+-(?P<관계>[^\s>-]+)->\s+(?P<dst>.+?)\s+\[(?P<연결종류>[^\]]+)\]\s+근거\s+(?P<근거>.+)$")
_개념머리 = re.compile(r"^(?P<id>\S+)\s+\((?P<표시>신규|기존|재사용)\)$")
_규범머리 = re.compile(r"^(?P<id>규범:\S+)(?P<추가>\s+\(근거 추가\))?$")
_값 = re.compile(r"^(-?\d+(?:\.\d+)?)\s+(\S+)$")
_기한일 = re.compile(r"^\d{4}-\d{2}-\d{2}$")

수치_칸 = ("값", "위반연산", "발동연산", "분모", "분모주체", "누적범위", "부가세", "지역")
결과_칸 = ("결과.종류", "결과.문장", "결과.조건문장", "결과.제재", "결과.활성화", "결과.대체", "결과.기한일")
근거_칸 = {"수치근거": "수치", "조건근거": "조건", "대상근거": "대상"}
규범_칸 = {"계층", "대상", "수범자", "승인주체", "조건", *수치_칸, "시점", "증빙", *결과_칸, "예외", "해석미결",
          *근거_칸, "판정", "검수단계"}
규범_근거추가_칸 = {*근거_칸, "판정", "검수단계"}
개념_신규_필수 = ("정식명", "별칭", "적용사업", "검수단계", "근거")
개념_신규_칸 = {*개념_신규_필수, "상위", "연결종류", "정의"}
관계_칸 = {"적용사업", "검수단계", "판정", "(근거 추가)", "문맥조문", "적용조문"}
값없는_칸 = ("해석미결", "(근거 추가)")


@dataclass
class 문제:
    조문: str
    줄: int
    이유: str

    def __str__(self) -> str:
        return f"{self.조문} 줄 {self.줄}: {self.이유}"


@dataclass
class 변환결과:
    문제들: list[문제] = field(default_factory=list)
    새글: dict[str, str] = field(default_factory=dict)       # 파일 이름 → 쓸 글(바뀐 파일만)
    옛글: dict[str, str | None] = field(default_factory=dict)
    기록: str = ""
    집계: dict = field(default_factory=dict)

    @property
    def 통과(self) -> bool:
        return not self.문제들


def JSON_글(값: dict) -> str:
    """원천 파일 저장 형식: 들여쓰기 1칸 · 한글 그대로 · LF · 끝 줄바꿈(ontology/*.json 과 같다)."""
    return json.dumps(값, ensure_ascii=False, indent=1) + "\n"


# 입력 읽기 ──────────────────────────────────────────────────────────────────

def 입력원문_읽기(글: str) -> dict[str, dict]:
    """입력_원문.md → {조문 ID: {본문(정규화), 원문결함}}. `<!--` 줄은 본문이 아니고 `[원문결함]` 표시만 본다."""
    out: dict[str, dict] = {}
    현재 = None
    for 줄 in 글.splitlines():
        m = _블록머리.match(줄)
        if m:
            현재 = out.setdefault(m.group(1), {"줄들": [], "원문결함": False})
        elif 현재 is not None:
            if 줄.startswith("<!--"):
                현재["원문결함"] = 현재["원문결함"] or "[원문결함]" in 줄
            else:
                현재["줄들"].append(줄)
    return {k: {"본문": 발췌_정규화("\n".join(v["줄들"])), "원문결함": v["원문결함"]} for k, v in out.items()}


@dataclass
class 항목:
    라벨: str
    조문: str
    줄: int
    내용: str


def 확정_읽기(글: str, 문제들: list[문제]) -> tuple[list[tuple[str, int, bool]], list[항목]]:
    """확정.md → (블록 [(조문, 줄, 원문결함 표시)], 개념 · 관계 · 규범 항목). `## ` 절과 첫 블록 앞 머리는 읽지 않는다."""
    블록: list[tuple[str, int, bool]] = []
    항목들: list[항목] = []
    현재: str | None = None
    라벨: str | None = None
    절안 = False
    for 번호, 줄 in enumerate(글.splitlines(), 1):
        m = _블록머리.match(줄)
        if m:
            if 절안:
                문제들.append(문제(m.group(1), 번호, "블록은 `## ` 절보다 앞에 둔다"))
                현재 = None
                continue
            현재, 라벨 = m.group(1), None
            블록.append([현재, 번호, False])
            continue
        if 줄.startswith("## "):
            절안, 현재 = True, None
            continue
        if 현재 is None or not 줄.strip():
            continue
        if 줄.startswith("#"):
            문제들.append(문제(현재, 번호, "블록 안에 제목 줄이 있다"))
            continue
        if 줄.startswith((" ", "\t")):
            if 라벨 is None:
                문제들.append(문제(현재, 번호, "라벨 없는 들여쓴 줄"))
            elif 라벨 in ("개념", "관계", "규범"):
                항목들.append(항목(라벨, 현재, 번호, 줄.strip()))
            continue
        m = _라벨.match(줄)
        if not m:
            문제들.append(문제(현재, 번호, "라벨 없는 줄: 원문 · 원문결함 · 개념 · 관계 · 규범 · 모르는 것 · 용어 후보 · 대상 밖 중 하나로 시작한다"))
            라벨 = None
            continue
        라벨, 내용 = m.group(1), m.group(2).strip()
        if 라벨 == "원문결함":
            if not 내용.startswith("true"):
                문제들.append(문제(현재, 번호, f"원문결함 줄은 `true` 로 시작한다: {내용!r}"))
            블록[-1][2] = True
        elif 라벨 in ("개념", "관계", "규범") and 내용:
            항목들.append(항목(라벨, 현재, 번호, 내용))
    return [tuple(b) for b in 블록], [h for h in 항목들 if h.내용 != "해당 없음"]


# 칸 풀기 ────────────────────────────────────────────────────────────────────

class _실패(Exception):
    pass


def 칸_나누기(내용: str) -> tuple[str, dict[str, str]]:
    """`머리 | 이름 값 | …` → (머리, {이름: 값}). 따옴표 밖의 ` | ` 에서만 나눈다(발췌는 표 행의 ` | ` 를 그대로 적는다).
    닫히지 않은 따옴표 · 같은 칸 두 번은 실패."""
    if '"' in _따옴표.sub("", 내용):
        raise _실패("따옴표가 닫히지 않았다")
    가림 = _따옴표.sub(lambda m: "\0" * len(m.group()), 내용)      # 글자 수를 그대로 둬 자리를 원문에 쓴다
    조각, 시작 = [], 0
    for m in re.finditer(r" \| ", 가림):
        조각.append(내용[시작:m.start()])
        시작 = m.end()
    조각.append(내용[시작:])
    칸: dict[str, str] = {}
    for c in 조각[1:]:
        c = c.strip()
        이름, 값 = (c, "") if c in 값없는_칸 else (c.partition(" ")[0], c.partition(" ")[2].strip())
        if 이름 in 칸:
            raise _실패(f"같은 칸이 두 번 있다: {이름}")
        if not 값 and 이름 not in 값없는_칸:
            raise _실패(f"값이 없는 칸: {이름!r}")
        if 이름 in 값없는_칸 and 값:
            raise _실패(f"`{이름}` 은 값 없이 쓴다")
        칸[이름] = 값
    return 조각[0].strip(), 칸


def _괄호밖_나누기(값: str, 무엇: str) -> list[str]:
    """괄호 밖의 쉼표에서만 나눈다(`대중교통(버스, 지하철)` · `기계장치(공구·기구,비품,SW등)` 은 하나). 괄호가 짝이 맞지 않으면 실패."""
    out, 조각, 깊이 = [], "", 0
    for ch in 값:
        if ch in "([{（":
            깊이 += 1
        elif ch in ")]}）":
            깊이 -= 1
            if 깊이 < 0:
                raise _실패(f"{무엇}의 괄호가 짝이 맞지 않는다: {값}")
        if ch == "," and 깊이 == 0:
            out.append(조각)
            조각 = ""
        else:
            조각 += ch
    if 깊이:
        raise _실패(f"{무엇}의 괄호가 짝이 맞지 않는다: {값}")
    out.append(조각)
    return [x.strip() for x in out if x.strip()]


def _생략검사(값: str) -> None:
    if "…" in 값 or "..." in 값:
        raise _실패(f"`…` 생략 표기가 든 ID: {값}")


def _ID들(값: str) -> list[str]:
    out = _괄호밖_나누기(값, "ID 목록")
    for x in out:
        _생략검사(x)
        if ":" not in x or " " in x:
            raise _실패(f"ID 형식이 아니다: {x!r}")
    return out


def _따옴표값(값: str, 이름: str) -> str:
    if not (_따옴표.fullmatch(값) and len(값) >= 2):
        raise _실패(f"{이름} 은 큰따옴표 하나로 감싼다")
    return 값[1:-1].replace('\\"', '"')


def _조문_검사(조문: str, schema: dict) -> None:
    _생략검사(조문)
    try:
        풀이 = 조문ID_풀이(조문)
    except ValueError as e:
        raise _실패(str(e)) from e
    if 풀이["문서키"] not in (schema.get("문서키") or {}):
        raise _실패(f"schema.json 문서키에 없다: {풀이['문서키']}")


class 해석기:
    """확정.md 의 줄을 원천 JSON 행 초안으로 바꾼다. 원천 · 입력 대조는 근거와 ID 형식까지만 한다."""

    def __init__(self, 입력: dict[str, dict], schema: dict, 묶음사업: dict[str, str]):
        self.입력, self.schema, self.묶음사업 = 입력, schema, 묶음사업

    def 근거(self, 값: str, 블록: str) -> list[dict]:
        """`"발췌"` 또는 `조:문서키#조문 "발췌"` 를 ` · ` 로 이은 것 → [{조문, 발췌[, 원문결함]}]."""
        out, pos = [], 0
        while True:
            m = _근거항목.match(값, pos)
            if not m:
                raise _실패(f"근거 꼴이 아니다(`\"발췌\"` 또는 `조:문서키#조문 \"발췌\"` 를 ` · ` 로 잇는다): {값[pos:pos + 40]!r}")
            조문 = (m.group(1) or 블록).strip()
            _생략검사(조문)
            if 조문 not in self.입력:
                raise _실패(f"입력에 없는 조문: {조문}")
            발췌 = 발췌_정규화(m.group(2).replace('\\"', '"'))
            if not 발췌:
                raise _실패("빈 발췌")
            if 발췌 not in self.입력[조문]["본문"]:
                raise _실패(f"입력 원문에 없는 발췌: {조문} {발췌!r}")
            g = {"조문": 조문, "발췌": 발췌}
            if self.입력[조문]["원문결함"]:
                g["원문결함"] = True
            out.append(g)
            pos = m.end()
            if pos >= len(값):
                return out
            if 값[pos] != "·":
                raise _실패(f"근거 사이는 ` · ` 로 잇는다: {값[pos:pos + 40]!r}")
            pos += 1

    def 범위(self, 값: str) -> str | None:
        """`공통` → None, 사업키 → 사업키(입력_문서키.json 의 사업)."""
        if 값 == "공통":
            return None
        if 값 not in self.묶음사업:
            raise _실패(f"적용사업은 `공통` 또는 이 묶음의 사업키({', '.join(self.묶음사업)})다: {값!r}")
        return 값

    def 개념(self, h: 항목) -> dict:
        머리, 칸 = 칸_나누기(h.내용)
        m = _개념머리.match(머리)
        if not m:
            raise _실패("개념은 한 줄에 하나, `<ID> (신규|기존|재사용) | …` 로 적는다")
        i, 표시 = m.group("id"), m.group("표시")
        _생략검사(i)
        try:
            풀이 = 개념ID_풀이(i)
        except ValueError as e:
            raise _실패(str(e)) from e
        if 풀이["종류"] not in 종류_최상위:
            raise _실패(f"모르는 개념 종류: {풀이['종류']}")
        if "근거" not in 칸:
            raise _실패("개념에는 근거가 있어야 한다")
        d = {"id": i, "표시": 표시, "근거": self.근거(칸["근거"], h.조문)}
        if 표시 != "신규":
            남는 = set(칸) - {"근거"}
            if 남는:
                raise _실패(f"({표시}) 개념에는 ID 와 근거만 적는다: {', '.join(sorted(남는))}")
            return d
        for k in 칸:
            if k not in 개념_신규_칸:
                raise _실패(f"모르는 칸: {k}")
        없는 = [k for k in 개념_신규_필수 if k not in 칸]
        if 없는:
            raise _실패(f"(신규) 개념에 필수 칸이 없다: {', '.join(없는)}")
        if 칸["검수단계"] not in 검수단계_값:
            raise _실패(f"검수단계 값이 틀리다: {칸['검수단계']!r}")
        사업 = self.범위(칸["적용사업"])
        if 풀이["사업키"] is not None and 풀이["사업키"] != 사업:
            raise _실패(f"ID 의 @{풀이['사업키']} 와 적용사업({칸['적용사업']})이 다르다")
        상위 = _ID들(칸["상위"]) if "상위" in 칸 else []
        if 상위 and "연결종류" not in 칸:
            raise _실패("상위를 적으면 연결종류도 적는다")
        if "연결종류" in 칸 and not 상위:
            raise _실패("상위 없이 연결종류만 적었다")
        if "연결종류" in 칸 and 칸["연결종류"] not in 연결종류_값:
            raise _실패(f"연결종류 값이 틀리다: {칸['연결종류']!r}")
        별칭 = [] if 칸["별칭"] == "없음" else _괄호밖_나누기(칸["별칭"], "별칭")
        d.update(종류=풀이["종류"], 정식명=칸["정식명"], 별칭=별칭, 상위=상위, 정의=칸.get("정의"), 사업=사업,
                 연결종류=칸.get("연결종류"), 검수단계=칸["검수단계"])
        return d

    def 끝점(self, 값: str, 끝: str) -> str:
        _생략검사(값)
        if 값.startswith("조:"):
            _조문_검사(값, self.schema)
        elif 값 == "미특정" or 값.startswith("미특정:"):
            if 끝 != "dst":
                raise _실패("미특정 은 dst 에만 쓴다")
        elif 값.startswith("사업:"):
            if 값[3:] not in (self.schema.get("사업키") or {}):
                raise _실패(f"schema.json 사업키에 없다: {값}")
        elif ":" not in 값 or " " in 값:
            raise _실패(f"{끝} 가 ID 형식이 아니다: {값!r}")
        return 값

    def 관계(self, h: 항목) -> dict:
        머리, 칸 = 칸_나누기(h.내용)
        m = _관계머리.match(머리)
        if not m:
            raise _실패("관계는 `src -관계-> dst [연결종류] 근거 \"발췌\" | 적용사업 … | 검수단계 …` 로 적는다")
        for k in 칸:
            if k not in 관계_칸:
                raise _실패(f"모르는 칸: {k}")
        종류 = m["관계"]
        if 종류 not in 관계_값:
            raise _실패(f"모르는 관계: {종류}")
        if m["연결종류"] not in 연결종류_값:
            raise _실패(f"연결종류 값이 틀리다: {m['연결종류']!r}")
        if "검수단계" not in 칸:
            raise _실패("필수 칸이 없다: 검수단계")
        if 칸["검수단계"] not in 검수단계_값:
            raise _실패(f"검수단계 값이 틀리다: {칸['검수단계']!r}")
        if "판정" in 칸 and 칸["판정"] not in 판정_값:
            raise _실패(f"판정 값이 틀리다: {칸['판정']!r}")
        d = {"src": self.끝점(m["src"].strip(), "src"), "관계": 종류, "dst": self.끝점(m["dst"].strip(), "dst"),
             "연결종류": m["연결종류"], "검수단계": 칸["검수단계"], "근거": self.근거(m["근거"], h.조문),
             "근거추가": "(근거 추가)" in 칸, "사업": None, "사업있음": "적용사업" in 칸}
        if "적용사업" in 칸:
            d["사업"] = self.범위(칸["적용사업"])
        elif 종류 != "상위":
            raise _실패("상위가 아닌 관계에는 적용사업(공통 또는 사업키)을 적는다")
        if 종류 == "상위" and ("문맥조문" in 칸 or "적용조문" in 칸):
            raise _실패("상위 관계에는 문맥조문 · 적용조문을 쓰지 않는다")
        d["문맥조문"] = 칸.get("문맥조문")
        if d["문맥조문"]:
            _조문_검사(d["문맥조문"], self.schema)
        d["적용조문"] = [x.strip() for x in 칸["적용조문"].split(",")] if "적용조문" in 칸 else None
        for 조 in d["적용조문"] or []:
            _조문_검사(조, self.schema)
        return d

    def 규범ID(self, i: str) -> dict:
        _생략검사(i)
        try:
            풀이 = 규범ID_풀이(i)
        except ValueError as e:
            raise _실패(str(e)) from e
        if 풀이["종류"] not in 규범종류_값:
            raise _실패(f"모르는 규범 종류: {풀이['종류']}")
        if not 풀이["순번"].isdigit():
            raise _실패(f"순번이 숫자가 아니다: {i}")
        if 풀이["범위"] != "공통" and 풀이["범위"] not in self.묶음사업:
            raise _실패(f"규범 ID 범위는 `공통` 또는 이 묶음의 사업키다: {풀이['범위']}")
        return 풀이

    def _규범ID들(self, 값: str) -> list[str]:
        out = _ID들(값)
        for x in out:
            if not x.startswith("규범:"):
                raise _실패(f"결과 · 예외의 ID 는 규범 ID 를 다 적는다: {x!r}")
            self.규범ID(x)
        return out

    def _조건(self, 값: str, 종류: str) -> dict:
        조각, 깊이, 시작, 따옴 = [], 0, 0, False
        for k, ch in enumerate(값):
            if ch == '"' and (k == 0 or 값[k - 1] != "\\"):
                따옴 = not 따옴
            elif not 따옴 and ch in "{[":
                깊이 += 1
            elif not 따옴 and ch in "}]":
                깊이 -= 1
            elif not 따옴 and 깊이 == 0 and 값.startswith(" · ", k):
                조각.append(값[시작:k])
                시작 = k + 3
        조각.append(값[시작:])
        구조화, 자연어, 우선 = [], [], None
        for c in (x.strip() for x in 조각):
            if not c:
                raise _실패("빈 조건 조각")
            if c.startswith("{"):
                바깥 = re.split(r'("(?:[^"\\]|\\.)*")', c)
                글 = "".join(re.sub(r"([{,]\s*)([^\s\"{},:\[\]]+)\s*:", r'\1"\2":', s) if k % 2 == 0 else s
                            for k, s in enumerate(바깥))
                try:
                    o = json.loads(글)
                except json.JSONDecodeError as e:
                    raise _실패(f"구조화 조건 꼴이 아니다: {c[:60]}") from e
                if not isinstance(o, dict):
                    raise _실패(f"구조화 조건 꼴이 아니다: {c[:60]}")
                if set(o) == {"항목", "연산", "값"}:
                    구조화.append(o)
                elif set(o) == {"상위", "하위", "범위", "출처"}:
                    if 우선 is not None:
                        raise _실패("우선순위 조건은 하나만 적는다")
                    if o["상위"] not in 계층_값 or o["하위"] not in 계층_값 or o["범위"] not in 우선순위범위_값 \
                            or o["출처"] not in 우선순위출처_값:
                        raise _실패(f"우선순위 조건 값이 틀리다: {c}")
                    우선 = o
                else:
                    raise _실패(f"구조화 조건의 키는 항목 · 연산 · 값 또는 상위 · 하위 · 범위 · 출처다: {sorted(o)}")
            else:
                if '"' in c:
                    raise _실패(f"자연어 조건은 따옴표 없이 쓴다: {c[:40]}")
                자연어.append(c)
        if 종류 == "우선순위":
            if 우선 is None or 구조화:
                raise _실패("우선순위 규범의 조건은 {상위, 하위, 범위, 출처} 하나(+ 자연어)다")
            return {**우선, **({"자연어": 자연어} if 자연어 else {})}
        if 우선 is not None:
            raise _실패("우선순위가 아닌 규범에 우선순위 조건을 썼다")
        return {"구조화": 구조화, "자연어": 자연어}

    def 규범(self, h: 항목) -> dict:
        머리, 칸 = 칸_나누기(h.내용)
        m = _규범머리.match(머리)
        if not m:
            raise _실패("규범 줄은 `<규범 ID> | …` 또는 `<규범 ID> (근거 추가) | …` 로 시작한다")
        i = m.group("id")
        풀이 = self.규범ID(i)
        추가 = bool(m.group("추가"))
        for k in 칸:
            if k == "근거":
                raise _실패("칸 없는 근거: 수치근거 · 조건근거 · 대상근거 중 하나로 적는다")
            if k == "결과":
                raise _실패(f"`결과 {칸[k]}` 줄임은 쓰지 않는다: 결과.종류 · 결과.활성화 <규범 ID> 처럼 칸을 적는다")
            if k == "적용사업":
                raise _실패("규범의 적용사업은 규범 ID 에서 푼다(칸으로 적지 않는다)")
            if k not in (규범_근거추가_칸 if 추가 else 규범_칸):
                raise _실패(f"모르는 칸: {k}" if k not in 규범_칸 else f"(근거 추가) 줄에는 근거 칸 · 판정 · 검수단계만 쓴다: {k}")
        if "검수단계" in 칸 and 칸["검수단계"] not in 검수단계_값:
            raise _실패(f"검수단계 값이 틀리다: {칸['검수단계']!r}")
        if "판정" in 칸 and 칸["판정"] not in 판정_값:
            raise _실패(f"판정 값이 틀리다: {칸['판정']!r}")
        근거 = {v: self.근거(칸[k], h.조문) if k in 칸 else [] for k, v in 근거_칸.items()}
        if not any(근거.values()):
            raise _실패("근거가 없다(수치근거 · 조건근거 · 대상근거 중 하나 이상)")
        if 추가:
            return {"id": i, "근거추가": True, "근거": 근거, "검수단계": 칸.get("검수단계"), "범위": 풀이["범위"]}
        for k in ("계층", "대상", "검수단계"):
            if k not in 칸:
                raise _실패(f"필수 칸이 없다: {k}")
        if 칸["계층"] not in 계층_값:
            raise _실패(f"계층 값이 틀리다: {칸['계층']!r}")
        수치 = None
        if any(k in 칸 for k in 수치_칸):
            mv = _값.match(칸.get("값", ""))
            if not mv:
                raise _실패("수치 칸이 있으면 `값 <숫자> <단위>` 가 있어야 한다")
            v = float(mv.group(1)) if "." in mv.group(1) else int(mv.group(1))
            if mv.group(2) not in 단위_값:
                raise _실패(f"단위 값이 틀리다: {mv.group(2)!r}")
            수치 = {"값": v, "단위": mv.group(2)}
            if "위반연산" in 칸:
                if 칸["위반연산"] not in ("초과", "이상"):
                    raise _실패(f"위반연산 값이 틀리다: {칸['위반연산']!r}")
                수치["위반연산"] = 칸["위반연산"]
            if "발동연산" in 칸:
                if 칸["발동연산"] not in ("이상", "초과"):
                    raise _실패(f"발동연산 값이 틀리다: {칸['발동연산']!r}")
                수치["발동연산"] = 칸["발동연산"]
            분모 = [x.strip() for x in 칸.get("분모", "").split(",") if x.strip()]
            for x in 분모:
                if x not in 분모_값:
                    raise _실패(f"분모 값이 틀리다: {x!r}")
            수치["분모"] = 분모
            수치["분모주체"] = 칸.get("분모주체")
            for k, 허용 in (("누적범위", 누적범위_값), ("부가세", 부가세_값), ("지역", 지역_값)):
                if k in 칸 and 칸[k] not in 허용:
                    raise _실패(f"{k} 값이 틀리다: {칸[k]!r}")
                수치[k] = 칸.get(k)
        행 = {"id": i, "종류": 풀이["종류"], "계층": 칸["계층"], "적용사업": None,
             "대상": _ID들(칸["대상"]), "수범자": _ID들(칸["수범자"]) if "수범자" in 칸 else [],
             "승인주체": _ID들(칸["승인주체"]) if "승인주체" in 칸 else [],
             "조건": self._조건(칸["조건"], 풀이["종류"]) if "조건" in 칸 else (None if 풀이["종류"] == "우선순위" else {"구조화": [], "자연어": []}),
             "수치": 수치}
        if 행["조건"] is None:
            raise _실패("우선순위 규범에는 조건 {상위, 하위, 범위, 출처} 가 있어야 한다")
        if "시점" in 칸:
            s = 칸["시점"]
            if s not in ("집행 전", "집행 후") and not (s.startswith("기한: ") and s[4:].strip()):
                raise _실패(f"시점은 `집행 전` · `집행 후` · `기한: <글>` 이다: {s!r}")
            행["시점"] = s
        if "증빙" in 칸:
            행["증빙"] = _ID들(칸["증빙"])
        결과: dict = {}
        if "결과.종류" in 칸:
            if '"' in 칸["결과.종류"]:
                raise _실패("결과.종류 는 따옴표 없이 쓴다")
            if 풀이["종류"] in ("금지", "한도") and 칸["결과.종류"] not in 결과종류_값:
                raise _실패(f"결과.종류 값이 틀리다: {칸['결과.종류']!r}. {' · '.join(결과종류_값)} 중 하나다. `불가` 가 아니면 코드 불가가 꺼진다")
            결과["종류"] = 칸["결과.종류"]
        for k in ("문장", "조건문장"):
            if f"결과.{k}" in 칸:
                결과[k] = _따옴표값(칸[f"결과.{k}"], f"결과.{k}")
                if "|" in 결과[k]:
                    raise _실패(f"결과.{k} 에 표 구분자 `|` 를 쓰지 않는다. 사용자가 읽는 문장이다")
        if "결과.제재" in 칸:
            if '"' in 칸["결과.제재"]:
                raise _실패("결과.제재 는 따옴표 없이 쓴다")
            결과["제재"] = 칸["결과.제재"]
        for k in ("활성화", "대체"):
            if f"결과.{k}" in 칸:
                결과[k] = self._규범ID들(칸[f"결과.{k}"])
        if "결과.기한일" in 칸:
            s = 칸["결과.기한일"]
            try:
                ok = bool(_기한일.match(s)) and bool(datetime.date.fromisoformat(s))
            except ValueError:
                ok = False
            if not ok:
                raise _실패(f"결과.기한일 은 YYYY-MM-DD 다: {s!r}")
            결과["기한일"] = s
        예외: list[str] = []
        if "예외" in 칸:
            pos, 값 = 0, 칸["예외"]
            while pos < len(값):
                mm = re.compile(r'\s*("(?:[^"\\]|\\.)*"|규범:(?:[^,\s(]|\([^)]*\))+)\s*(,|$)').match(값, pos)
                if not mm:
                    raise _실패(f"예외는 규범 ID 또는 \"단서 발췌\" 를 `, ` 로 잇는다: {값[pos:pos + 40]!r}")
                x = mm.group(1)
                if x.startswith('"'):
                    발췌 = 발췌_정규화(x[1:-1].replace('\\"', '"'))
                    if not 발췌 or 발췌 not in self.입력[h.조문]["본문"]:
                        raise _실패(f"입력 원문에 없는 단서 발췌: {발췌!r}")
                    예외.append(발췌)
                else:
                    예외 += self._규범ID들(x)
                pos = mm.end()
        행.update(결과=결과, 예외=예외, 해석미결="해석미결" in 칸, 검수단계=칸["검수단계"], 근거=근거)
        return {"id": i, "근거추가": False, "행": 행, "범위": 풀이["범위"]}


# 원천 다루기 ────────────────────────────────────────────────────────────────

def _목록(값) -> list:
    return [] if 값 is None else 값 if isinstance(값, list) else [값]


def 규범_조문(규범ID: str) -> str:
    """규범 ID 가 속한 조문 ID(5-8b). `규범:모두의창업/제36조①/한도/1` → `조:모두의창업#제36조①`,
    `규범:공통/지침#제35조①/허용/1` → `조:지침#제35조①`(조문 키에 `#` 가 있으면 문서키가 들어 있다). 형식이 틀리면 ValueError."""
    풀이 = 규범ID_풀이(규범ID)
    키 = 풀이["조문키"]
    return f"조:{키}" if "#" in 키 else f"조:{풀이['범위']}#{키}"


def _근거_합치기(대상목록: list, 새것: list) -> int:
    """(조문, 발췌) 가 같은 근거는 빼고 덧붙인다. 덧붙인 수."""
    있음 = {(g.get("조문"), g.get("발췌")) for g in 대상목록}
    n = 0
    for g in 새것:
        if (g["조문"], g["발췌"]) not in 있음:
            대상목록.append(dict(g))
            있음.add((g["조문"], g["발췌"]))
            n += 1
    return n


def 변환(묶음: Path, 원천폴더: Path, 재추출: bool = False) -> 변환결과:
    묶음, 원천폴더 = Path(묶음), Path(원천폴더)
    r = 변환결과()
    P = r.문제들

    def 읽기(경로: Path) -> str | None:
        try:
            return 경로.read_text(encoding="utf-8")
        except FileNotFoundError:
            P.append(문제("-", 0, f"파일이 없다: {경로}"))
            return None

    확정글, 원문글, 문서키글, schema글 = (읽기(묶음 / "확정.md"), 읽기(묶음 / "입력_원문.md"),
                                    읽기(묶음 / "입력_문서키.json"), 읽기(원천폴더 / "schema.json"))
    if None in (확정글, 원문글, 문서키글, schema글):
        return r
    schema, 문서키 = json.loads(schema글), json.loads(문서키글)
    사업표 = schema.get("사업키") or {}
    묶음사업 = 문서키.get("사업키") or {}
    for k, v in 묶음사업.items():
        if 사업표.get(k) != v:
            P.append(문제("-", 0, f"입력_문서키.json 사업키 {k}={v!r} 가 schema.json 과 다르다({사업표.get(k)!r})"))
    for k, v in (문서키.get("문서키") or {}).items():
        if (schema.get("문서키") or {}).get(k) != v:
            P.append(문제("-", 0, f"입력_문서키.json 문서키 {k}={v!r} 가 schema.json 과 다르다"))
    입력 = 입력원문_읽기(원문글)
    블록, 항목들 = 확정_읽기(확정글, P)
    for 조문, 줄, 결함 in 블록:
        if 조문 not in 입력:
            P.append(문제(조문, 줄, f"입력에 없는 조문: {조문}"))
        elif 결함 and not 입력[조문]["원문결함"]:
            P.append(문제(조문, 줄, "확정표는 원문결함이라는데 입력_원문.md 블록에 [원문결함] 표시가 없다"))
        elif not 결함 and 입력[조문]["원문결함"]:
            P.append(문제(조문, 줄, "입력_원문.md 블록은 [원문결함] 인데 확정표 블록에 `원문결함: true` 줄이 없다"))
    블록ID = [b[0] for b in 블록]
    for 조문 in 블록ID:
        if 블록ID.count(조문) > 1:
            P.append(문제(조문, 0, "같은 조문 블록이 두 번 있다"))
            break

    # 원천 파일: 공통 + schema 의 사업 파일 전부(있는 것). 쓰는 것은 공통과 이 묶음 사업뿐이다.
    파일: dict[str, dict] = {}
    for 이름 in [공통_파일, *[f"{k}.json" for k in 사업표]]:
        경로 = 원천폴더 / 이름
        if 경로.exists():
            r.옛글[이름] = 경로.read_text(encoding="utf-8")
            파일[이름] = json.loads(r.옛글[이름])
        elif 이름 == 공통_파일:
            P.append(문제("-", 0, f"원천 파일이 없다: {경로}"))
            return r
    for k in 묶음사업:
        이름 = f"{k}.json"
        if 이름 not in 파일:
            r.옛글[이름] = None
            파일[이름] = {"스키마": schema.get("스키마"), "사업": 사업표[k], "사업키": k, "개념": [], "관계": [], "규범": []}

    def 파일이름(사업키: str | None) -> str:
        return 공통_파일 if 사업키 is None else f"{사업키}.json"

    def 사업명(사업키: str | None) -> str | None:
        return None if 사업키 is None else 사업표[사업키]

    def 파일사업(이름: str) -> str | None:
        return None if 이름 == 공통_파일 else 파일[이름].get("사업")

    쓰는파일 = {공통_파일, *[f"{k}.json" for k in 묶음사업]}
    기존개념 = {c["id"]: (이름, c) for 이름, f in 파일.items() for c in f.get("개념") or []}
    기존규범 = {n["id"]: (이름, n) for 이름, f in 파일.items() for n in f.get("규범") or []}
    기존관계 = {}
    for 이름, f in 파일.items():
        for x in f.get("관계") or []:
            기존관계[(x["src"], x["관계"], x["dst"], x["적용사업"] if "적용사업" in x else 파일사업(이름))] = (이름, x)

    # 1. 줄 해석 ─────────────────────────────────────────────────────────
    해 = 해석기(입력, schema, 묶음사업)
    개념줄, 관계줄, 규범줄 = [], [], []
    for h in 항목들:
        if h.조문 not in 입력:
            continue                         # 블록 머리에서 이미 실패로 적었다
        try:
            d = getattr(해, h.라벨)(h)
        except _실패 as e:
            P.append(문제(h.조문, h.줄, str(e)))
            continue
        d["_줄"], d["_조문"] = h.줄, h.조문
        {"개념": 개념줄, "관계": 관계줄, "규범": 규범줄}[h.라벨].append(d)

    신규: dict[str, dict] = {}
    for d in 개념줄:
        if d["표시"] == "신규":
            if d["id"] in 신규:
                P.append(문제(d["_조문"], d["_줄"], f"같은 개념을 두 번 (신규) 로 적었다(두 번째부터는 재사용): {d['id']}"))
            else:
                신규[d["id"]] = d
        elif d["표시"] == "기존" and d["id"] not in 기존개념:
            P.append(문제(d["_조문"], d["_줄"], f"(기존) 은 원천에 있어야 한다(이 표의 (신규) 를 가리키면 (재사용)): {d['id']}"))
        elif d["id"] not in 기존개념 and d["id"] not in 신규 and not any(
                x["id"] == d["id"] and x["표시"] == "신규" for x in 개념줄):
            P.append(문제(d["_조문"], d["_줄"], f"({d['표시']}) 라는데 원천에도 이 표의 (신규) 에도 없다: {d['id']}"))
    표규범 = {}
    for d in 규범줄:
        if not d["근거추가"]:
            if d["id"] in 표규범:
                P.append(문제(d["_조문"], d["_줄"], f"같은 규범 ID 가 표에 두 번 있다: {d['id']}"))
            표규범[d["id"]] = d

    # 2. 참조 검사 ───────────────────────────────────────────────────────
    def 개념있음(i: str) -> bool:
        return i in 기존개념 or i in 신규

    묶음사업명 = {사업명(k) for k in 묶음사업}

    def 다른사업개념_검사(i: str, 행사업: str | None, d: dict) -> None:
        """다른 사업 전용 개념은 가리켜도 이 사업 조회에서 보이지 않는다(ontology_lookup.py:56, :407). 규범이 죽으므로 실패시킨다."""
        사 = 기존개념[i][1].get("적용사업") if i in 기존개념 else None
        if 사 is not None and 사 != 행사업 and 사 not in 묶음사업명:
            P.append(문제(d["_조문"], d["_줄"], f"다른 사업 전용 개념이다(적용사업 {사}): {i}. 이 사업에서 쓰려면 `<ID>@<사업키>` 를 (신규) 로 적는다"))

    for d in 개념줄:
        다른사업개념_검사(d["id"], None, d)
    for d in 신규.values():
        for p in d["상위"]:
            다른사업개념_검사(p, 사업명(d["사업"]), d)
    for d in 관계줄:
        for 끝 in ("src", "dst"):
            v = d[끝]
            if not (v.startswith(("조:", "미특정", "사업:"))) and not 개념있음(v):
                P.append(문제(d["_조문"], d["_줄"], f"{끝} 가 원천에도 이 표의 (신규) 에도 없다: {v}"))
            elif not v.startswith(("조:", "미특정", "사업:")):
                다른사업개념_검사(v, 사업명(d["사업"]) if d["사업있음"] else None, d)
        if d["관계"] == "상위" and (d["src"].startswith(("조:", "미특정", "사업:")) or d["dst"].startswith(("조:", "미특정", "사업:"))):
            P.append(문제(d["_조문"], d["_줄"], "상위 관계의 양 끝은 개념이다"))
    for i, d in 신규.items():
        for p in d["상위"]:
            if not 개념있음(p):
                P.append(문제(d["_조문"], d["_줄"], f"상위가 원천에도 이 표의 (신규) 에도 없다: {p}"))
    for d in 규범줄:
        if d["근거추가"]:
            if d["id"] not in 표규범 and d["id"] not in 기존규범:
                P.append(문제(d["_조문"], d["_줄"], f"(근거 추가) 인데 원천에도 표에도 없는 규범: {d['id']}"))
            continue
        행 = d["행"]
        for 칸 in ("대상", "수범자", "승인주체", "증빙"):
            for v in 행.get(칸) or []:
                if not (칸 == "대상" and v == 모든비목) and not 개념있음(v):
                    P.append(문제(d["_조문"], d["_줄"], f"{칸} 이 원천에도 이 표의 (신규) 에도 없다: {v}"))
                다른사업개념_검사(v, 사업명(None if d["범위"] == "공통" else d["범위"]), d)
        for v in [*행["결과"].get("활성화", []), *행["결과"].get("대체", []), *[x for x in 행["예외"] if x.startswith("규범:")]]:
            if v not in 표규범 and v not in 기존규범:
                P.append(문제(d["_조문"], d["_줄"], f"가리키는 규범이 원천에도 표에도 없다: {v}"))
        if d["id"] in 기존규범:
            if not 재추출:
                P.append(문제(d["_조문"], d["_줄"], f"같은 규범 ID 가 원천에 있다(교체하려면 --재추출): {d['id']}"))
            elif 기존규범[d["id"]][0] != 파일이름(None if d["범위"] == "공통" else d["범위"]):
                P.append(문제(d["_조문"], d["_줄"], f"교체할 규범이 다른 파일에 있다: {기존규범[d['id']][0]}"))

    # 2b. --재추출: 이 확정표 블록에 속한 원천 규범은 모두 표에 있어야 한다. 지우는 기능은 없다(5-7d 변환 규칙).
    #     표에 (근거 추가) 로만 적힌 규범도 「표에 있다」로 본다(검증자가 남긴 행이다). 표에 없는 블록의 규범은 보지 않는다.
    블록줄 = {조문: 줄 for 조문, 줄, _ in 블록}
    if 재추출:
        표ID = {d["id"] for d in 규범줄}
        for i in 기존규범:
            try:
                조 = 규범_조문(i)
            except ValueError:
                continue
            if 조 in 블록줄 and i not in 표ID:
                P.append(문제(조, 블록줄[조], f"확정표에 없는 원천 규범이 있다: {i}. 변환기는 지우지 않는다"))

    # 3. 참조되지 않는 신규 주체 · 시간 · 절차 · 증빙 (묶음 안 참조만 센다) ──────────
    참조 = set()
    for d in 규범줄:
        if not d["근거추가"]:
            for 칸 in ("대상", "수범자", "승인주체", "증빙"):
                참조.update(d["행"].get(칸) or [])
    for d in 관계줄:
        if d["관계"] != "상위":
            참조.update((d["src"], d["dst"]))
    상위들 = {i: list(d["상위"]) for i, d in 신규.items()}
    for d in 관계줄:
        if d["관계"] == "상위":
            상위들.setdefault(d["src"], []).append(d["dst"])
    while True:
        들어감 = {i for i, d in 신규.items() if i in 기존개념 or d["종류"] not in 제외_종류 or i in 참조}
        새참조 = 참조 | {p for i in 들어감 | set(기존개념) for p in 상위들.get(i, [])}
        if 새참조 == 참조:
            break
        참조 = 새참조
    뺀개념 = [i for i in 신규 if i not in 들어감]

    if P:
        return r

    # 4. 원천에 옮기기 ───────────────────────────────────────────────────
    합친개념, 합친관계, 합친규범, 교체규범, 이어붙인근거, 교체관계 = [], [], [], [], [], []
    새개념, 새관계, 새규범 = {n: 0 for n in 쓰는파일}, {n: 0 for n in 쓰는파일}, {n: 0 for n in 쓰는파일}

    def 쓸수있나(이름: str, d: dict, 무엇: str, 행사업: str | None) -> bool:
        """공통 행(적용사업 null)은 어느 파일에 있든 근거 · 별칭을 합친다(5-7d). 공통 범위는 모든 파일을 합쳐 읽는다(ontology_load.py:7).
        다른 사업 전용 행은 고칠 수 없다."""
        if 이름 not in 쓰는파일:
            if 행사업 is None:
                쓰는파일.add(이름)
                return True
            P.append(문제(d["_조문"], d["_줄"], f"{무엇}이 이 묶음 밖 사업 파일({이름})에 있어 고칠 수 없다"))
            return False
        return True

    초안: dict[str, dict] = {}                 # 새 개념 id → 상위 · 연결종류 · 근거를 모으는 초안
    for i, d in 신규.items():
        if i in 뺀개념:
            continue
        if i in 기존개념:
            이름, c = 기존개념[i]
            다름 = [k for k, a, b in (("정식명", c.get("정식명"), d["정식명"]), ("검수단계", c.get("검수단계"), d["검수단계"]),
                                     ("적용사업", c.get("적용사업"), 사업명(d["사업"])))
                  if a != b]
            if d["상위"] and set(_목록(c.get("상위"))) != set(d["상위"]):
                다름.append("상위")
            if d["연결종류"] and c.get("연결종류") != d["연결종류"]:
                다름.append("연결종류")
            if d["정의"] and c.get("정의") != d["정의"]:
                다름.append("정의")
            if 다름:
                P.append(문제(d["_조문"], d["_줄"], f"원천에 있는 개념과 값이 다르다({', '.join(다름)}): {i}"))
                continue
            if not 쓸수있나(이름, d, "개념", c.get("적용사업")):
                continue
            n = _근거_합치기(c.setdefault("근거", []), d["근거"])
            별칭 = c.setdefault("별칭", [])
            for a in d["별칭"]:
                if a not in 별칭:
                    별칭.append(a)
                    n += 1
            if n and i not in 합친개념:
                합친개념.append(i)
            continue
        이름 = 파일이름(d["사업"])
        행: dict = {}
        파일[이름]["개념"].append(행)
        새개념[이름] += 1
        초안[i] = {"행": 행, "d": d, "상위": list(d["상위"]), "연결종류": d["연결종류"], "근거": [], "이름": 이름}
        _근거_합치기(초안[i]["근거"], d["근거"])

    # 개념 근거 줄 · 상위 관계를 줄 순서대로
    사건 = sorted([("개념", d) for d in 개념줄 if d["표시"] != "신규"] + [("상위", d) for d in 관계줄 if d["관계"] == "상위"],
                key=lambda x: x[1]["_줄"])
    for 갈래, d in 사건:
        i = d["id"] if 갈래 == "개념" else d["src"]
        if i in 뺀개념:
            continue
        if i in 초안:
            o = 초안[i]
            if 갈래 == "상위":
                if d["근거추가"] and d["dst"] not in o["상위"]:
                    P.append(문제(d["_조문"], d["_줄"], f"(근거 추가) 인데 기존 상위 관계가 없다: {i} -상위-> {d['dst']}"))
                    continue
                if o["연결종류"] not in (None, d["연결종류"]):
                    P.append(문제(d["_조문"], d["_줄"], f"한 개념의 상위 연결종류가 둘이다({o['연결종류']} · {d['연결종류']}): {i}"))
                    continue
                if d["검수단계"] != o["d"]["검수단계"]:
                    P.append(문제(d["_조문"], d["_줄"], f"상위 관계의 검수단계({d['검수단계']})가 개념({o['d']['검수단계']})과 다르다: {i}"))
                    continue
                if d["사업있음"] and d["사업"] != o["d"]["사업"]:
                    P.append(문제(d["_조문"], d["_줄"], f"상위 관계의 적용사업이 개념과 다르다: {i}"))
                    continue
                o["연결종류"] = d["연결종류"]
                if d["dst"] not in o["상위"]:
                    o["상위"].append(d["dst"])
            _근거_합치기(o["근거"], d["근거"])
            continue
        이름, c = 기존개념[i]
        if 갈래 == "상위":
            if d["dst"] not in _목록(c.get("상위")):
                P.append(문제(d["_조문"], d["_줄"], f"상위 충돌: 원천의 {i} 상위는 {c.get('상위')!r} 인데 {d['dst']} 를 적었다"))
                continue
            if c.get("연결종류") != d["연결종류"] or c.get("검수단계") != d["검수단계"]:
                P.append(문제(d["_조문"], d["_줄"], f"원천의 상위 관계와 연결종류 · 검수단계가 다르다"
                                                  f"({c.get('연결종류')} · {c.get('검수단계')}): {i}"))
                continue
            if d["사업있음"] and 사업명(d["사업"]) != c.get("적용사업"):
                P.append(문제(d["_조문"], d["_줄"], f"상위 관계의 적용사업이 원천 개념과 다르다: {i}"))
                continue
        if not 쓸수있나(이름, d, "개념", c.get("적용사업")):
            continue
        if _근거_합치기(c.setdefault("근거", []), d["근거"]) and i not in 합친개념:
            합친개념.append(i)

    for i, o in 초안.items():
        d = o["d"]
        상위 = o["상위"]
        if 상위 and not o["연결종류"]:
            P.append(문제(d["_조문"], d["_줄"], f"상위는 있는데 연결종류가 없다: {i}"))
        행 = {"id": i, "종류": d["종류"], "정식명": d["정식명"], "별칭": d["별칭"],
             "상위": None if not 상위 else 상위[0] if len(상위) == 1 else 상위, "정의": d["정의"], "적용사업": 사업명(d["사업"])}
        if 상위:
            행["연결종류"] = o["연결종류"]
        행.update(검수단계=d["검수단계"], 근거=o["근거"])
        o["행"].update(행)

    # 상위가 아닌 관계
    표관계: dict[tuple, dict] = {}
    for d in 관계줄:
        if d["관계"] == "상위":
            continue
        키 = (d["src"], d["관계"], d["dst"], 사업명(d["사업"]))
        if 키 in 표관계 or 키 in 기존관계:
            if 키 in 표관계:
                이름, x = None, 표관계[키]
            else:
                이름, x = 기존관계[키]
            다름 = [k for k in ("연결종류", "검수단계") if x.get(k) != d[k]]
            if d["문맥조문"] and x.get("문맥조문") != d["문맥조문"]:
                다름.append("문맥조문")
            if d["적용조문"] is not None and _목록(x.get("적용조문")) != d["적용조문"]:
                다름.append("적용조문")
            if 다름 and (not 재추출 or 이름 is None):
                P.append(문제(d["_조문"], d["_줄"], f"같은 관계가 있는데 값이 다르다({', '.join(다름)}): {키[:3]}"))
                continue
            if 이름 is not None and not 쓸수있나(이름, d, "관계", 키[3]):
                continue
            if 다름:                               # --재추출: 규범 교체와 같은 규칙. 표 값으로 바꾸고 기록한다(5-7d)
                x.update({k: d[k] for k in 다름})
                교체관계.append(키[:3])
            if _근거_합치기(x.setdefault("근거", []), d["근거"]) and 이름 is not None and 키 not in 합친관계:
                합친관계.append(키)
            continue
        if d["근거추가"]:
            P.append(문제(d["_조문"], d["_줄"], f"(근거 추가) 인데 원천에도 표에도 같은 관계가 없다: {키[:3]}"))
            continue
        행 = {"src": d["src"], "관계": d["관계"], "dst": d["dst"], "연결종류": d["연결종류"], "적용사업": 사업명(d["사업"]),
             "문맥조문": d["문맥조문"]}
        if d["적용조문"] is not None:
            행["적용조문"] = d["적용조문"]
        행.update(검수단계=d["검수단계"], 근거=[])
        _근거_합치기(행["근거"], d["근거"])
        이름 = 파일이름(d["사업"])
        파일[이름]["관계"].append(행)
        새관계[이름] += 1
        표관계[키] = 행

    # 규범
    표행: dict[str, dict] = {}
    for d in 규범줄:
        if d["근거추가"]:
            continue
        사업키 = None if d["범위"] == "공통" else d["범위"]
        행 = d["행"]
        행["적용사업"] = 사업명(사업키)
        키순 = ["id", "종류", "계층", "적용사업", "대상", "수범자", "승인주체", "조건", "수치", "시점", "증빙",
              "결과", "예외", "해석미결", "검수단계", "근거"]
        행 = {k: 행[k] for k in 키순 if k in 행}
        이름 = 파일이름(사업키)
        if d["id"] in 기존규범:
            목록 = 파일[이름]["규범"]
            k = next(j for j, n in enumerate(목록) if n["id"] == d["id"])
            # 옛 행의 근거 중 이 확정표 블록 밖 조문의 것(다른 묶음의 (근거 추가))은 새 행에 이어 붙인다(버리지 않는다)
            for 칸, 옛근거 in (목록[k].get("근거") or {}).items():
                for g in 옛근거 or []:
                    if g.get("조문") not in 블록줄 and _근거_합치기(행["근거"].setdefault(칸, []), [g]):
                        이어붙인근거.append((d["id"], 칸, g.get("조문"), g.get("발췌")))
            목록[k] = 행
            교체규범.append(d["id"])
        else:
            파일[이름]["규범"].append(행)
            새규범[이름] += 1
        표행[d["id"]] = 행
    for d in 규범줄:
        if not d["근거추가"]:
            continue
        if d["id"] in 표행:
            n, 이름 = 표행[d["id"]], None
        else:
            이름, n = 기존규범[d["id"]]
            if not 쓸수있나(이름, d, "규범", n.get("적용사업")):
                continue
        if d["검수단계"] and n.get("검수단계") != d["검수단계"]:
            P.append(문제(d["_조문"], d["_줄"], f"(근거 추가) 의 검수단계({d['검수단계']})가 규범({n.get('검수단계')})과 다르다"))
            continue
        근거 = n.setdefault("근거", {"수치": [], "조건": [], "대상": []})
        덧 = sum(_근거_합치기(근거.setdefault(k, []), v) for k, v in d["근거"].items())
        if 덧 and 이름 is not None and d["id"] not in 합친규범:
            합친규범.append(d["id"])

    if P:
        return r

    for 이름 in sorted(쓰는파일):
        글 = JSON_글(파일[이름])
        if 글 != r.옛글.get(이름):
            r.새글[이름] = 글

    # 5. 집계 · 기록 ──────────────────────────────────────────────────────
    표시수 = {t: sum(1 for d in 개념줄 if d["표시"] == t) for t in ("신규", "기존", "재사용")}
    상위수 = sum(1 for d in 관계줄 if d["관계"] == "상위")
    규범수 = sum(1 for d in 규범줄 if not d["근거추가"])
    빠진블록 = [k for k in 입력 if k not in 블록ID]
    r.집계 = {"개념줄": 표시수, "관계줄": len(관계줄), "상위줄": 상위수, "규범줄": 규범수,
             "규범근거추가줄": len(규범줄) - 규범수, "새개념": sum(새개념.values()), "뺀개념": 뺀개념,
             "이미있던신규": [i for i in 신규 if i in 기존개념], "합친개념": 합친개념, "새관계": sum(새관계.values()),
             "합친관계": 합친관계, "교체관계": 교체관계, "새규범": sum(새규범.values()), "교체규범": 교체규범, "합친규범": 합친규범,
             "이어붙인근거": 이어붙인근거, "빠진블록": 빠진블록, "파일": {}}
    for 이름 in sorted(쓰는파일):
        옛 = json.loads(r.옛글[이름]) if r.옛글.get(이름) else {"개념": [], "관계": [], "규범": []}
        r.집계["파일"][이름] = {k: (len(옛.get(k) or []), len(파일[이름][k])) for k in ("개념", "관계", "규범")}
        r.집계["파일"][이름]["추가분만"] = 추가분만(r.옛글.get(이름) or "", r.새글[이름]) if 이름 in r.새글 else True
    r.기록 = 기록_글(묶음, 원천폴더, 재추출, r.집계, 신규)
    return r


def 추가분만(옛: str, 새: str) -> bool:
    """새 글이 옛 글에 줄을 더하기만 했는가. 목록 끝에 붙일 때 앞 줄 끝에 붙는 `,` 와 빈 목록 `[]` 이 열리는 것만 허용."""
    if not 옛:
        return True
    a, b = 옛.splitlines(), 새.splitlines()
    for 연산, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if 연산 in ("equal", "insert"):
            continue
        if 연산 == "replace" and i2 - i1 == 1:
            옛줄, 새줄들 = a[i1], b[j1:j2]
            if 새줄들[0] == 옛줄 + ",":
                continue
            m = re.fullmatch(r"(\s*.*)\[\](,?)", 옛줄)
            if m and 새줄들[0] == m.group(1) + "[" and 새줄들[-1].strip() == "]" + m.group(2):
                continue
        return False
    return True


def 기록_글(묶음: Path, 원천폴더: Path, 재추출: bool, 집: dict, 신규: dict) -> str:
    t = 집["개념줄"]
    표신규 = t["신규"]
    줄 = [f"# JSON 변환 기록 ({묶음.name})", "",
          f"`scripts/extraction_to_json.py` 가 썼다({datetime.date.today().isoformat()}). 입력 `확정.md` · `입력_원문.md` · "
          f"`입력_문서키.json` · `schema.json`, 원천 `{원천폴더.as_posix()}`, 재추출 {'예' if 재추출 else '아니오'}. "
          "변환기는 판단하지 않는다. 다음은 `ontology_check.py` 다.", "", "## 집계", "", "```",
          f"확정표  개념 줄 {sum(t.values())} (신규 {표신규} · 기존 {t['기존']} · 재사용 {t['재사용']}) · 관계 줄 {집['관계줄']} "
          f"(상위 {집['상위줄']} · 그 밖 {집['관계줄'] - 집['상위줄']}) · 규범 줄 {집['규범줄'] + 집['규범근거추가줄']} "
          f"(규범 {집['규범줄']} · 근거 추가 {집['규범근거추가줄']})",
          f"규범    확정 {집['규범줄']} = 새 행 {집['새규범']} + 교체 {len(집['교체규범'])} · 근거만 합친 원천 규범 {len(집['합친규범'])}",
          f"개념    신규 {표신규} = 새 행 {집['새개념']} + 뺀 개념 {len(집['뺀개념'])} + 원천에 이미 있던 것 {len(집['이미있던신규'])}"
          f" · 근거 · 별칭만 합친 원천 개념 {len(집['합친개념'])}",
          f"관계    상위 {집['상위줄']} → 개념.상위 · 그 밖 {집['관계줄'] - 집['상위줄']} → 새 행 {집['새관계']}"
          f" · 근거만 합친 원천 관계 {len(집['합친관계'])}"]
    for 이름, v in 집["파일"].items():
        줄.append(f"파일    {이름}  " + " · ".join(f"{k} {a}→{b}" for k, (a, b) in ((k, v[k]) for k in ("개념", "관계", "규범")))
                 + f" · 기존 줄을 바꾸지 않음 {'예' if v['추가분만'] else '아니오'}")
    줄 += ["```", ""]
    목록들 = [("뺀 개념 (묶음 안에서 참조되지 않는 신규 주체 · 시간 · 절차 · 증빙)",
             [f"{i} (정식명 {신규[i]['정식명']}, 별칭 {', '.join(신규[i]['별칭']) or '없음'})" for i in 집["뺀개념"]]),
            ("근거 · 별칭만 합친 원천 개념", 집["합친개념"]),
            ("근거만 합친 원천 관계", [f"{s} -{k}-> {d}" for s, k, d, _ in 집["합친관계"]]),
            ("근거만 합친 원천 규범", 집["합친규범"]), ("교체한 규범 (--재추출)", 집["교체규범"]), ("값을 바꾼 관계 (--재추출)", [" ".join(k) for k in 집["교체관계"]]),
            ("이어 붙인 근거 (교체한 규범의 옛 근거 중 이 확정표 블록 밖 조문의 것)",
             [f"{i} 근거.{칸} {조} \"{발}\"" for i, 칸, 조, 발 in 집.get("이어붙인근거", [])]),
            ("확정표에 블록이 없는 입력 조문", 집["빠진블록"])]
    for 제목, 값들 in 목록들:
        if 값들:
            줄 += [f"## {제목}", "", *[f"- `{v}`" for v in 값들], ""]
    return "\n".join(줄)


class 쓰기실패(OSError):
    pass


def 쓰기(r: 변환결과, 묶음: Path, 원천폴더: Path) -> None:
    """바뀐 원천 파일과 JSON_변환기록.md 를 한 묶음으로 쓴다.
    ① 대상이 모두 쓸 수 있는지 먼저 본다 ② 임시 파일에 다 쓴다 ③ 바꿔 끼운다.
    어느 단계든 실패하면 이미 바꾼 파일을 옛 글로 되돌리고(새로 만든 파일은 지운다) 임시 파일을 지운 뒤 쓰기실패 를 낸다."""
    기록경로 = Path(묶음) / "JSON_변환기록.md"
    대상: list[tuple[Path, bytes, bytes | None]] = []        # (경로, 새 글, 옛 글 · 없던 파일은 None)
    for 이름, 글 in r.새글.items():
        경로 = Path(원천폴더) / 이름
        대상.append((경로, 글.encode("utf-8"), 경로.read_bytes() if 경로.exists() else None))
    대상.append((기록경로, r.기록.encode("utf-8"), 기록경로.read_bytes() if 기록경로.exists() else None))
    못씀 = [str(p) for p, _, 옛 in 대상
            if (옛 is not None and not os.access(p, os.W_OK)) or not os.access(p.parent, os.W_OK)]
    if 못씀:
        raise 쓰기실패(f"쓸 수 없는 파일이 있어 아무것도 쓰지 않았다: {', '.join(못씀)}")
    임시들: list[Path] = []
    바꾼: list[tuple[Path, bytes | None]] = []
    try:
        for 경로, 새, _ in 대상:
            임시 = 경로.with_name(경로.name + ".tmp_변환")
            임시들.append(임시)
            임시.write_bytes(새)
        for (경로, _, 옛), 임시 in zip(대상, 임시들):
            os.replace(임시, 경로)
            바꾼.append((경로, 옛))
    except OSError as e:
        되돌림실패 = []
        for 경로, 옛 in reversed(바꾼):
            try:
                if 옛 is None:
                    경로.unlink()
                else:
                    경로.write_bytes(옛)
            except OSError:
                되돌림실패.append(str(경로))
        for 임시 in 임시들:
            try:
                임시.unlink(missing_ok=True)
            except OSError:
                되돌림실패.append(str(임시))
        덧 = f" 되돌리지 못한 파일: {', '.join(되돌림실패)}" if 되돌림실패 else " 바꾼 파일은 옛 글로 되돌렸다."
        raise 쓰기실패(f"쓰다가 실패했다({e}).{덧}") from e


# self-test ─────────────────────────────────────────────────────────────────

_시험_schema = {"스키마": 2, "사업키": {"시험사업": "시험 사업"}, "문서키": {"시험사업": "문서-시험", "지침": "문서-지침"},
               "문서사업": {"시험사업": "시험사업"}, "문서 표시명": {"시험사업": "시험", "지침": "지침"}}
_시험_문서키 = {"사업키": {"시험사업": "시험 사업"}, "문서키": {"시험사업": "문서-시험", "지침": "문서-지침"},
              "문서사업": {"시험사업": "시험사업"}, "문서 표시명": {"시험사업": "시험", "지침": "지침"}}
_시험_원문 = """# 입력 원문 — 시험

각 머리말이 조문 ID 다.

### 조:시험사업#제1조①
<!-- 제1조(여비) -->
제1조(여비) ① “숙박비”는 1인 1박당 표-1에 따라 금액 내 실비 정산하여야 하며 지급 기준
금액을 초과하여 집행할 수 없다. 숙박비가 50만원 이상이면 사전 승인을 받아야 한다.

### 조:시험사업#제1조①표-1
<!-- 제1조(여비) [원문결함] -->
< 표-1 > 국내 숙박비 지급기준

| 구분 | 특별시 | 그 밖의 지역 |
|---|---|---|
| 숙박비 (원) | 100,000 | 70,000 |

### 조:시험사업#제2조
<!-- 제2조(외주) -->
제2조(외주) 외주용역은 외부 업체와 계약하여야 한다. 단, 부득이한 경우 플랫폼을 통하여 대금을 지급한다.
사업 운영 기준은 지침 제42조를 따른다.

### 조:지침#제9조
<!-- 제9조(적용) -->
제9조(적용) 사업별 기준에 명시되지 않은 사항은 이 지침을 따른다.
"""
_시험_블록2 = """### 조:시험사업#제2조
원문: 제2조(외주) 외주용역은 외부 업체와 계약하여야 한다. …
개념: 비목:창업/외주용역비 (신규) | 정식명 외주용역비 | 별칭 외주비 | 적용사업 공통 | 검수단계 AI검증 | 근거 "외주용역은"
관계: 조:시험사업#제2조 -준용-> 조:지침#제42조 [원문명시] 근거 "지침 제42조를 따른다" | 적용사업 시험사업 | 검수단계 AI대조
규범: 규범:시험사업/제2조/의무/1 | 계층 L2 | 대상 비목:창업/외주용역비 | 수범자 주체:진출자 | 조건 {항목: "거래처유형", 연산: "=", 값: "사업자등록증 등록업체"} · 계약 전 | 조건근거 "외부 업체와 계약하여야 한다" | 시점 집행 전 | 결과.문장 "외주용역은 외부 업체와 계약해야 합니다." | 예외 규범:시험사업/제2조/허용/1, "단, 부득이한 경우" | 대상근거 "외주용역은" | 판정 일치 | 검수단계 AI검증
      규범:시험사업/제2조/허용/1 | 계층 L2 | 대상 비목:창업/외주용역비 | 조건 {항목: "거래경로", 연산: "in", 값: ["플랫폼", "마켓"]} · 부득이한 경우 | 조건근거 "단, 부득이한 경우" | 결과.조건문장 "부득이하면 플랫폼을 통해 대금을 지급할 수 있습니다." | 대상근거 "플랫폼을 통하여 대금을 지급한다." | 판정 A | 검수단계 AI대조
모르는 것: 없음
"""
_시험_확정 = """# 확정 추출표 — 시험 (검증자)

머리 글은 변환기가 읽지 않는다. 형식은 docs/5_룰/5-7d_확정표_형식.md.

### 조:시험사업#제1조①
원문: 제1조(여비) ① “숙박비”는 …
      둘째 원문 줄은 들여 쓴다.
개념: 세목:창업/여비/숙박비 (기존) | 근거 "“숙박비”는 1인 1박당"
      품목:창업/호텔@시험사업 (신규) | 정식명 호텔 | 별칭 호텔 숙박, 호텔비 | 적용사업 시험사업 | 검수단계 AI검증 | 근거 "숙박비"
      주체:숙박업체 (신규) | 정식명 숙박업체 | 별칭 없음 | 적용사업 공통 | 검수단계 AI검증 | 근거 "숙박비"
      주체:승인기관 (신규) | 정식명 승인기관 | 별칭 없음 | 적용사업 공통 | 검수단계 AI대조 | 근거 "사전 승인을 받아야 한다"
관계: 품목:창업/호텔@시험사업 -상위-> 세목:창업/여비/숙박비 [해석] 근거 "숙박비" | 검수단계 AI검증 | 판정 일치
      세목:창업/여비/숙박비 -상위-> 비목:창업/여비 [원문명시] 근거 "\\"숙박비\\"는" | (근거 추가) | 검수단계 AI검증
      조:시험사업#제1조① -준용-> 조:시험사업#제1조①표-1 [원문명시] 근거 "표-1에 따라" | 적용사업 시험사업 | 검수단계 AI검증
규범: 규범:시험사업/제1조①/임계값/1 | 계층 L2 | 대상 세목:창업/여비/숙박비 | 값 500000 원 | 발동연산 이상 | 부가세 정의없음 | 결과.활성화 규범:시험사업/제1조①/의무/2 | 수치근거 "숙박비가 50만원 이상이면" | 판정 일치 | 검수단계 AI검증
      규범:시험사업/제1조①/의무/2 | 계층 L2 | 대상 세목:창업/여비/숙박비 | 승인주체 주체:승인기관 | 시점 집행 전 | 결과.문장 "숙박비가 50만원 이상이면 사전 승인을 받아야 합니다." | 대상근거 "사전 승인을 받아야 한다" | 판정 일치 | 검수단계 AI대조 | 해석미결
모르는 것: 승인 주체가 누구인지
용어 후보: 숙박비

### 조:시험사업#제1조①표-1
원문결함: true (발췌는 PDF 복원본)
원문: < 표-1 > 국내 숙박비 지급기준
개념: 세목:창업/여비/숙박비 (재사용) | 근거 "국내 숙박비 지급기준"
관계: 해당 없음
규범: 규범:시험사업/제1조①표-1/한도/1 | 계층 L2 | 대상 세목:창업/여비/숙박비, 품목:창업/호텔@시험사업 | 값 100000 원 | 위반연산 초과 | 분모 인,박 | 부가세 정의없음 | 지역 특별시 | 결과.종류 불가 | 수치근거 "100,000" · 조:시험사업#제1조① "지급 기준 금액을 초과하여 집행할 수 없다." | 조건근거 "특별시" | 대상근거 "국내 숙박비 지급기준" | 판정 일치 | 검수단계 AI검증
      규범:시험사업/제1조①/의무/1 (근거 추가) | 대상근거 "국내 숙박비 지급기준" | 검수단계 AI검증

""" + _시험_블록2 + """
### 조:지침#제9조
개념: 해당 없음
관계: 해당 없음
규범: 규범:공통/지침#제9조/우선순위/1 | 계층 L1 | 대상 비목:창업/* | 조건 {상위: "L1", 하위: "L2", 범위: "unspecified_only", 출처: "규정조항"} | 조건근거 "사업별 기준에 명시되지 않은 사항은 이 지침을 따른다." | 검수단계 AI검증

## 결정 로그

### 블록이 아닌 제목
- `## ` 절은 변환기가 읽지 않는다.
"""


def _시험_원천() -> dict[str, dict]:
    여비근거 = [{"조문": "조:지침#제9조", "발췌": "이 지침"}]
    공통 = {"스키마": 2, "개념": [
        {"id": "비목:창업/여비", "종류": "비목", "정식명": "여비", "별칭": [], "상위": None, "정의": None, "적용사업": None,
         "검수단계": "AI검증", "근거": 여비근거},
        {"id": "비목:창업/재료비", "종류": "비목", "정식명": "재료비", "별칭": [], "상위": None, "정의": None, "적용사업": None,
         "검수단계": "AI검증", "근거": 여비근거},
        {"id": "주체:진출자", "종류": "주체", "정식명": "진출자", "별칭": [], "상위": None, "정의": None, "적용사업": None,
         "검수단계": "AI검증", "근거": 여비근거}], "관계": [], "규범": []}
    사업 = {"스키마": 2, "사업": "시험 사업", "사업키": "시험사업", "개념": [
        {"id": "세목:창업/여비/숙박비", "종류": "세목", "정식명": "숙박비", "별칭": [], "상위": "비목:창업/여비", "정의": None,
         "적용사업": None, "연결종류": "원문명시", "검수단계": "AI검증", "근거": [{"조문": "조:시험사업#제1조①", "발췌": "숙박비"}]}],
        "관계": [{"src": "사업:시험사업", "관계": "사용", "dst": "비목:창업/여비", "연결종류": "원문명시", "적용사업": "시험 사업",
                 "문맥조문": None, "검수단계": "AI검증", "근거": [{"조문": "조:시험사업#제1조①", "발췌": "숙박비"}]}],
        "규범": [{"id": "규범:시험사업/제1조①/의무/1", "종류": "의무", "계층": "L2", "적용사업": "시험 사업",
                 "대상": ["세목:창업/여비/숙박비"], "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []}, "수치": None,
                 "결과": {"문장": "숙박비는 기준 금액 안에서 실제 쓴 만큼만 정산해야 합니다."}, "예외": [], "해석미결": False,
                 "검수단계": "AI검증", "근거": {"수치": [], "조건": [], "대상": [{"조문": "조:시험사업#제1조①", "발췌": "금액 내 실비 정산하여야 하며"}]}}]}
    return {공통_파일: 공통, "시험사업.json": 사업}


_결함 = {"원문결함": True}
_시험_기대 = {
    "공통_L1.json": {
        "개념": [
            {"id": "주체:승인기관", "종류": "주체", "정식명": "승인기관", "별칭": [], "상위": None, "정의": None, "적용사업": None,
             "검수단계": "AI대조", "근거": [{"조문": "조:시험사업#제1조①", "발췌": "사전 승인을 받아야 한다"}]},
            {"id": "비목:창업/외주용역비", "종류": "비목", "정식명": "외주용역비", "별칭": ["외주비"], "상위": None, "정의": None,
             "적용사업": None, "검수단계": "AI검증", "근거": [{"조문": "조:시험사업#제2조", "발췌": "외주용역은"}]}],
        "관계": [],
        "규범": [
            {"id": "규범:공통/지침#제9조/우선순위/1", "종류": "우선순위", "계층": "L1", "적용사업": None, "대상": ["비목:창업/*"],
             "수범자": [], "승인주체": [], "조건": {"상위": "L1", "하위": "L2", "범위": "unspecified_only", "출처": "규정조항"},
             "수치": None, "결과": {}, "예외": [], "해석미결": False, "검수단계": "AI검증",
             "근거": {"수치": [], "조건": [{"조문": "조:지침#제9조", "발췌": "사업별 기준에 명시되지 않은 사항은 이 지침을 따른다."}], "대상": []}}]},
    "시험사업.json": {
        "개념": [
            {"id": "세목:창업/여비/숙박비", "종류": "세목", "정식명": "숙박비", "별칭": [], "상위": "비목:창업/여비", "정의": None,
             "적용사업": None, "연결종류": "원문명시", "검수단계": "AI검증", "근거": [
                 {"조문": "조:시험사업#제1조①", "발췌": "숙박비"}, {"조문": "조:시험사업#제1조①", "발췌": "\"숙박비\"는 1인 1박당"},
                 {"조문": "조:시험사업#제1조①", "발췌": "\"숙박비\"는"},
                 {"조문": "조:시험사업#제1조①표-1", "발췌": "국내 숙박비 지급기준", **_결함}]},
            {"id": "품목:창업/호텔@시험사업", "종류": "품목", "정식명": "호텔", "별칭": ["호텔 숙박", "호텔비"],
             "상위": "세목:창업/여비/숙박비", "정의": None, "적용사업": "시험 사업", "연결종류": "해석", "검수단계": "AI검증",
             "근거": [{"조문": "조:시험사업#제1조①", "발췌": "숙박비"}]}],
        "관계": [
            {"src": "조:시험사업#제1조①", "관계": "준용", "dst": "조:시험사업#제1조①표-1", "연결종류": "원문명시",
             "적용사업": "시험 사업", "문맥조문": None, "검수단계": "AI검증", "근거": [{"조문": "조:시험사업#제1조①", "발췌": "표-1에 따라"}]},
            {"src": "조:시험사업#제2조", "관계": "준용", "dst": "조:지침#제42조", "연결종류": "원문명시", "적용사업": "시험 사업",
             "문맥조문": None, "검수단계": "AI대조", "근거": [{"조문": "조:시험사업#제2조", "발췌": "지침 제42조를 따른다"}]}],
        "규범": [
            {"id": "규범:시험사업/제1조①/의무/1", "종류": "의무", "계층": "L2", "적용사업": "시험 사업", "대상": ["세목:창업/여비/숙박비"],
             "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []}, "수치": None,
             "결과": {"문장": "숙박비는 기준 금액 안에서 실제 쓴 만큼만 정산해야 합니다."}, "예외": [], "해석미결": False,
             "검수단계": "AI검증", "근거": {"수치": [], "조건": [], "대상": [
                 {"조문": "조:시험사업#제1조①", "발췌": "금액 내 실비 정산하여야 하며"},
                 {"조문": "조:시험사업#제1조①표-1", "발췌": "국내 숙박비 지급기준", **_결함}]}},
            {"id": "규범:시험사업/제1조①/임계값/1", "종류": "임계값", "계층": "L2", "적용사업": "시험 사업",
             "대상": ["세목:창업/여비/숙박비"], "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []},
             "수치": {"값": 500000, "단위": "원", "발동연산": "이상", "분모": [], "분모주체": None, "누적범위": None,
                    "부가세": "정의없음", "지역": None},
             "결과": {"활성화": ["규범:시험사업/제1조①/의무/2"]}, "예외": [], "해석미결": False, "검수단계": "AI검증",
             "근거": {"수치": [{"조문": "조:시험사업#제1조①", "발췌": "숙박비가 50만원 이상이면"}], "조건": [], "대상": []}},
            {"id": "규범:시험사업/제1조①/의무/2", "종류": "의무", "계층": "L2", "적용사업": "시험 사업",
             "대상": ["세목:창업/여비/숙박비"], "수범자": [], "승인주체": ["주체:승인기관"], "조건": {"구조화": [], "자연어": []},
             "수치": None, "시점": "집행 전", "결과": {"문장": "숙박비가 50만원 이상이면 사전 승인을 받아야 합니다."}, "예외": [],
             "해석미결": True, "검수단계": "AI대조",
             "근거": {"수치": [], "조건": [], "대상": [{"조문": "조:시험사업#제1조①", "발췌": "사전 승인을 받아야 한다"}]}},
            {"id": "규범:시험사업/제1조①표-1/한도/1", "종류": "한도", "계층": "L2", "적용사업": "시험 사업",
             "대상": ["세목:창업/여비/숙박비", "품목:창업/호텔@시험사업"], "수범자": [], "승인주체": [],
             "조건": {"구조화": [], "자연어": []},
             "수치": {"값": 100000, "단위": "원", "위반연산": "초과", "분모": ["인", "박"], "분모주체": None, "누적범위": None,
                    "부가세": "정의없음", "지역": "특별시"},
             "결과": {"종류": "불가"}, "예외": [], "해석미결": False, "검수단계": "AI검증",
             "근거": {"수치": [{"조문": "조:시험사업#제1조①표-1", "발췌": "100,000", **_결함},
                             {"조문": "조:시험사업#제1조①", "발췌": "지급 기준 금액을 초과하여 집행할 수 없다."}],
                    "조건": [{"조문": "조:시험사업#제1조①표-1", "발췌": "특별시", **_결함}],
                    "대상": [{"조문": "조:시험사업#제1조①표-1", "발췌": "국내 숙박비 지급기준", **_결함}]}},
            {"id": "규범:시험사업/제2조/의무/1", "종류": "의무", "계층": "L2", "적용사업": "시험 사업", "대상": ["비목:창업/외주용역비"],
             "수범자": ["주체:진출자"], "승인주체": [],
             "조건": {"구조화": [{"항목": "거래처유형", "연산": "=", "값": "사업자등록증 등록업체"}], "자연어": ["계약 전"]},
             "수치": None, "시점": "집행 전", "결과": {"문장": "외주용역은 외부 업체와 계약해야 합니다."},
             "예외": ["규범:시험사업/제2조/허용/1", "단, 부득이한 경우"], "해석미결": False, "검수단계": "AI검증",
             "근거": {"수치": [], "조건": [{"조문": "조:시험사업#제2조", "발췌": "외부 업체와 계약하여야 한다"}],
                    "대상": [{"조문": "조:시험사업#제2조", "발췌": "외주용역은"}]}},
            {"id": "규범:시험사업/제2조/허용/1", "종류": "허용", "계층": "L2", "적용사업": "시험 사업", "대상": ["비목:창업/외주용역비"],
             "수범자": [], "승인주체": [],
             "조건": {"구조화": [{"항목": "거래경로", "연산": "in", "값": ["플랫폼", "마켓"]}], "자연어": ["부득이한 경우"]},
             "수치": None, "결과": {"조건문장": "부득이하면 플랫폼을 통해 대금을 지급할 수 있습니다."}, "예외": [], "해석미결": False,
             "검수단계": "AI대조",
             "근거": {"수치": [], "조건": [{"조문": "조:시험사업#제2조", "발췌": "단, 부득이한 경우"}],
                    "대상": [{"조문": "조:시험사업#제2조", "발췌": "플랫폼을 통하여 대금을 지급한다."}]}}]}}

# 실제 데이터 재현: 1_A_제34-37조 확정.md 의 3행을 이 형식으로 옮긴 것. 입력은 그 묶음 입력_원문.md 의 블록 그대로다.
_재현_schema = {"스키마": 2, "사업키": {"모두의창업": "모두의 창업 프로젝트"},
               "문서키": {"모두의창업": "모두의 창업 프로젝트 세부관리기준(개정본)"}, "문서 표시명": {"모두의창업": "모두의 창업"}}
_재현_원문 = """# 입력 원문 — 1_A_제34-37조 (재현에 쓰는 블록만)

### 조:모두의창업#제34조①
<!-- 제34조(재료비·기계장치비) -->
제34조(재료비·기계장치비) ① 재료비는 시제품 제작에 소요되는 재료(원료), 데
이터 등 구입비용으로 협약기간 내 납품 및 사용이 완료될 경우에 한해 집행이
가능하다.

### 조:모두의창업#제34조③
<!-- 제34조(재료비·기계장치비) -->
③ 시제품 제작에 직‧간접적으로 활용되는 기구, 비품, 기계 등의 구입이 가능
하나 취득가액이 500만원 이상 구입할 경우, 사업운영위원회 사전심의를 받은
건에 한하여 집행이 가능하다.

### 조:모두의창업#제35조①
<!-- 제35조(외주용역비) -->
제35조(외주용역비) ① 외주용역비는 시제품 제작 및 사업화를 위해 외부 업체
(사업자등록증등록업체)와 계약 등을 통한 방식으로 시제품제작, 시장조사, 연구
개발 등을 시행 시 집행할 수 있다. 프리랜서 마켓을 이용할 경우 사업자등록증
등록업체와의 계약을 통한 집행만 가능하다. 단, 부득이하게 프리랜서 마켓에
등록된 개인을 통해 과업을 수행할 경우, 해당 플랫폼을 통하여서만 거래 대금
지급이 가능하다.

### 조:모두의창업#제37조④
<!-- 제37조(여비) -->
④ “숙박비“는 1인 1박당 표-6에 따라 금액 내 실비 정산하여야 하며 지급 기준
금액을 초과하여 집행할 수 없다.

### 조:모두의창업#제37조④표-6
<!-- 제37조(여비) [원문결함] -->
< 표-6 > 국내 숙박비 지급기준(한도 내 실비정산)

| 구분 | 특별시 | 광역시 | 그 박의 지역 |
|---|---|---|---|
| 국내 숙박비 (원) | 100,000 | 80,000 | 70,000 |
"""
_재현_확정 = """# 확정 추출표 — 재현

### 조:모두의창업#제34조③
개념: 해당 없음
관계: 해당 없음
규범: 규범:모두의창업/제34조③/임계값/1 | 계층 L2 | 대상 비목:창업/기계장치, 품목:창업/기구, 품목:창업/비품, 품목:창업/기계 | 값 5000000 원 | 발동연산 이상 | 부가세 정의없음 | 결과.활성화 규범:모두의창업/제34조③/의무/1 | 수치근거 "취득가액이 500만원 이상 구입할 경우" | 대상근거 "기구, 비품, 기계 등의 구입이 가능" · 조:모두의창업#제34조① "제34조(재료비·기계장치비)" | 판정 수정 | 검수단계 AI대조

### 조:모두의창업#제35조①
개념: 해당 없음
관계: 해당 없음
규범: 규범:모두의창업/제35조①/의무/1 | 계층 L2 | 대상 비목:창업/외주용역비 | 조건 {항목: "거래처유형", 연산: "=", 값: "사업자등록증 등록업체"} | 시점 집행 전 | 결과.문장 "외주용역은 외부 업체(사업자등록증 등록업체)와 계약 등을 맺어 진행해야 합니다." | 대상근거 "외부 업체 (사업자등록증등록업체)와 계약 등을 통한 방식으로" | 판정 B | 검수단계 AI검증

### 조:모두의창업#제37조④표-6
원문결함: true (이 블록의 모든 발췌는 입력_원문.md 의 PDF 복원본)
개념: 해당 없음
관계: 해당 없음
규범: 규범:모두의창업/제37조④표-6/한도/1 | 계층 L2 | 대상 세목:창업/여비/숙박비 | 값 100000 원 | 위반연산 초과 | 분모 인,박 | 부가세 정의없음 | 지역 특별시 | 결과.종류 불가 | 수치근거 "100,000" · 조:모두의창업#제37조④ "지급 기준 금액을 초과하여 집행할 수 없다." | 대상근거 "국내 숙박비 지급기준(한도 내 실비정산)" | 조건근거 "특별시" | 판정 일치 | 검수단계 AI검증
"""
# 기대값: ontology/모두의창업.json 의 같은 ID 행(2026-11-08 작업 트리)을 그대로 옮겨 고정했다.
_재현_기대 = json.loads(r"""[
{"id": "규범:모두의창업/제34조③/임계값/1", "종류": "임계값", "계층": "L2", "적용사업": "모두의 창업 프로젝트", "대상": ["비목:창업/기계장치", "품목:창업/기구", "품목:창업/비품", "품목:창업/기계"], "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []}, "수치": {"값": 5000000, "단위": "원", "발동연산": "이상", "분모": [], "분모주체": null, "누적범위": null, "부가세": "정의없음", "지역": null}, "결과": {"활성화": ["규범:모두의창업/제34조③/의무/1"]}, "예외": [], "해석미결": false, "검수단계": "AI대조", "근거": {"수치": [{"조문": "조:모두의창업#제34조③", "발췌": "취득가액이 500만원 이상 구입할 경우"}], "조건": [], "대상": [{"조문": "조:모두의창업#제34조③", "발췌": "기구, 비품, 기계 등의 구입이 가능"}, {"조문": "조:모두의창업#제34조①", "발췌": "제34조(재료비·기계장치비)"}]}},
{"id": "규범:모두의창업/제35조①/의무/1", "종류": "의무", "계층": "L2", "적용사업": "모두의 창업 프로젝트", "대상": ["비목:창업/외주용역비"], "수범자": [], "승인주체": [], "조건": {"구조화": [{"항목": "거래처유형", "연산": "=", "값": "사업자등록증 등록업체"}], "자연어": []}, "수치": null, "시점": "집행 전", "결과": {"문장": "외주용역은 외부 업체(사업자등록증 등록업체)와 계약 등을 맺어 진행해야 합니다."}, "예외": [], "해석미결": false, "검수단계": "AI검증", "근거": {"수치": [], "조건": [], "대상": [{"조문": "조:모두의창업#제35조①", "발췌": "외부 업체 (사업자등록증등록업체)와 계약 등을 통한 방식으로"}]}},
{"id": "규범:모두의창업/제37조④표-6/한도/1", "종류": "한도", "계층": "L2", "적용사업": "모두의 창업 프로젝트", "대상": ["세목:창업/여비/숙박비"], "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []}, "수치": {"값": 100000, "단위": "원", "위반연산": "초과", "분모": ["인", "박"], "분모주체": null, "누적범위": null, "부가세": "정의없음", "지역": "특별시"}, "결과": {"종류": "불가"}, "예외": [], "해석미결": false, "검수단계": "AI검증", "근거": {"수치": [{"조문": "조:모두의창업#제37조④표-6", "발췌": "100,000", "원문결함": true}, {"조문": "조:모두의창업#제37조④", "발췌": "지급 기준 금액을 초과하여 집행할 수 없다."}], "조건": [{"조문": "조:모두의창업#제37조④표-6", "발췌": "특별시", "원문결함": true}], "대상": [{"조문": "조:모두의창업#제37조④표-6", "발췌": "국내 숙박비 지급기준(한도 내 실비정산)", "원문결함": true}]}}
]""")


def _재현_원천() -> dict[str, dict]:
    def 개념(i, 종류, 이름, 상위=None):
        c = {"id": i, "종류": 종류, "정식명": 이름, "별칭": [], "상위": 상위, "정의": None, "적용사업": None}
        if 상위:
            c["연결종류"] = "원문명시"
        c.update(검수단계="AI검증", 근거=[{"조문": "조:모두의창업#제34조①", "발췌": "재료비"}])
        return c
    공통 = {"스키마": 2, "개념": [개념("비목:창업/기계장치", "비목", "기계장치"), 개념("비목:창업/외주용역비", "비목", "외주용역비"),
                               개념("비목:창업/여비", "비목", "여비")], "관계": [], "규범": []}
    사업 = {"스키마": 2, "사업": "모두의 창업 프로젝트", "사업키": "모두의창업", "개념": [
        개념(f"품목:창업/{x}", "품목", x, "비목:창업/기계장치") for x in ("기구", "비품", "기계")] + [
        개념("세목:창업/여비/숙박비", "세목", "숙박비", "비목:창업/여비")], "관계": [], "규범": [
        {"id": "규범:모두의창업/제34조③/의무/1", "종류": "의무", "계층": "L2", "적용사업": "모두의 창업 프로젝트",
         "대상": ["비목:창업/기계장치"], "수범자": [], "승인주체": [], "조건": {"구조화": [], "자연어": []}, "수치": None,
         "결과": {"문장": "사전심의를 받은 건만 집행할 수 있습니다."}, "예외": [], "해석미결": False, "검수단계": "AI대조",
         "근거": {"수치": [], "조건": [], "대상": [{"조문": "조:모두의창업#제34조③", "발췌": "사업운영위원회 사전심의를 받은"}]}}]}
    return {공통_파일: 공통, "모두의창업.json": 사업}


def _행키(x: dict):
    return x.get("id") or (x.get("src"), x.get("관계"), x.get("dst"))


def _시험_폴더(임시: Path, 원천: dict, schema: dict, 확정: str, 원문: str, 문서키: dict) -> tuple[Path, Path]:
    원천폴더 = 임시 / "원천"
    묶음 = 원천폴더 / "추출" / "시험묶음"          # ontology_check 의 복원본(추출/**/입력_원문.md) 자리
    묶음.mkdir(parents=True)
    (원천폴더 / "schema.json").write_text(JSON_글(schema), encoding="utf-8")
    for 이름, v in 원천.items():
        (원천폴더 / 이름).write_bytes(JSON_글(v).encode("utf-8"))
    (묶음 / "확정.md").write_text(확정, encoding="utf-8")
    (묶음 / "입력_원문.md").write_text(원문, encoding="utf-8")
    (묶음 / "입력_문서키.json").write_text(JSON_글(문서키), encoding="utf-8")
    return 묶음, 원천폴더


def _해시들(폴더: Path) -> dict[str, str]:
    return {str(p.relative_to(폴더)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(폴더).rglob("*")) if p.is_file()}


def _실행(확정: str, 재추출: bool = False, 원천: dict | None = None, schema: dict | None = None, 원문: str | None = None,
         문서키: dict | None = None, 쓰기도: bool = True):
    """임시 폴더에 고정 입력을 만들고 main 과 같은 순서로 돌린다. 반환 (결과, 묶음, 원천폴더, 전 해시, 후 해시, 임시)."""
    임시 = tempfile.TemporaryDirectory()
    묶음, 원천폴더 = _시험_폴더(Path(임시.name), 원천 or _시험_원천(), schema or _시험_schema, 확정, 원문 or _시험_원문,
                          문서키 or _시험_문서키)
    전 = _해시들(원천폴더)
    r = 변환(묶음, 원천폴더, 재추출)
    if r.통과 and 쓰기도:
        쓰기(r, 묶음, 원천폴더)
    return r, 묶음, 원천폴더, 전, _해시들(원천폴더), 임시


def _자체시험() -> int:
    결과들: list[tuple[str, bool, str]] = []

    def 확인(이름: str, 참: bool, 설명: str = "") -> None:
        결과들.append((이름, bool(참), 설명))

    # 1 · 2 · 3 정상 입력
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정)
    with 임시:
        확인("정상 입력이 통과한다", r.통과, "; ".join(map(str, r.문제들)))
        if r.통과:
            틀림 = []
            for 이름, 기대 in _시험_기대.items():
                실제 = json.loads((원천폴더 / 이름).read_text(encoding="utf-8"))
                바탕 = _시험_원천()[이름]
                for k in ("개념", "관계", "규범"):
                    # 기대값에는 고친 기존 행(숙박비 개념 · 의무/1)과 새 행이 있다. 나머지 기존 행은 그대로여야 한다.
                    덧 = {_행키(x): x for x in 기대[k]}
                    바탕키 = [_행키(y) for y in 바탕[k]]
                    기대행 = [덧.get(키, y) for 키, y in zip(바탕키, 바탕[k])] + [x for 키, x in 덧.items() if 키 not in 바탕키]
                    if 실제[k] != 기대행:
                        틀림.append(f"{이름} {k}: {json.dumps(실제[k], ensure_ascii=False)[:300]}")
                    elif json.dumps(실제[k], ensure_ascii=False) != json.dumps(기대행, ensure_ascii=False):
                        틀림.append(f"{이름} {k}: 키 순서가 다르다")
            확인("정상 입력 → 기대 JSON 행과 정확히 일치(개념 · 관계 · 규범 전 칸 · 키 순서)", not 틀림, "; ".join(틀림))
            확인("참조되지 않는 신규 주체는 넣지 않고 기록에 남긴다",
                 r.집계["뺀개념"] == ["주체:숙박업체"] and "주체:숙박업체" in (묶음 / "JSON_변환기록.md").read_text(encoding="utf-8"),
                 str(r.집계["뺀개념"]))
            기록 = (묶음 / "JSON_변환기록.md").read_text(encoding="utf-8")
            확인("변환기록 집계: 규범 확정 6 = 새 행 6 + 교체 0 · 신규 개념 4 = 새 행 3 + 뺀 1 + 원천에 있던 것 0",
                 "규범    확정 6 = 새 행 6 + 교체 0" in 기록 and "신규 4 = 새 행 3 + 뺀 개념 1 + 원천에 이미 있던 것 0" in 기록,
                 기록.split("```")[1] if "```" in 기록 else 기록)
            import ontology_check as oc
            검사 = oc.검사결과()
            원천 = oc.원천_읽기(원천폴더)
            oc.코드_검사(원천, 검사, None)
            확인("결과가 ontology_check.코드_검사(DB 없음 · 원문결함은 복원본 대조)를 통과한다", 검사.통과,
                 "; ".join(f"{e['코드']} {e['위치']} {e['내용']}" for e in 검사.오류))

    # 4 추가만 하는 입력은 기존 줄을 바꾸지 않는다(바이트 차이 = 추가분)
    추가확정 = "# 추가만\n\n" + _시험_블록2
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(추가확정)
    with 임시:
        옛 = {이름: JSON_글(v) for 이름, v in _시험_원천().items()}
        새 = {이름: (원천폴더 / 이름).read_text(encoding="utf-8") for 이름 in 옛}
        확인("기존 행을 바꾸지 않으면 바이트 차이가 추가분뿐이다",
             r.통과 and all(추가분만(옛[n], 새[n]) for n in 옛) and all(새[n] != 옛[n] for n in 옛)
             and not 추가분만(옛["시험사업.json"], 새["시험사업.json"].replace('"AI검증"', '"AI대조"', 1)),
             "; ".join(map(str, r.문제들)))

    # 4b 사업 파일이 없으면 머리(스키마 · 사업 · 사업키)를 갖춰 만든다
    공통만 = {공통_파일: _시험_원천()[공통_파일]}
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(추가확정, 원천=공통만)
    with 임시:
        ok = r.통과 and (원천폴더 / "시험사업.json").exists()
        if ok:
            새파일 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))
            ok = list(새파일) == ["스키마", "사업", "사업키", "개념", "관계", "규범"] and 새파일["사업"] == "시험 사업" \
                and 새파일["스키마"] == 2 and [n["id"] for n in 새파일["규범"]] == ["규범:시험사업/제2조/의무/1", "규범:시험사업/제2조/허용/1"]
        확인("사업 파일이 없으면 머리를 갖춰 새로 만든다", ok, "; ".join(map(str, r.문제들)))

    # 5 실제 데이터 재현
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_재현_확정, 원천=_재현_원천(), schema=_재현_schema, 원문=_재현_원문,
                                   문서키={"사업키": _재현_schema["사업키"], "문서키": _재현_schema["문서키"]})
    with 임시:
        if r.통과:
            실제 = json.loads((원천폴더 / "모두의창업.json").read_text(encoding="utf-8"))["규범"][1:]
            같음 = json.dumps(실제, ensure_ascii=False) == json.dumps(_재현_기대, ensure_ascii=False)
            확인("1_A 실제 3행(한도 · 임계값+활성화 · 구조화 조건 의무)이 모두의창업.json 행과 같은 JSON", 같음,
                 json.dumps(실제, ensure_ascii=False)[:400])
        else:
            확인("1_A 실제 3행 재현", False, "; ".join(map(str, r.문제들)))

    # 6 실패 사례: 옮길 수 없으면 종료 1 · 이유 · 파일 그대로
    한도줄 = "| 판정 일치 | 검수단계 AI검증\n      규범:시험사업/제1조①/의무/2"
    실패사례 = [
        ("모르는 칸", _시험_확정.replace(한도줄, "| 위치 12-2 가(1) " + 한도줄), False, "모르는 칸: 위치"),
        ("칸 없는 근거", _시험_확정.replace('수치근거 "숙박비가 50만원', '근거 "숙박비가 50만원'), False, "칸 없는 근거"),
        ("신규 개념 정식명 없음", _시험_확정.replace("| 정식명 호텔 ", ""), False, "필수 칸이 없다: 정식명"),
        ("입력에 없는 발췌", _시험_확정.replace("숙박비가 50만원 이상이면\"", "숙박비가 60만원 이상이면\""), False, "입력 원문에 없는 발췌"),
        ("입력에 없는 조문(블록)", _시험_확정.replace("## 결정 로그", "### 조:시험사업#제9조\n규범: 해당 없음\n\n## 결정 로그"), False,
         "입력에 없는 조문: 조:시험사업#제9조"),
        ("입력에 없는 조문(근거)", _시험_확정.replace('· 조:시험사업#제1조① "지급', '· 조:시험사업#제8조 "지급'), False, "입력에 없는 조문"),
        ("같은 규범 ID(--재추출 없음)", _시험_확정.replace("규범:시험사업/제1조①/의무/2", "규범:시험사업/제1조①/의무/1"), False,
         "같은 규범 ID 가 원천에 있다"),
        ("상위 충돌", _시험_확정.replace("관계: 해당 없음\n규범: 규범:시험사업/제1조①표-1",
                                    "관계: 세목:창업/여비/숙박비 -상위-> 비목:창업/재료비 [원문명시] 근거 \"숙박비\" | 검수단계 AI검증\n"
                                    "규범: 규범:시험사업/제1조①표-1"), False, "상위 충돌"),
        ("`…` 표기", _시험_확정.replace("대상 세목:창업/여비/숙박비 | 값 500000", "대상 세목:…/숙박비 | 값 500000"), False, "생략 표기"),
        ("`결과 의무/1` 줄임", _시험_확정.replace("| 결과.활성화 규범:시험사업/제1조①/의무/2", "| 결과 의무/2"), False, "줄임은 쓰지 않는다"),
        ("검수단계 값 틀림", _시험_확정.replace("판정 일치 | 검수단계 AI대조 | 해석미결", "판정 일치 | 검수단계 검수중 | 해석미결"), False,
         "검수단계 값이 틀리다"),
        ("결과.문장 안의 `|`", _시험_확정.replace("사전 승인을 받아야 합니다.\"", "사전 승인을 | 받아야 합니다.\""), False, "표 구분자"),
        ("한도의 결과.종류 자유 글", _시험_확정.replace("| 결과.종류 불가 |", "| 결과.종류 집행불가 |"), False, "결과.종류 값이 틀리다"),
        ("별칭의 괄호 짝", _시험_확정.replace("별칭 호텔 숙박, 호텔비", "별칭 호텔(숙박, 호텔비"), False, "괄호가 짝이"),
        ("다른 사업 전용 개념을 대상으로", _시험_확정.replace("| 대상 세목:창업/여비/숙박비 | 값 500000", "| 대상 증빙:다른사업증빙 | 값 500000"), False, "다른 사업 전용 개념"),
        ("(근거 추가) 관계인데 원천에 없음", _시험_확정.replace("[원문명시] 근거 \"표-1에 따라\" | 적용사업",
                                                  "[원문명시] 근거 \"표-1에 따라\" | (근거 추가) | 적용사업"), False, "(근거 추가) 인데"),
        ("규범 줄에 적용사업 칸", _시험_확정.replace("| 계층 L2 | 대상 비목:창업/외주용역비 | 수범자", "| 계층 L2 | 적용사업 시험사업 | 대상 비목:창업/외주용역비 | 수범자"),
         False, "규범 ID 에서 푼다"),
        ("같은 칸 두 번", _시험_확정.replace("| 판정 일치 | 검수단계 AI대조 | 해석미결", "| 판정 일치 | 판정 일치 | 검수단계 AI대조 | 해석미결"),
         False, "같은 칸이 두 번"),
        ("예외 단서 발췌가 입력 원문에 없음", _시험_확정.replace('허용/1, "단, 부득이한 경우"', '허용/1, "단, 언제든지"'), False,
         "입력 원문에 없는 단서 발췌"),
        ("(재사용) 인데 원천에도 표에도 없음", _시험_확정.replace("세목:창업/여비/숙박비 (재사용)", "세목:창업/여비/없는세목 (재사용)"), False,
         "(재사용) 라는데 원천에도 이 표의 (신규) 에도 없다"),
        ("(기존) 인데 원천에 없음", _시험_확정.replace("세목:창업/여비/숙박비 (기존)", "세목:창업/여비/없는세목 (기존)"), False,
         "(기존) 은 원천에 있어야 한다"),
        ("(기존) 이 이 표의 (신규) 를 가리킴", _시험_확정.replace("세목:창업/여비/숙박비 (재사용)", "품목:창업/호텔@시험사업 (기존)"), False,
         "(기존) 은 원천에 있어야 한다"),
        ("원문결함 표시가 확정에만 있음", _시험_확정.replace("### 조:시험사업#제2조\n원문:", "### 조:시험사업#제2조\n원문결함: true\n원문:"), False,
         "확정표는 원문결함이라는데"),
        ("원문결함 표시가 입력에만 있음", _시험_확정.replace("원문결함: true (발췌는 PDF 복원본)\n", ""), False,
         "`원문결함: true` 줄이 없다"),
        ("결과.활성화 가 가리키는 규범이 없음", _시험_확정.replace("결과.활성화 규범:시험사업/제1조①/의무/2", "결과.활성화 규범:시험사업/제1조①/의무/9"),
         False, "가리키는 규범이 원천에도 표에도 없다"),
        ("수치 단위 값 틀림", _시험_확정.replace("값 500000 원", "값 500000 달러"), False, "단위 값이 틀리다"),
        ("관계 dst 가 원천에도 표에도 없음", _시험_확정.replace(
            '근거 "표-1에 따라" | 적용사업 시험사업 | 검수단계 AI검증',
            '근거 "표-1에 따라" | 적용사업 시험사업 | 검수단계 AI검증\n'
            '      사업:시험사업 -사용-> 비목:창업/없는비목 [원문명시] 근거 "숙박비" | 적용사업 시험사업 | 검수단계 AI검증'), False,
         "dst 가 원천에도 이 표의 (신규) 에도 없다"),
        ("관계 src 가 원천에도 표에도 없음", _시험_확정.replace(
            '근거 "표-1에 따라" | 적용사업 시험사업 | 검수단계 AI검증',
            '근거 "표-1에 따라" | 적용사업 시험사업 | 검수단계 AI검증\n'
            '      품목:창업/없는품목 -사용-> 비목:창업/여비 [원문명시] 근거 "숙박비" | 적용사업 시험사업 | 검수단계 AI검증'), False,
         "src 가 원천에도 이 표의 (신규) 에도 없다"),
    ]
    파일그대로 = []
    다른사업_원천 = {**_시험_원천(), "다른사업.json": {
        "스키마": 2, "사업": "다른 사업", "사업키": "다른사업", "관계": [], "규범": [], "개념": [
            {"id": "증빙:다른사업증빙", "종류": "증빙", "정식명": "다른사업증빙", "별칭": [], "상위": None, "정의": None,
             "적용사업": "다른 사업", "검수단계": "AI검증", "근거": [{"조문": "조:지침#제9조", "발췌": "이 지침"}]},
            {"id": "증빙:공통증빙", "종류": "증빙", "정식명": "공통증빙", "별칭": [], "상위": None, "정의": None,
             "적용사업": None, "검수단계": "AI검증", "근거": [{"조문": "조:지침#제9조", "발췌": "이 지침"}]}]}}
    다른사업_schema = {**_시험_schema, "사업키": {**_시험_schema["사업키"], "다른사업": "다른 사업"}}
    for 이름, 확정, 재, 기대 in 실패사례:
        r, 묶음, 원천폴더, 전, 후, 임시 = _실행(확정, 재, 원천=다른사업_원천, schema=다른사업_schema)
        with 임시:
            찾음 = any(기대 in str(p) for p in r.문제들)
            확인(f"실패: {이름}", not r.통과 and 찾음, "; ".join(map(str, r.문제들)) or "통과해 버렸다")
            파일그대로.append(전 == 후 and not (묶음 / "JSON_변환기록.md").exists())
    확인(f"실패 때 원천 · 기록 파일이 그대로다({len(파일그대로)}건)", all(파일그대로))

    # 7 여러 실패를 한 번에 모아 낸다 · 종료 코드 1
    둘 = _시험_확정.replace('수치근거 "숙박비가 50만원', '근거 "숙박비가 50만원').replace("| 정식명 호텔 ", "")
    with tempfile.TemporaryDirectory() as t:
        묶음, 원천폴더 = _시험_폴더(Path(t), _시험_원천(), _시험_schema, 둘, _시험_원문, _시험_문서키)
        코드 = main([str(묶음), "--쓰기", "--원천", str(원천폴더), "--조용히"])
        r = 변환(묶음, 원천폴더)
    확인("실패를 전부 모아 내고 종료 코드 1", 코드 == 1 and len(r.문제들) >= 2, f"코드 {코드}, 문제 {len(r.문제들)}")

    # 8 --재추출: 같은 ID 를 제자리에서 교체하고 목록을 남긴다
    교체 = _시험_확정.replace(
        "      규범:시험사업/제1조①/의무/1 (근거 추가) | 대상근거 \"국내 숙박비 지급기준\" | 검수단계 AI검증\n",
        "      규범:시험사업/제1조①/의무/1 | 계층 L2 | 대상 세목:창업/여비/숙박비 | 결과.문장 \"숙박비는 표의 금액 안에서 정산합니다.\" | 대상근거 \"국내 숙박비 지급기준\" | 검수단계 AI검증\n")
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(교체, True)
    with 임시:
        ok = r.통과
        if ok:
            규범 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["규범"]
            ok = 규범[0]["id"] == "규범:시험사업/제1조①/의무/1" and 규범[0]["결과"]["문장"] == "숙박비는 표의 금액 안에서 정산합니다." \
                and r.집계["교체규범"] == ["규범:시험사업/제1조①/의무/1"] and "교체한 규범" in (묶음 / "JSON_변환기록.md").read_text(encoding="utf-8")
        확인("--재추출 은 같은 규범 ID 를 제자리에서 교체하고 기록한다", ok, "; ".join(map(str, r.문제들)))

    # 8b --재추출: 같은 관계의 검수단계를 표 값으로 바꾸고 기록한다. 재추출이 아니면 실패다
    관계원천 = _시험_원천()
    관계원천["시험사업.json"]["관계"].append(
        {"src": "조:시험사업#제1조①", "관계": "준용", "dst": "조:시험사업#제1조①표-1", "연결종류": "원문명시", "적용사업": "시험 사업",
         "문맥조문": None, "검수단계": "AI대조", "근거": [{"조문": "조:시험사업#제1조①", "발췌": "표-1에 따라"}]})
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(교체, True, 원천=관계원천)
    with 임시:
        ok = r.통과
        if ok:
            관계 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["관계"]
            행 = next(x for x in 관계 if x["src"] == "조:시험사업#제1조①" and x["관계"] == "준용")
            ok = 행["검수단계"] == "AI검증" and r.집계["교체관계"] and "값을 바꾼 관계" in (묶음 / "JSON_변환기록.md").read_text(encoding="utf-8")
        확인("--재추출 은 같은 관계의 검수단계를 표 값으로 바꾸고 기록한다", ok, "; ".join(map(str, r.문제들)))
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정, False, 원천=관계원천)
    with 임시:
        확인("재추출이 아니면 값이 다른 같은 관계는 실패한다", not r.통과 and any("같은 관계가 있는데" in str(x) for x in r.문제들) and 전 == 후)

    # 9 쓰지 않으면 파일을 고치지 않는다
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정, 쓰기도=False)
    with 임시:
        확인("--쓰기 없이는 파일을 고치지 않는다", r.통과 and 전 == 후 and r.새글)

    # 10 따옴표 안의 ` | ` 는 칸을 가르지 않는다(표 행 발췌)
    표행 = _시험_확정.replace('수치근거 "100,000"', '수치근거 "숙박비 (원) | 100,000"')
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(표행)
    with 임시:
        발췌 = ""
        if r.통과:
            규범 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["규범"]
            발췌 = next(n for n in 규범 if n["id"] == "규범:시험사업/제1조①표-1/한도/1")["근거"]["수치"][0]["발췌"]
        확인("표 행 발췌를 따옴표 안의 ` | ` 까지 그대로 옮긴다", 발췌 == "숙박비 (원) | 100,000", "; ".join(map(str, r.문제들)) or 발췌)

    # 11 괄호 안 쉼표는 별칭을 가르지 않는다
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정.replace("별칭 호텔 숙박, 호텔비", "별칭 호텔(숙박, 호텔비), 호텔"))
    with 임시:
        별칭 = []
        if r.통과:
            개념 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["개념"]
            별칭 = next(c for c in 개념 if c["id"] == "품목:창업/호텔@시험사업")["별칭"]
        확인("괄호 안 쉼표는 별칭을 가르지 않는다", 별칭 == ["호텔(숙박, 호텔비)", "호텔"], "; ".join(map(str, r.문제들)) or str(별칭))

    # 11b 조문 키에 쉼표가 있어도 ID 하나다(예비 붙임2 `기계장치(공구·기구,비품,SW등)`)
    확인("괄호 안 쉼표는 ID 목록을 가르지 않는다",
         _ID들("규범:시험사업/붙임2/기계장치(공구·기구,비품,SW등)/의무/1, 규범:시험사업/제1조/의무/1")
         == ["규범:시험사업/붙임2/기계장치(공구·기구,비품,SW등)/의무/1", "규범:시험사업/제1조/의무/1"])

    # 12 묶음 밖 파일의 공통 개념에는 근거를 합치고, 그 사업 전용 행은 그대로 둔다
    공통근거 = _시험_확정.replace('개념: 세목:창업/여비/숙박비 (기존) | 근거 "“숙박비”는 1인 1박당"',
                            '개념: 세목:창업/여비/숙박비 (기존) | 근거 "“숙박비”는 1인 1박당"\n      증빙:공통증빙 (기존) | 근거 "사전 승인을"')
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(공통근거, 원천=다른사업_원천, schema=다른사업_schema)
    with 임시:
        ok = r.통과
        if ok:
            다른 = json.loads((원천폴더 / "다른사업.json").read_text(encoding="utf-8"))
            공통행 = next(c for c in 다른["개념"] if c["id"] == "증빙:공통증빙")
            전용행 = next(c for c in 다른["개념"] if c["id"] == "증빙:다른사업증빙")
            ok = {"조문": "조:시험사업#제1조①", "발췌": "사전 승인을"} in 공통행["근거"] and len(전용행["근거"]) == 1 \
                and "다른사업.json" in r.집계["파일"] and "증빙:공통증빙" in r.집계["합친개념"]
        확인("묶음 밖 파일의 공통 개념에 근거를 합치고 기록한다", ok, "; ".join(map(str, r.문제들)))

    # 13 다른 사업 전용 개념에 (기존) 으로 근거를 달 수 없다
    전용근거 = 공통근거.replace("증빙:공통증빙 (기존)", "증빙:다른사업증빙 (기존)")
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(전용근거, 원천=다른사업_원천, schema=다른사업_schema)
    with 임시:
        확인("다른 사업 전용 개념에 (기존) 근거를 달면 실패한다", not r.통과 and any("다른 사업 전용 개념" in str(p) for p in r.문제들) and 전 == 후)

    # 14 --재추출: 교체할 규범이 규범 ID 범위와 다른 파일에 있으면 실패
    옮긴원천 = _시험_원천()
    옮긴원천[공통_파일]["규범"].append(옮긴원천["시험사업.json"]["규범"].pop(0))
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(교체, True, 원천=옮긴원천)
    with 임시:
        확인("실패: --재추출 교체 대상이 다른 파일에 있음", not r.통과 and any("교체할 규범이 다른 파일에 있다" in str(p) for p in r.문제들)
             and 전 == 후, "; ".join(map(str, r.문제들)) or "통과해 버렸다")

    # 15 --재추출: 이 확정표 블록에 속한 원천 규범이 표에 없으면 실패(지우지 않는다)
    빠짐 = _시험_확정.replace("      규범:시험사업/제1조①/의무/1 (근거 추가) | 대상근거 \"국내 숙박비 지급기준\" | 검수단계 AI검증\n", "")
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(빠짐, True)
    with 임시:
        기대말 = "확정표에 없는 원천 규범이 있다: 규범:시험사업/제1조①/의무/1. 변환기는 지우지 않는다"
        확인("실패: --재추출 인데 이 블록의 원천 규범이 확정표에 없음", not r.통과 and any(기대말 in str(p) for p in r.문제들)
             and 전 == 후 and not (묶음 / "JSON_변환기록.md").exists(), "; ".join(map(str, r.문제들)) or "통과해 버렸다")

    # 16 부분 재추출(블록 일부): 표에 없는 블록의 원천 규범은 건드리지 않는다
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(추가확정, True)
    with 임시:
        ok = r.통과
        if ok:
            옛행 = _시험_원천()["시험사업.json"]["규범"][0]
            새행 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["규범"][0]
            ok = 옛행 == 새행 and not r.집계["교체규범"]
        확인("부분 재추출은 다른 블록의 원천 규범을 그대로 둔다", ok, "; ".join(map(str, r.문제들)))

    # 17 --재추출 교체: 옛 행의 근거 중 이 확정표 블록 밖 조문의 것은 이어 붙이고 기록한다
    밖근거 = {"조문": "조:지침#제42조", "발췌": "다른 묶음이 붙인 발췌"}
    밖원천 = _시험_원천()
    밖원천["시험사업.json"]["규범"][0]["근거"]["대상"].append(dict(밖근거))
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(교체, True, 원천=밖원천)
    with 임시:
        ok, 실제 = r.통과, None
        if ok:
            실제 = json.loads((원천폴더 / "시험사업.json").read_text(encoding="utf-8"))["규범"][0]["근거"]["대상"]
            기록 = (묶음 / "JSON_변환기록.md").read_text(encoding="utf-8")
            ok = 실제 == [{"조문": "조:시험사업#제1조①표-1", "발췌": "국내 숙박비 지급기준", "원문결함": True}, 밖근거] \
                and "이어 붙인 근거" in 기록 and "다른 묶음이 붙인 발췌" in 기록 \
                and r.집계["이어붙인근거"] == [("규범:시험사업/제1조①/의무/1", "대상", "조:지침#제42조", "다른 묶음이 붙인 발췌")]
        확인("--재추출 교체는 블록 밖 근거를 이어 붙이고(블록 안 옛 근거는 버린다) 기록한다", ok,
             "; ".join(map(str, r.문제들)) or json.dumps(실제, ensure_ascii=False))

    # 18 쓰기: 쓸 수 없는 대상이 있으면 아무것도 바꾸지 않는다(먼저 확인)
    import stat
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정, 쓰기도=False)
    with 임시:
        대상파일 = 원천폴더 / "시험사업.json"
        os.chmod(대상파일, stat.S_IREAD)
        try:
            try:
                쓰기(r, 묶음, 원천폴더)
                말 = "쓰기가 통과해 버렸다"
            except 쓰기실패 as e:
                말 = str(e)
        finally:
            os.chmod(대상파일, stat.S_IREAD | stat.S_IWRITE)
        남은임시 = list(원천폴더.rglob("*.tmp_변환"))
        확인("쓰기: 쓸 수 없는 파일이 있으면 먼저 멈추고 아무것도 바꾸지 않는다",
             "쓸 수 없는 파일이 있어" in 말 and _해시들(원천폴더) == 전 and not 남은임시, 말)

    # 19 쓰기: 바꿔 끼우는 중에 실패하면 이미 바꾼 파일을 옛 글로 되돌리고 임시 파일을 남기지 않는다
    r, 묶음, 원천폴더, 전, 후, 임시 = _실행(_시험_확정, 쓰기도=False)
    with 임시:
        진짜 = os.replace
        횟수 = [0]

        def 가짜(a, b):
            횟수[0] += 1
            if 횟수[0] == 2:
                raise PermissionError("시험: 두 번째 바꿔 끼우기 실패")
            진짜(a, b)
        os.replace = 가짜
        try:
            try:
                쓰기(r, 묶음, 원천폴더)
                말 = "쓰기가 통과해 버렸다"
            except 쓰기실패 as e:
                말 = str(e)
        finally:
            os.replace = 진짜
        남은임시 = list(원천폴더.rglob("*.tmp_변환"))
        확인("쓰기: 중간 실패 때 바꾼 파일을 되돌리고 임시 파일을 남기지 않는다",
             "되돌렸다" in 말 and _해시들(원천폴더) == 전 and not 남은임시 and 횟수[0] == 2, 말)

    for 이름, 참, 설명 in 결과들:
        print(f"{'통과' if 참 else '실패'}: {이름}" + ("" if 참 or not 설명 else f"\n      {설명}"))
    n = sum(1 for _, 참, _ in 결과들 if 참)
    print(f"selftest: {n}/{len(결과들)} 통과")
    return 0 if n == len(결과들) else 1


# CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="확정 추출표(확정.md) → 원천 JSON")
    p.add_argument("묶음", nargs="?", help="묶음 폴더(확정.md · 입력_원문.md · 입력_문서키.json)")
    p.add_argument("--쓰기", action="store_true", help="원천 JSON 과 <묶음>/JSON_변환기록.md 를 쓴다(없으면 요약만)")
    p.add_argument("--재추출", action="store_true", help="같은 규범 ID 를 제자리에서 교체한다")
    p.add_argument("--원천", default=str(원천_기본폴더), help="원천 폴더(기본 ontology/)")
    p.add_argument("--조용히", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _자체시험()
    if not a.묶음:
        p.error("묶음 폴더를 적는다")
    r = 변환(Path(a.묶음), Path(a.원천), a.재추출)
    if not r.통과:
        if not a.조용히:
            print(f"옮길 수 없는 줄 {len(r.문제들)}건. 파일을 고치지 않았다.")
            for x in r.문제들:
                print(f"  {x}")
        return 1
    if not a.조용히:
        print(r.기록)
    if a.쓰기:
        try:
            쓰기(r, Path(a.묶음), Path(a.원천))
        except 쓰기실패 as e:
            print(f"쓰지 못했다: {e}")
            return 1
        if not a.조용히:
            print(f"썼다: {', '.join(r.새글) or '(바뀐 원천 파일 없음)'} · {Path(a.묶음) / 'JSON_변환기록.md'}")
    elif not a.조용히:
        print("(--쓰기 없이 돌려 파일은 고치지 않았다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
