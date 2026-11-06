# -*- coding: utf-8 -*-
"""온톨로지 원천 JSON 검사: 코드 검사 → OWL 생성(owlready2) → HermiT (docs/5_룰/5-4b_온톨로지_스키마.md).

전부 통과해야 ontology_load.py 가 적재한다. 입력은 ontology/schema.json · 공통_L1.json · <사업키>.json 이다.
코드가 보는 것: 필수 칸 · 값, 계층 순환 · 고아, 근거, 별칭 충돌, ID 존재, 수치 연산, 발췌 ⊂ 본문, 문서키 활성 판.
추론기가 보는 것: 일관성 · 불만족 클래스 · 서로소 · 정의역 · 치역. OWL 은 열린 세계라 "없다"를 못 보고 subClassOf 순환을
동치로 처리하므로, 앞의 것은 코드가 맡는다. Java · owlready2 는 개발 PC 와 CI 검사 잡에만 있다(API 이미지에 없다).

    python scripts/ontology_check.py                 # 전체(DB 사용)
    python scripts/ontology_check.py 모두의창업       # 그 사업 + 공통
    python scripts/ontology_check.py --no-db          # 발췌 · 문서키 검사를 건너뛴다
    python scripts/ontology_check.py --self-test      # 일부러 넣은 위반이 각각 실패해야 통과
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

if __name__ == "__main__":
    # 콘솔 코드페이지와 상관없이 한국어 출력이 깨지지 않게 한다
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import paths  # noqa: E402

ROOT = paths.ensure_on_path()

from ontology_norm import 개념ID_풀이, 규범ID_풀이, 발췌_정규화, 발췌_포함, 조문ID_풀이  # noqa: E402
from rule_lookup import _norm  # noqa: E402

원천_기본폴더 = ROOT / "ontology"
공통_파일 = "공통_L1.json"
모든비목 = "비목:창업/*"

검수단계_값 = ("AI초안", "AI대조", "AI검증", "사람확정", "재검토")
검증된_단계 = ("AI검증", "사람확정")
규범종류_값 = ("금지", "허용", "한도", "임계값", "의무", "요건", "우선순위")
관계_값 = ("상위", "사용", "정의", "준용", "위임", "동치", "서로소", "개정")
연결종류_값 = ("원문명시", "해석")
계층_값 = ("L1", "L2", "L3")
단위_값 = ("원", "%", "개", "대")
분모_값 = ("인", "일", "박", "시간", "건", "월", "계약총액", "용역비총액")
누적범위_값 = ("건별", "동일업체합계", "월", "협약기간")
부가세_값 = ("포함", "공급가액", "정의없음")
지역_값 = ("특별시", "광역시", "그밖")
우선순위범위_값 = ("unspecified_only", "all", "상충시")
우선순위출처_값 = ("규정조항", "서비스정책")

# 개념 종류 → OWL 최상위 클래스(5-4b). 비목 ⊃ 세목 ⊃ 품목은 subClassOf 로 잇는다.
종류_최상위 = {
    "사업": "사업", "비목": "사업비항목", "세목": "사업비항목", "품목": "사업비항목",
    "주체": "행위자", "행위자": "행위자", "기관": "행위자", "사람": "행위자", "거래처": "행위자", "위원회": "행위자",
    "절차": "절차행위", "절차행위": "절차행위", "시간": "기간시점", "기간": "기간시점", "시점": "기간시점",
    "증빙": "증빙", "집행건": "집행건",
}
최상위_클래스 = ("사업", "행위자", "사업비항목", "집행건", "절차행위", "기간시점", "증빙")


class 검사오류(Exception):
    """원천 파일을 읽을 수 없을 때."""


@dataclass
class 검사결과:
    오류: list[dict] = field(default_factory=list)
    경고: list[dict] = field(default_factory=list)
    건너뜀: list[str] = field(default_factory=list)
    통계: dict = field(default_factory=dict)

    def 실패(self, 코드: str, 위치: str, 내용: str) -> None:
        self.오류.append({"코드": 코드, "위치": 위치, "내용": 내용})

    def 알림(self, 코드: str, 위치: str, 내용: str) -> None:
        self.경고.append({"코드": 코드, "위치": 위치, "내용": 내용})

    @property
    def 통과(self) -> bool:
        return not self.오류

    def 코드들(self) -> set[str]:
        return {e["코드"] for e in self.오류}

    def 요약(self) -> dict:
        return {"통과": self.통과, "오류": self.오류, "경고": self.경고, "건너뜀": self.건너뜀, "통계": self.통계}


def 행_해시(행: dict) -> str:
    """원천 행의 내용 해시. concepts · norms 의 원천해시이고 적재 diff 가 읽는다."""
    글 = json.dumps(행, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(글.encode("utf-8")).hexdigest()


def 파일_해시(경로: Path) -> str:
    return hashlib.sha256(경로.read_bytes()).hexdigest()


# 원천 읽기 ─────────────────────────────────────────────────────────────────

def _json_읽기(경로: Path) -> dict:
    try:
        값 = json.loads(경로.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise 검사오류(f"원천 파일이 없다: {경로}") from e
    except json.JSONDecodeError as e:
        raise 검사오류(f"JSON 형식 오류: {경로}: {e}") from e
    if not isinstance(값, dict):
        raise 검사오류(f"최상위가 객체가 아니다: {경로}")
    return 값


def 원천_읽기(폴더: Path | str = 원천_기본폴더, 사업키들: Iterable[str] | None = None) -> dict:
    """schema.json · 공통_L1.json · 사업 파일을 읽는다. 사업키들이 None 이면 파일이 있는 사업 전부다.

    반환 {schema, 파일: {이름: 해시}, 공통: 묶음, 사업: {사업명: 묶음}, 폴더}. 묶음은 {개념, 관계, 규범, 파일}.
    """
    폴더 = Path(폴더)
    schema = _json_읽기(폴더 / "schema.json")
    사업표 = schema.get("사업키") or {}
    if 사업키들 is None:
        대상 = [k for k in 사업표 if (폴더 / f"{k}.json").exists()]
    else:
        대상 = list(사업키들)
        for k in 대상:
            if k not in 사업표:
                raise 검사오류(f"schema.json 사업키에 없다: {k}")
    파일해시 = {"schema.json": 파일_해시(폴더 / "schema.json")}

    def 묶음(이름: str, 원천: dict) -> dict:
        파일해시[이름] = 파일_해시(폴더 / 이름)
        return {"개념": 원천.get("개념") or [], "관계": 원천.get("관계") or [], "규범": 원천.get("규범") or [],
                "파일": 이름, "스키마": 원천.get("스키마"), "사업": 원천.get("사업"), "사업키": 원천.get("사업키")}

    공통 = 묶음(공통_파일, _json_읽기(폴더 / 공통_파일))
    사업 = {사업표[k]: 묶음(f"{k}.json", _json_읽기(폴더 / f"{k}.json")) for k in 대상}
    return {"schema": schema, "파일": 파일해시, "공통": 공통, "사업": 사업, "폴더": 폴더}


# 정규 형태로 펼치기 ────────────────────────────────────────────────────────

def _목록(값: Any) -> list:
    if 값 is None or 값 == "":
        return []
    return 값 if isinstance(값, list) else [값]


def 관계_적용사업(행: dict, 파일사업: str | None) -> str | None:
    """관계에 적용사업 키가 없으면 그 파일의 범위다(공통으로 퍼지지 않게 한다). 키가 있으면 값 그대로(null = 공통)."""
    return 행["적용사업"] if "적용사업" in 행 else 파일사업


def 펼치기(원천: dict) -> dict:
    """파일별 행을 합친다. 개념.상위는 관계(상위) 행으로도 만든다. 반환 {개념, 관계, 규범}, 각 행에 `_파일` · `_파일사업`."""
    개념, 관계, 규범 = [], [], []
    묶음들 = [(원천["공통"], None)] + [(b, 사업) for 사업, b in 원천["사업"].items()]
    for 묶음, 사업 in 묶음들:
        for c in 묶음["개념"]:
            c2 = dict(c)
            c2["_파일"], c2["_파일사업"] = 묶음["파일"], 사업
            c2["상위"] = _목록(c.get("상위"))
            c2["별칭"] = _목록(c.get("별칭"))
            개념.append(c2)
            for p in c2["상위"]:
                관계.append({"src": c.get("id"), "관계": "상위", "dst": p, "연결종류": c.get("연결종류"),
                           "적용사업": c.get("적용사업"), "검수단계": c.get("검수단계"), "근거": c.get("근거") or [],
                           "_파일": 묶음["파일"], "_파일사업": 사업, "_개념에서": True})
        for r in 묶음["관계"]:
            r2 = dict(r)
            r2["_파일"], r2["_파일사업"] = 묶음["파일"], 사업
            r2["적용사업"] = 관계_적용사업(r, 사업)
            r2["_개념에서"] = False
            관계.append(r2)
        for n in 묶음["규범"]:
            n2 = dict(n)
            n2["_파일"], n2["_파일사업"] = 묶음["파일"], 사업
            규범.append(n2)
    return {"개념": 개념, "관계": 관계, "규범": 규범}


def _원본(행: dict) -> dict:
    return {k: v for k, v in 행.items() if not k.startswith("_")}


# 발췌 대조용 본문 제공자 ───────────────────────────────────────────────────

def 복원본_읽기(폴더: Path) -> dict[str, str]:
    """ontology/추출/**/입력_원문.md → {조문 ID: 본문}. 원문결함 발췌는 DB 본문 대신 이것과 대조한다."""
    out: dict[str, str] = {}
    for md in sorted((Path(폴더) / "추출").rglob("입력_원문.md")):
        현재: str | None = None
        for 줄 in md.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^###\s+(조:\S+)", 줄)
            if m:
                현재 = m.group(1)
                out.setdefault(현재, "")
            elif 현재 is not None and not 줄.startswith("<!--"):
                out[현재] += 줄 + "\n"
    return out


class DB_원문:
    """DB 조회 묶음. 검사 동안 조문 본문을 메모리에 들고 있는다."""

    def __init__(self, cur):
        self.cur = cur
        self._조문: dict[tuple[str, str], str] = {}
        self._문서상태: dict[str, str] = {}
        self._읽은문서: set[str] = set()

    def 문서상태(self, doc_id: str) -> str | None:
        if doc_id not in self._문서상태:
            행 = self.cur.execute("SELECT status FROM corpus.documents WHERE doc_id = %s", (doc_id,)).fetchone()
            self._문서상태[doc_id] = 행[0] if 행 else None
        return self._문서상태[doc_id]

    def 본문(self, doc_id: str, 조번호: str) -> str | None:
        if doc_id not in self._읽은문서:
            self._읽은문서.add(doc_id)
            for 조, 글 in self.cur.execute("SELECT 조번호, 본문 FROM corpus.doc_articles WHERE doc_id = %s", (doc_id,)):
                self._조문[(doc_id, 조)] = 글
        return self._조문.get((doc_id, 조번호))

    def 사업_존재(self, 사업명: str) -> bool:
        return self.cur.execute("SELECT 1 FROM corpus.programs WHERE 사업명 = %s", (사업명,)).fetchone() is not None


# 코드 검사 ─────────────────────────────────────────────────────────────────

def _근거_검사(근거목록: Any, 위치: str, 문서키: dict, 결과: 검사결과, db, 복원본: dict[str, str]) -> None:
    """근거 항목 형식 + 조문 ID 풀이 + 정규화 발췌 ⊂ 본문. db 가 None 이면 DB 대조만 건너뛴다."""
    for g in _목록(근거목록):
        if not isinstance(g, dict) or not g.get("조문") or not g.get("발췌"):
            결과.실패("근거_형식", 위치, f"조문 · 발췌가 모두 있어야 한다: {g!r}")
            continue
        조문, 발췌 = g["조문"], g["발췌"]
        if 발췌 != 발췌_정규화(발췌):
            결과.실패("발췌_정규화", 위치, f"정규화한 형태로 저장해야 한다: {발췌!r}")
        try:
            풀이 = 조문ID_풀이(조문)
        except ValueError as e:
            결과.실패("조문ID_형식", 위치, str(e))
            continue
        doc_id = 문서키.get(풀이["문서키"])
        if not doc_id:
            결과.실패("문서키_없음", 위치, f"schema.json 문서키에 없다: {풀이['문서키']}")
            continue
        if g.get("원문결함"):
            본문 = 복원본.get(조문)
            if 본문 is None:
                결과.실패("복원본_없음", 위치, f"원문결함인데 입력_원문.md 에 없다: {조문}")
            elif not 발췌_포함(발췌, 본문):
                결과.실패("발췌_불일치", 위치, f"복원본에 없는 발췌: {조문} {발췌!r}")
            continue
        if db is None:
            continue
        본문 = db.본문(doc_id, 풀이["조번호"])
        if 본문 is None:
            결과.실패("조문_없음", 위치, f"DB 에 조문이 없다: {조문}")
        elif not 발췌_포함(발췌, 본문):
            결과.실패("발췌_불일치", 위치, f"본문에 없는 발췌: {조문} {발췌!r}")


def _조문_확인(조문: str, 위치: str, 문서키: dict, 결과: 검사결과, db) -> None:
    """관계의 조문 ID 가 풀리고 문서키가 있고(DB 있으면) 활성 판에 조문이 있는가."""
    try:
        풀이 = 조문ID_풀이(조문)
    except ValueError as e:
        결과.실패("조문ID_형식", 위치, str(e))
        return
    doc_id = 문서키.get(풀이["문서키"])
    if not doc_id:
        결과.실패("문서키_없음", 위치, f"schema.json 문서키에 없다: {풀이['문서키']}")
    elif db is not None and db.본문(doc_id, 풀이["조번호"]) is None:
        결과.실패("조문_없음", 위치, f"DB 에 조문이 없다: {조문}")


def 미특정인가(값: str) -> bool:
    return 값 == "미특정" or 값.startswith("미특정:")


def _범위_안(참조_사업: str | None, 쓰는_사업: str | None) -> bool:
    """쓰는 행의 범위에서 참조 대상이 보이는가. 공통 대상은 어디서나, 사업한정 대상은 그 사업에서만."""
    return 참조_사업 is None or 참조_사업 == 쓰는_사업


def _문자열들(값: Any) -> Iterable[str]:
    if isinstance(값, str):
        yield 값
    elif isinstance(값, dict):
        for v in 값.values():
            yield from _문자열들(v)
    elif isinstance(값, list):
        for v in 값:
            yield from _문자열들(v)


def _중복_검사(행들: list[dict], 키: str, 코드: str, 결과: 검사결과) -> None:
    본것: dict[str, dict] = {}
    for 행 in 행들:
        i = 행.get(키)
        if not i:
            continue
        원본 = _원본(행)
        if i in 본것 and 본것[i] != 원본:
            결과.실패(코드, str(i), f"같은 ID 의 내용이 다르다({행['_파일']})")
        본것.setdefault(i, 원본)


def _개념_검사(펼침: dict, schema: dict, 결과: 검사결과, db, 복원본: dict) -> dict[str, dict]:
    문서키 = schema.get("문서키") or {}
    개념표: dict[str, dict] = {}
    _중복_검사(펼침["개념"], "id", "개념ID_중복", 결과)
    for c in 펼침["개념"]:
        i = c.get("id")
        위치 = f"개념 {i or '(id 없음)'} [{c['_파일']}]"
        for 칸 in ("id", "종류", "정식명", "검수단계"):
            if not c.get(칸):
                결과.실패("필수칸", 위치, f"{칸} 이 없다")
        if not i:
            continue
        개념표.setdefault(i, c)
        try:
            풀이 = 개념ID_풀이(i)
            if 풀이["종류"] != c.get("종류") and not (풀이["종류"] == "주체" and c.get("종류") in 종류_최상위):
                결과.실패("개념ID_종류", 위치, f"ID 의 종류({풀이['종류']})와 종류 칸({c.get('종류')})이 다르다")
        except ValueError as e:
            결과.실패("개념ID_형식", 위치, str(e))
        if c.get("종류") and c["종류"] not in 종류_최상위:
            결과.실패("개념_종류", 위치, f"모르는 종류: {c['종류']}")
        if c.get("검수단계") and c["검수단계"] not in 검수단계_값:
            결과.실패("검수단계", 위치, f"허용되지 않는 값: {c['검수단계']}")
        if c.get("적용사업") not in (None, c["_파일사업"]):
            결과.실패("적용사업", 위치, f"파일 범위 밖의 적용사업: {c['적용사업']}")
        if c["_파일사업"] is not None and "적용사업" not in c:
            결과.실패("필수칸", 위치, "사업 파일의 개념은 적용사업 칸을 적어야 한다(null = 공통)")
        if not c.get("근거"):
            결과.실패("근거없음", 위치, "근거가 없다")
        _근거_검사(c.get("근거"), 위치, 문서키, 결과, db, 복원본)
        if c.get("종류") in ("세목", "품목") and not c["상위"]:
            결과.실패("고아", 위치, "상위가 없는 세목 · 품목")
    return 개념표


def _별칭_검사(펼침: dict, 결과: 검사결과) -> None:
    """같은 정규화 · 같은 적용사업에 다른 개념이면 충돌. 정식명도 조회 표현이라 함께 본다."""
    본것: dict[tuple[str, str | None], str] = {}
    for c in 펼침["개념"]:
        i = c.get("id")
        if not i:
            continue
        for 표현 in [c.get("정식명"), *c["별칭"]]:
            if not 표현:
                continue
            키 = _norm(str(표현))
            if not 키:
                결과.실패("별칭_빈값", f"개념 {i}", f"정규화하면 빈 문자열: {표현!r}")
                continue
            앞 = 본것.setdefault((키, c.get("적용사업")), i)
            if 앞 != i:
                결과.실패("별칭_충돌", f"개념 {i}", f"{표현!r} 이 {앞} 와 겹친다(적용사업 {c.get('적용사업')})")


def _관계_검사(펼침: dict, 개념표: dict, schema: dict, 결과: 검사결과, db, 복원본: dict) -> None:
    문서키 = schema.get("문서키") or {}
    사업표 = schema.get("사업키") or {}
    암묵_사업 = {f"사업:{k}" for k in 사업표} | {f"사업:{v}" for v in 사업표.values()}
    for r in 펼침["관계"]:
        위치 = f"관계 {r.get('src')} -{r.get('관계')}-> {r.get('dst')} [{r['_파일']}]"
        for 칸 in ("src", "관계", "dst"):
            if not r.get(칸):
                결과.실패("필수칸", 위치, f"{칸} 이 없다")
        if not (r.get("src") and r.get("관계") and r.get("dst")):
            continue
        if r["관계"] not in 관계_값:
            결과.실패("관계_종류", 위치, f"모르는 관계: {r['관계']}")
        if r.get("연결종류") and r["연결종류"] not in 연결종류_값:
            결과.실패("연결종류", 위치, f"허용되지 않는 값: {r['연결종류']}")
        if r.get("검수단계") and r["검수단계"] not in 검수단계_값:
            결과.실패("검수단계", 위치, f"허용되지 않는 값: {r['검수단계']}")
        if r.get("적용사업") not in (None, r["_파일사업"]):
            결과.실패("적용사업", 위치, f"파일 범위 밖의 적용사업: {r['적용사업']}")
        if not r["_개념에서"]:
            if not r.get("근거"):
                결과.실패("근거없음", 위치, "근거가 없다")
            _근거_검사(r.get("근거"), 위치, 문서키, 결과, db, 복원본)
        for 끝, 값 in (("src", r["src"]), ("dst", r["dst"])):
            if 값.startswith("조:"):
                _조문_확인(값, 위치, 문서키, 결과, db)
            elif 끝 == "dst" and 미특정인가(값):
                결과.알림("미특정", 위치, "조문을 특정하지 못한 대상")
            elif 값 in 암묵_사업:
                pass
            elif 값 not in 개념표:
                결과.실패("ID_없음", 위치, f"{끝} 가 없는 노드다: {값}")
            elif not _범위_안(개념표[값].get("적용사업"), r.get("적용사업")):
                결과.실패("범위_밖", 위치, f"{끝} 가 다른 사업의 노드다: {값}")
        if r["관계"] == "동치":
            if not r.get("문맥조문") or not r.get("적용조문"):
                결과.실패("필수칸", 위치, "동치에는 문맥조문 · 적용조문이 있어야 한다")
            for 조 in [r.get("문맥조문"), *_목록(r.get("적용조문"))]:
                if 조:
                    _조문_확인(조, 위치, 문서키, 결과, db)
    # 상위 관계에 쓰인 개념이 공통인데 상위가 사업한정이면 다른 사업에서 끊긴다(위 범위 검사가 잡는다).
    _순환_검사(펼침, 결과)


def _순환_검사(펼침: dict, 결과: 검사결과) -> None:
    """subClassOf 순환은 추론기에서 동치로 처리돼 모순이 아니다. 코드로 잡는다."""
    상위: dict[str, set[str]] = {}
    for r in 펼침["관계"]:
        if r.get("관계") == "상위" and r.get("src") and r.get("dst"):
            상위.setdefault(r["src"], set()).add(r["dst"])
    상태: dict[str, int] = {}          # 1 방문 중, 2 끝

    def 방문(n: str, 길: list[str]) -> None:
        상태[n] = 1
        for p in sorted(상위.get(n, ())):
            if 상태.get(p) == 1:
                결과.실패("계층_순환", f"개념 {n}", " → ".join([*길[길.index(p):], n, p]) if p in 길 else f"{n} → {p}")
            elif 상태.get(p) is None:
                방문(p, [*길, n])
        상태[n] = 2

    for n in sorted(상위):
        if 상태.get(n) is None:
            방문(n, [])


def _규범_검사(펼침: dict, 개념표: dict, schema: dict, 결과: 검사결과, db, 복원본: dict) -> None:
    문서키 = schema.get("문서키") or {}
    사업표 = schema.get("사업키") or {}
    규범표 = {n["id"]: n for n in 펼침["규범"] if n.get("id")}
    _중복_검사(펼침["규범"], "id", "규범ID_중복", 결과)

    def 개념_참조(값: str, 위치: str, 칸: str, 쓰는_사업: str | None) -> None:
        if 값 == 모든비목 and 칸 == "대상":
            return
        if 값 not in 개념표:
            결과.실패("ID_없음", 위치, f"{칸} 이 없는 노드다: {값}")
        elif not _범위_안(개념표[값].get("적용사업"), 쓰는_사업):
            결과.실패("범위_밖", 위치, f"{칸} 이 다른 사업의 노드다: {값}")

    for n in 펼침["규범"]:
        i = n.get("id")
        위치 = f"규범 {i or '(id 없음)'} [{n['_파일']}]"
        for 칸 in ("id", "종류", "계층", "검수단계"):
            if not n.get(칸):
                결과.실패("필수칸", 위치, f"{칸} 이 없다")
        대상 = _목록(n.get("대상"))
        if not 대상:
            결과.실패("필수칸", 위치, "대상이 없다")
        종류 = n.get("종류")
        if 종류 and 종류 not in 규범종류_값:
            결과.실패("규범_종류", 위치, f"모르는 종류: {종류}")
        if n.get("계층") and n["계층"] not in 계층_값:
            결과.실패("계층", 위치, f"허용되지 않는 값: {n['계층']}")
        if n.get("검수단계") and n["검수단계"] not in 검수단계_값:
            결과.실패("검수단계", 위치, f"허용되지 않는 값: {n['검수단계']}")
        if "해석미결" in n and not isinstance(n["해석미결"], bool):
            결과.실패("해석미결", 위치, "참 · 거짓이어야 한다")
        사업 = n.get("적용사업")
        if 사업 not in (None, n["_파일사업"]):
            결과.실패("적용사업", 위치, f"파일 범위 밖의 적용사업: {사업}")
        if i:
            try:
                풀이 = 규범ID_풀이(i)
                if 종류 and 풀이["종류"] != 종류:
                    결과.실패("규범ID_종류", 위치, f"ID 의 종류({풀이['종류']})와 종류 칸({종류})이 다르다")
                기대 = "공통" if 사업 is None else next((k for k, v in 사업표.items() if v == 사업), None)
                if 풀이["범위"] != 기대:
                    결과.실패("규범ID_범위", 위치, f"ID 의 범위({풀이['범위']})가 적용사업({사업})과 다르다")
            except ValueError as e:
                결과.실패("규범ID_형식", 위치, str(e))
        for 값 in 대상:
            개념_참조(값, 위치, "대상", 사업)
        for 칸 in ("수범자", "승인주체"):
            for 값 in _목록(n.get(칸)):
                개념_참조(값, 위치, 칸, 사업)

        근거 = n.get("근거")
        근거칸 = 근거 if isinstance(근거, dict) else {}
        if not isinstance(근거, dict) or not any(_목록(근거칸.get(k)) for k in ("수치", "조건", "대상")):
            결과.실패("근거없음", 위치, "근거가 없다")
        if isinstance(근거, dict):
            for 칸 in 근거칸:
                if 칸 not in ("수치", "조건", "대상"):
                    결과.실패("근거_칸", 위치, f"모르는 근거 칸: {칸}")
                _근거_검사(근거칸[칸], 위치, 문서키, 결과, db, 복원본)

        _수치_검사(n, 위치, 결과)
        if n.get("검수단계") in 검증된_단계 and n.get("수치") and not _목록(근거칸.get("수치")):
            결과.실패("수치근거", 위치, "AI검증 이상인데 수치 근거가 없다")
        if 종류 == "우선순위":
            조건 = n.get("조건") or {}
            for 칸 in ("상위", "하위", "범위", "출처"):
                if not 조건.get(칸):
                    결과.실패("필수칸", 위치, f"우선순위 조건의 {칸} 이 없다")
            if 조건.get("범위") and 조건["범위"] not in 우선순위범위_값:
                결과.실패("우선순위_범위", 위치, f"허용되지 않는 값: {조건['범위']}")
            if 조건.get("출처") and 조건["출처"] not in 우선순위출처_값:
                결과.실패("우선순위_출처", 위치, f"허용되지 않는 값: {조건['출처']}")

        결과값 = n.get("결과") or {}
        결과_규범 = [s for s in _문자열들(결과값) if s.startswith("규범:")]
        for 값 in 결과_규범:
            if 값 not in 규범표:
                결과.실패("ID_없음", 위치, f"결과가 없는 규범을 가리킨다: {값}")
        if 종류 == "임계값" and not 결과_규범:
            결과.실패("필수칸", 위치, "임계값의 결과에 활성화할 규범 ID 가 없다")
        for 값 in _목록(n.get("증빙")) + _목록(결과값.get("증빙") if isinstance(결과값, dict) else None):
            개념_참조(값, 위치, "증빙", 사업)
        for 값 in _목록(n.get("예외")):
            if isinstance(값, str) and 값.startswith("규범:"):
                if 값 not in 규범표:
                    결과.실패("ID_없음", 위치, f"예외가 없는 규범을 가리킨다: {값}")
            elif db is not None:
                _단서_검사(값, n, 위치, 문서키, 결과, db)
    _대체_순환_검사(펼침, 결과)


def _단서_검사(값: Any, 규범: dict, 위치: str, 문서키: dict, 결과: 검사결과, db) -> None:
    """예외가 규범 ID 가 아니면 단서 발췌다. 그 규범의 근거 조문 어딘가에 있어야 한다."""
    for 목록 in (규범.get("근거") or {}).values():
        for g in _목록(목록):
            try:
                풀이 = 조문ID_풀이(g.get("조문"))
            except (ValueError, TypeError, AttributeError):
                continue
            doc_id = 문서키.get(풀이["문서키"])
            if doc_id and 발췌_포함(str(값), db.본문(doc_id, 풀이["조번호"]) or ""):
                return
    결과.실패("발췌_불일치", 위치, f"단서 발췌가 근거 조문에 없다: {값!r}")


def _대체_순환_검사(펼침: dict, 결과: 검사결과) -> None:
    """결과.대체 가 서로를 가리키면 어느 쪽도 채택할 수 없다."""
    대체: dict[str, set[str]] = {}
    for n in 펼침["규범"]:
        값 = (n.get("결과") or {}).get("대체") if isinstance(n.get("결과"), dict) else None
        if n.get("id") and 값:
            대체[n["id"]] = set(_목록(값))
    for a, 들 in 대체.items():
        for b in 들:
            if a in 대체.get(b, ()):
                결과.실패("대체_순환", f"규범 {a}", f"{b} 와 서로 대체한다")


def _수치_검사(n: dict, 위치: str, 결과: 검사결과) -> None:
    종류, 수치 = n.get("종류"), n.get("수치")
    if 종류 in ("한도", "임계값") and not 수치:
        결과.실패("필수칸", 위치, f"{종류} 에 수치가 없다")
    if not 수치:
        return
    if not isinstance(수치, dict):
        결과.실패("수치_형식", 위치, "수치는 객체여야 한다")
        return
    값 = 수치.get("값")
    if isinstance(값, bool) or not isinstance(값, (int, float)):
        결과.실패("수치_값", 위치, f"값이 숫자가 아니다: {값!r}")
    if 수치.get("단위") not in 단위_값:
        결과.실패("수치_단위", 위치, f"허용되지 않는 단위: {수치.get('단위')!r}")
    위반, 발동 = 수치.get("위반연산"), 수치.get("발동연산")
    if 종류 == "한도" and (위반 not in ("초과", "이상") or 발동 is not None):
        결과.실패("수치_연산", 위치, f"한도는 위반연산(초과 · 이상)만 쓴다: 위반 {위반!r} · 발동 {발동!r}")
    if 종류 == "임계값" and (발동 not in ("이상", "초과") or 위반 is not None):
        결과.실패("수치_연산", 위치, f"임계값은 발동연산(이상 · 초과)만 쓴다: 위반 {위반!r} · 발동 {발동!r}")
    for 값 in _목록(수치.get("분모")):
        if 값 not in 분모_값:
            결과.실패("수치_분모", 위치, f"허용되지 않는 분모: {값!r}")
    for 칸, 허용 in (("누적범위", 누적범위_값), ("부가세", 부가세_값), ("지역", 지역_값)):
        if 수치.get(칸) is not None and 수치[칸] not in 허용:
            결과.실패(f"수치_{칸}", 위치, f"허용되지 않는 값: {수치[칸]!r}")


def _머리_검사(원천: dict, 결과: 검사결과, db) -> None:
    schema = 원천["schema"]
    번호 = schema.get("스키마")
    for 묶음, 사업 in [(원천["공통"], None), *[(b, s) for s, b in 원천["사업"].items()]]:
        if 묶음.get("스키마") != 번호:
            결과.실패("스키마_번호", 묶음["파일"], f"schema.json 은 {번호}, 파일은 {묶음.get('스키마')}")
        if 사업 is not None:
            if 묶음.get("사업") != 사업:
                결과.실패("사업키", 묶음["파일"], f"사업 칸({묶음.get('사업')})이 schema.json 사업키({사업})와 다르다")
            if db is not None and not db.사업_존재(사업):
                결과.실패("사업키", 묶음["파일"], f"corpus.programs 에 없는 사업명: {사업}")
    if db is None:
        결과.건너뜀.append("발췌 ⊂ DB 본문 · 조문 존재 · 문서키 활성 판 · 사업명(DB 없음)")
        return
    for 키, doc_id in (schema.get("문서키") or {}).items():
        상태 = db.문서상태(doc_id)
        if 상태 != "active":
            결과.실패("문서키_활성아님", 키, f"{doc_id} 의 상태가 {상태} 다(active 여야 한다)")


def 코드_검사(원천: dict, 결과: 검사결과, db=None, 복원본: dict[str, str] | None = None) -> dict:
    """OWL 앞의 코드 검사. 결과에 오류를 쌓고 펼친 행을 돌려준다."""
    복원본 = 복원본 if 복원본 is not None else 복원본_읽기(원천["폴더"]) if 원천.get("폴더") else {}
    펼침 = 펼치기(원천)
    _머리_검사(원천, 결과, db)
    개념표 = _개념_검사(펼침, 원천["schema"], 결과, db, 복원본)
    _별칭_검사(펼침, 결과)
    _관계_검사(펼침, 개념표, 원천["schema"], 결과, db, 복원본)
    _규범_검사(펼침, 개념표, 원천["schema"], 결과, db, 복원본)
    결과.통계.update(
        개념=len(개념표), 관계=len(펼침["관계"]), 규범=len({n.get("id") for n in 펼침["규범"]}),
        해석미결=sum(1 for n in 펼침["규범"] if n.get("해석미결")),
        **검수_비율(펼침))
    return 펼침


def 검수_비율(펼침: dict) -> dict:
    """AI검증 이상 비율. 적재 로그에 적는다."""
    out = {}
    for 이름 in ("개념", "관계", "규범"):
        행 = [r for r in 펼침[이름] if not r.get("_개념에서")]
        n = sum(1 for r in 행 if r.get("검수단계") in 검증된_단계)
        out[f"{이름}_AI검증이상"] = f"{n}/{len(행)}"
    return out


# OWL 생성 + HermiT ─────────────────────────────────────────────────────────

_기준IRI = "http://suddoe.kr/ontology/"
_SKOS = "http://www.w3.org/2004/02/skos/core#"
_PROV = "http://www.w3.org/ns/prov#"
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


def _로컬이름(접두: str, 값: str) -> str:
    """IRI 는 ASCII 해시로 만든다. HermiT 출력 디코딩이 콘솔 코드페이지를 따라 한글 IRI 가 깨지기 때문이다."""
    return f"{접두}_{hashlib.sha1(값.encode('utf-8')).hexdigest()[:20]}"


def _사업_보기(펼침: dict, 사업: str | None) -> dict:
    """공통(적용사업 NULL) + 그 사업 오버레이. 다른 사업의 행은 들어가지 않는다(5-4b 사업별 파일)."""
    def 보임(행: dict) -> bool:
        return 행.get("적용사업") in (None, 사업)
    return {k: [r for r in 펼침[k] if 보임(r)] for k in ("개념", "관계", "규범")}


def OWL_생성(펼침: dict, 사업: str | None, 저장경로: Path | None = None):
    """사업 파일 하나의 OWL world 를 만든다. 개념 노드 = 클래스, 규범 · 조문 · 문서 = 개체, 규범이 클래스를 가리키면 punning.

    반환 (world, {클래스 객체: 개념 ID}). 저장경로를 주면 RDF/XML 로 저장한다(원천은 JSON 이고 OWL 은 생성물이다).
    """
    import owlready2 as o

    보기 = _사업_보기(펼침, 사업)
    w = o.World()
    on = w.get_ontology(f"{_기준IRI}{_로컬이름('p', 사업 or '공통')}#")
    skos = on.get_namespace(_SKOS)
    prov = on.get_namespace(_PROV)
    타입 = w._abbreviate(_RDF_TYPE)

    with on:
        with skos:
            prefLabel = types.new_class("prefLabel", (o.AnnotationProperty,))
            altLabel = types.new_class("altLabel", (o.AnnotationProperty,))
            definition = types.new_class("definition", (o.AnnotationProperty,))
        with prov:
            wasDerivedFrom = types.new_class("wasDerivedFrom", (o.ObjectProperty,))

        최상위 = {n: types.new_class(_로컬이름("top", n), (o.Thing,)) for n in (*최상위_클래스, "규범", "조문", "문서")}
        for n, c in 최상위.items():
            setattr(c, "prefLabel", [n])
        o.AllDisjoint(list(최상위.values()))

        사용 = types.new_class("사용", (o.ObjectProperty,))
        사용.domain = [최상위["사업"]]
        사용.range = [최상위["사업비항목"]]
        대상속성 = types.new_class("대상", (o.ObjectProperty,))
        대상속성.domain = [최상위["규범"]]
        준용 = types.new_class("준용", (o.ObjectProperty,))
        준용.domain = [최상위["조문"]]
        위임 = types.new_class("위임", (o.ObjectProperty,))
        위임.domain = [최상위["조문"]]
        소속 = types.new_class("소속문서", (o.ObjectProperty,))
        소속.domain = [최상위["조문"]]
        소속.range = [최상위["문서"]]

        클래스: dict[str, Any] = {}
        for c in 보기["개념"]:
            if c["id"] in 클래스:
                continue
            최상 = 최상위[종류_최상위[c["종류"]]]
            k = types.new_class(_로컬이름("c", c["id"]), (최상,))
            k.prefLabel = [c["정식명"]]
            if c["별칭"]:
                k.altLabel = list(c["별칭"])
            if c.get("정의"):
                k.definition = [c["정의"]]
            k.label = [c["id"]]
            클래스[c["id"]] = k
            on._add_obj_triple_spo(k.storid, 타입, 최상.storid)    # punning: 같은 IRI 의 개체에도 최상위 타입을 준다

        조문개체: dict[str, Any] = {}
        문서개체: dict[str, Any] = {}

        def 조문(i: str):
            if i not in 조문개체:
                x = 최상위["조문"](_로컬이름("a", i))
                x.label = [i]
                문서 = 조문ID_풀이(i)["문서키"]
                if 문서 not in 문서개체:
                    문서개체[문서] = 최상위["문서"](_로컬이름("d", 문서))
                    문서개체[문서].label = [문서]
                x.소속문서 = [문서개체[문서]]
                조문개체[i] = x
            return 조문개체[i]

        def 사업개체(i: str):
            k = 클래스.get(i)
            if k is None:
                k = 최상위["사업"](_로컬이름("b", i))
                k.label = [i]
                클래스[i] = k
            return k

        def 근거_연결(주체저장id: int, 근거: Any) -> None:
            for g in _목록(근거):
                on._add_obj_triple_spo(주체저장id, w._abbreviate(wasDerivedFrom.iri), 조문(g["조문"]).storid)

        def 객체값(s_storid: int, 속성, 끝: str) -> None:
            if 끝.startswith("조:"):
                값 = 조문(끝).storid
            elif 끝 in 클래스:
                값 = 클래스[끝].storid
            else:
                return                                              # 미특정: OWL 에 넣지 않는다
            on._add_obj_triple_spo(s_storid, w._abbreviate(속성.iri), 값)

        for c in 보기["개념"]:
            근거_연결(클래스[c["id"]].storid, c.get("근거"))

        for r in 보기["관계"]:
            관계 = r["관계"]
            if 관계 == "상위":
                클래스[r["src"]].is_a.append(클래스[r["dst"]])
            elif 관계 == "서로소":
                o.AllDisjoint([클래스[r["src"]], 클래스[r["dst"]]])
            elif 관계 == "사용":
                사업_s = 사업개체(r["src"]) if r["src"].startswith("사업:") else 클래스[r["src"]]
                객체값(사업_s.storid, 사용, r["dst"])
            elif 관계 in ("준용", "위임"):
                if r["src"].startswith("조:"):
                    객체값(조문(r["src"]).storid, 준용 if 관계 == "준용" else 위임, r["dst"])

        for n in 보기["규범"]:
            개체 = 최상위["규범"](_로컬이름("n", n["id"]))
            개체.label = [n["id"]]
            for 값 in _목록(n.get("대상")):
                객체값(개체.storid, 대상속성, 값)
            for 목록 in (n.get("근거") or {}).values():
                근거_연결(개체.storid, 목록)

    if 저장경로 is not None:
        Path(저장경로).parent.mkdir(parents=True, exist_ok=True)
        on.save(str(저장경로), format="rdfxml")
    return w, 클래스


def HermiT_검사(펼침: dict, 사업: str | None, 결과: 검사결과, 저장경로: Path | None = None) -> None:
    """일관성 · 불만족 클래스 · 서로소 위반이면 오류를 쌓는다. 실행 자체가 실패해도 오류다(fail-closed)."""
    범위 = 사업 or "공통"
    try:
        import owlready2 as o
    except ImportError:
        결과.실패("OWL_실행", 범위, "owlready2 가 없다. pip install owlready2")
        return
    try:
        w, 클래스 = OWL_생성(펼침, 사업, 저장경로)
    except Exception as e:  # noqa: BLE001 - OWL 을 못 만들면 적재하지 않는다
        결과.실패("OWL_생성", 범위, f"{type(e).__name__}: {e}")
        return
    try:
        o.sync_reasoner_hermit(w, infer_property_values=False, debug=0)
    except o.OwlReadyInconsistentOntologyError:
        결과.실패("OWL_불일치", 범위, "온톨로지가 일관되지 않다(정의역 · 치역 또는 개체의 서로소 위반)")
        return
    except Exception as e:  # noqa: BLE001 - Java 없음 · HermiT 오류 · 재부모 처리 실패
        결과.실패("OWL_실행", 범위, f"{type(e).__name__}: {e}")
        return
    # World.inconsistent_classes() 는 HermiT 가 불만족으로 낸 클래스를 놓치는 경우가 있어(owlready2 0.51 에서 확인)
    # 각 클래스의 equivalent_to 를 직접 본다.
    나쁨 = [i for i, k in 클래스.items()
          if isinstance(k, o.ThingClass) and o.Nothing in k.equivalent_to]
    for i in sorted(나쁨):
        결과.실패("OWL_불만족", 범위, f"불만족 클래스(서로소 위반 등): {i}")


def 전체_검사(원천: dict, cur=None, 복원본: dict[str, str] | None = None, OWL폴더: Path | None = None) -> tuple[검사결과, dict]:
    """코드 검사 → (통과하면) 공통 · 사업별 OWL + HermiT. cur 가 None 이면 DB 대조를 건너뛴다."""
    결과 = 검사결과()
    펼침 = 코드_검사(원천, 결과, DB_원문(cur) if cur is not None else None, 복원본)
    if not 결과.통과:
        결과.건너뜀.append("OWL · HermiT(코드 검사 실패)")
        return 결과, 펼침
    for 사업 in [None, *원천["사업"]]:
        저장 = None
        if OWL폴더 is not None:
            저장 = Path(OWL폴더) / f"{_사업파일이름(원천, 사업)}.owl"
        HermiT_검사(펼침, 사업, 결과, 저장)
    return 결과, 펼침


def _사업파일이름(원천: dict, 사업: str | None) -> str:
    if 사업 is None:
        return "공통"
    return next((k for k, v in (원천["schema"].get("사업키") or {}).items() if v == 사업), 사업)


# self-test ────────────────────────────────────────────────────────────────

_본문 = {
    ("문서A", "제33조"): "제33조(시제품 제작) ① 재료비 ② 비목별 집행 기준은 지침 제42조에 따른다.",
    ("문서A", "제36조"): "제36조(지급수수료) ① “멘토링비”는 1인 1일 최대 30만원 한도(시간당 10만원\n초과 불가)이며, "
                       "12만 5천원을 초과하는 경우 기타 소득세 등을 공제 후 집행하여야 한다.",
    ("문서A", "별지2"): "지급수수료 멘토링비, 기자재임차비",
    ("문서B", "제42조"): "제42조(지급수수료) 재료비 기계장치 구분. 서버 임차는 지급수수료다.",
}


class _가짜DB:
    """self-test 용. DB 없이 본문 · 문서상태를 내준다."""

    def __init__(self, 문서상태: dict | None = None):
        self._상태 = 문서상태 or {"문서A": "active", "문서B": "active"}

    def 문서상태(self, doc_id: str):
        return self._상태.get(doc_id)

    def 본문(self, doc_id: str, 조번호: str):
        return _본문.get((doc_id, 조번호))

    def 사업_존재(self, 사업명: str) -> bool:
        return 사업명 == "테스트사업"


def _시험_원천() -> dict:
    근거_재료 = [{"조문": "조:공통문서#제42조", "발췌": "재료비 기계장치 구분"}]
    공통 = {"스키마": 2, "개념": [
        {"id": "비목:창업/재료비", "종류": "비목", "정식명": "재료비", "별칭": [], "상위": None, "검수단계": "AI검증", "근거": 근거_재료},
        {"id": "비목:창업/기계장치", "종류": "비목", "정식명": "기계장치", "별칭": [], "상위": None, "검수단계": "AI검증", "근거": 근거_재료},
        {"id": "비목:창업/지급수수료", "종류": "비목", "정식명": "지급수수료", "별칭": [], "상위": None, "검수단계": "AI검증",
         "근거": [{"조문": "조:공통문서#제42조", "발췌": "서버 임차는 지급수수료다."}]},
        {"id": "주체:진출자", "종류": "주체", "정식명": "진출자", "별칭": [], "상위": None, "검수단계": "AI검증", "근거": 근거_재료},
    ], "관계": [
        {"src": "비목:창업/재료비", "관계": "서로소", "dst": "비목:창업/기계장치", "적용사업": None,
         "연결종류": "원문명시", "검수단계": "AI검증", "근거": 근거_재료},
    ], "규범": []}
    사업 = {"스키마": 2, "사업": "테스트사업", "사업키": "시험", "개념": [
        {"id": "세목:창업/지급수수료/멘토링비", "종류": "세목", "정식명": "멘토링비", "별칭": ["멘토링 비용"],
         "상위": "비목:창업/지급수수료", "적용사업": None, "검수단계": "AI대조",
         "근거": [{"조문": "조:시험문서#별지2/지급수수료/정의", "발췌": "멘토링비, 기자재임차비"}]},
        {"id": "품목:창업/철재", "종류": "품목", "정식명": "철재", "별칭": [], "상위": "비목:창업/재료비",
         "적용사업": "테스트사업", "검수단계": "AI대조",
         "근거": [{"조문": "조:공통문서#제42조", "발췌": "재료비 기계장치 구분"}]},
    ], "관계": [
        {"src": "조:시험문서#제33조②", "관계": "준용", "dst": "조:공통문서#제42조", "연결종류": "원문명시",
         "적용사업": "테스트사업", "근거": [{"조문": "조:시험문서#제33조②", "발췌": "비목별 집행 기준은 지침 제42조에 따른다."}]},
        {"src": "사업:시험", "관계": "사용", "dst": "비목:창업/지급수수료", "연결종류": "원문명시",
         "근거": [{"조문": "조:시험문서#제33조②", "발췌": "비목별 집행 기준은"}]},
        {"src": "조:시험문서#제36조①", "관계": "준용", "dst": "미특정:소득세법 시행령", "연결종류": "원문명시",
         "근거": [{"조문": "조:시험문서#제36조①", "발췌": "12만 5천원을 초과하는 경우"}]},
    ], "규범": [
        {"id": "규범:시험/제36조①/한도/1", "종류": "한도", "계층": "L2", "적용사업": "테스트사업",
         "대상": ["세목:창업/지급수수료/멘토링비"], "수범자": ["주체:진출자"], "승인주체": [],
         "조건": {"구조화": [], "자연어": []},
         "수치": {"값": 300000, "단위": "원", "위반연산": "초과", "분모": ["인", "일"], "누적범위": "건별", "부가세": "정의없음"},
         "결과": {"종류": "불가"}, "예외": [], "해석미결": False, "검수단계": "AI검증",
         "근거": {"수치": [{"조문": "조:시험문서#제36조①", "발췌": "1인 1일 최대 30만원 한도"}], "조건": [],
                 "대상": [{"조문": "조:시험문서#제36조①", "발췌": "\"멘토링비\"는"}]}},
        {"id": "규범:시험/제36조①/임계값/1", "종류": "임계값", "계층": "L2", "적용사업": "테스트사업",
         "대상": ["세목:창업/지급수수료/멘토링비"], "수치": {"값": 125000, "단위": "원", "발동연산": "초과", "부가세": "정의없음"},
         "결과": {"활성화": ["규범:시험/제36조①/의무/1"]}, "검수단계": "AI대조",
         "근거": {"수치": [{"조문": "조:시험문서#제36조①", "발췌": "12만 5천원을 초과하는 경우"}]}},
        {"id": "규범:시험/제36조①/의무/1", "종류": "의무", "계층": "L2", "적용사업": "테스트사업",
         "대상": ["세목:창업/지급수수료/멘토링비"], "결과": {"조건문": "기타 소득세 공제 후 집행"}, "검수단계": "AI대조",
         "근거": {"조건": [{"조문": "조:시험문서#제36조①", "발췌": "기타 소득세 등을 공제 후 집행하여야 한다."}]}},
    ]}
    schema = {"스키마": 2, "사업키": {"시험": "테스트사업"}, "문서키": {"시험문서": "문서A", "공통문서": "문서B"},
              "문서 표시명": {"시험문서": "시험", "공통문서": "지침"}}

    def 묶음(이름, r):
        return {"개념": r["개념"], "관계": r["관계"], "규범": r["규범"], "파일": 이름, "스키마": r["스키마"],
                "사업": r.get("사업"), "사업키": r.get("사업키")}
    return {"schema": schema, "파일": {}, "공통": 묶음(공통_파일, 공통), "사업": {"테스트사업": 묶음("시험.json", 사업)}, "폴더": None}


def _사업묶음(원천: dict) -> dict:
    return 원천["사업"]["테스트사업"]


def _규범(원천: dict, i: str) -> dict:
    return next(n for n in _사업묶음(원천)["규범"] if n["id"] == i)


def _개념(원천: dict, i: str) -> dict:
    return next(c for b in (원천["공통"], _사업묶음(원천)) for c in b["개념"] if c["id"] == i)


def _위반_순환(r):
    _개념(r, "비목:창업/지급수수료")["상위"] = "세목:창업/지급수수료/멘토링비"


def _위반_서로소(r):
    _개념(r, "품목:창업/철재")["상위"] = ["비목:창업/재료비", "비목:창업/기계장치"]


def _위반_치역(r):
    _사업묶음(r)["관계"][1]["dst"] = "주체:진출자"


def _위반_발췌(r):
    _규범(r, "규범:시험/제36조①/한도/1")["근거"]["수치"][0]["발췌"] = "1인 1일 최대 40만원 한도"


def _위반_복원본(r):
    g = _규범(r, "규범:시험/제36조①/한도/1")["근거"]["수치"][0]
    g["원문결함"], g["발췌"] = True, "복원본에 없는 말"


def _위반_고아(r):
    _개념(r, "세목:창업/지급수수료/멘토링비")["상위"] = None


def _위반_별칭(r):
    _개념(r, "품목:창업/철재")["별칭"] = ["자재"]
    _개념(r, "품목:창업/철재")["적용사업"] = None
    _개념(r, "세목:창업/지급수수료/멘토링비")["별칭"] = ["자재"]


def _위반_근거없음(r):
    _개념(r, "주체:진출자")["근거"] = []


def _위반_규범근거(r):
    _규범(r, "규범:시험/제36조①/의무/1")["근거"] = {"수치": [], "조건": [], "대상": []}


def _위반_필수칸(r):
    del _규범(r, "규범:시험/제36조①/한도/1")["계층"]


def _위반_대상(r):
    _규범(r, "규범:시험/제36조①/한도/1")["대상"] = ["세목:창업/없는세목"]


def _위반_예외(r):
    _규범(r, "규범:시험/제36조①/한도/1")["예외"] = ["규범:시험/없는항/허용/1"]


def _위반_관계(r):
    _사업묶음(r)["관계"][0]["dst"] = "비목:창업/없는비목"


def _위반_검수단계(r):
    _개념(r, "주체:진출자")["검수단계"] = "검수중"


def _위반_수치근거(r):
    _규범(r, "규범:시험/제36조①/한도/1")["근거"]["수치"] = []


def _위반_연산종류(r):
    수치 = _규범(r, "규범:시험/제36조①/한도/1")["수치"]
    del 수치["위반연산"]
    수치["발동연산"] = "초과"


def _위반_임계값연산(r):
    수치 = _규범(r, "규범:시험/제36조①/임계값/1")["수치"]
    del 수치["발동연산"]
    수치["위반연산"] = "초과"


def _위반_범위(r):
    # 공통 규범이 사업한정 노드를 가리키면 다른 사업에서 끊긴다
    _개념(r, "세목:창업/지급수수료/멘토링비")["적용사업"] = "테스트사업"
    r["공통"]["규범"].append({"id": "규범:공통/제1조/금지/1", "종류": "금지", "계층": "L1", "적용사업": None,
                            "대상": ["세목:창업/지급수수료/멘토링비"], "검수단계": "AI초안",
                            "근거": {"대상": [{"조문": "조:공통문서#제42조", "발췌": "재료비 기계장치 구분"}]}})


def _자체시험() -> int:
    사례: list[tuple[str, Callable[[dict], None], str, dict]] = [
        ("계층 순환", _위반_순환, "계층_순환", {}),
        ("서로소 위반(한 품목이 재료비 ⊥ 기계장치 둘에 속함)", _위반_서로소, "OWL_불만족", {}),
        ("치역 위반(사용의 치역이 비목이 아님)", _위반_치역, "OWL_불일치", {}),
        ("발췌 오류", _위반_발췌, "발췌_불일치", {}),
        ("원문결함 발췌가 복원본과 다름", _위반_복원본, "발췌_불일치", {"복원본": {"조:시험문서#제36조①": "다른 말"}}),
        ("고아 세목", _위반_고아, "고아", {}),
        ("별칭 충돌", _위반_별칭, "별칭_충돌", {}),
        ("근거 없는 노드", _위반_근거없음, "근거없음", {}),
        ("근거 없는 규범", _위반_규범근거, "근거없음", {}),
        ("필수 칸 누락", _위반_필수칸, "필수칸", {}),
        ("없는 대상 ID", _위반_대상, "ID_없음", {}),
        ("없는 예외 ID", _위반_예외, "ID_없음", {}),
        ("없는 관계 ID", _위반_관계, "ID_없음", {}),
        ("검수단계 값", _위반_검수단계, "검수단계", {}),
        ("AI검증 규범의 수치 근거 없음", _위반_수치근거, "수치근거", {}),
        ("한도에 발동연산", _위반_연산종류, "수치_연산", {}),
        ("임계값에 위반연산", _위반_임계값연산, "수치_연산", {}),
        ("문서키가 활성 판이 아님", lambda r: None, "문서키_활성아님", {"문서상태": {"문서A": "superseded", "문서B": "active"}}),
        ("공통 규범이 사업한정 노드를 가리킴", _위반_범위, "범위_밖", {}),
    ]
    실패수 = 0

    def 실행(원천, 문서상태=None, 복원본=None) -> 검사결과:
        결과 = 검사결과()
        펼침 = 코드_검사(원천, 결과, _가짜DB(문서상태), 복원본 or {})
        if 결과.통과:
            for 사업 in [None, *원천["사업"]]:
                HermiT_검사(펼침, 사업, 결과)
        return 결과

    기준 = 실행(_시험_원천())
    if 기준.통과:
        print("통과: 정상 입력은 코드 검사 + HermiT 를 통과한다")
    else:
        실패수 += 1
        print(f"실패: 정상 입력이 걸렸다: {기준.오류}")

    for 이름, 위반, 기대, 옵션 in 사례:
        원천 = _시험_원천()
        위반(원천)
        결과 = 실행(원천, 옵션.get("문서상태"), 옵션.get("복원본"))
        if 기대 in 결과.코드들():
            print(f"통과: {이름} → {기대}")
        else:
            실패수 += 1
            print(f"실패: {이름} → 기대 {기대}, 실제 {sorted(결과.코드들()) or '통과'}")

    # 복원본 대조 자체가 맞는 경우: 원문결함 발췌가 복원본에 있으면 DB 본문과 달라도 통과한다
    원천 = _시험_원천()
    _위반_복원본(원천)
    결과 = 실행(원천, None, {"조:시험문서#제36조①": "복원본에 없는 말"})
    if 결과.통과:
        print("통과: 원문결함 발췌는 복원본과 대조한다")
    else:
        실패수 += 1
        print(f"실패: 원문결함 대조: {결과.오류}")

    # --no-db: 발췌 오류를 보지 않지만 나머지는 본다
    원천 = _시험_원천()
    _위반_발췌(원천)
    결과 = 검사결과()
    코드_검사(원천, 결과, None, {})
    if "발췌_불일치" not in 결과.코드들() and any("DB 없음" in s for s in 결과.건너뜀):
        print("통과: DB 없이 돌리면 발췌 대조를 건너뛰고 건너뛰었다고 적는다")
    else:
        실패수 += 1
        print(f"실패: --no-db 동작: {결과.오류} / {결과.건너뜀}")

    전체 = len(사례) + 3
    print(f"selftest: {전체 - 실패수}/{전체} 통과")
    return 1 if 실패수 else 0


# CLI ──────────────────────────────────────────────────────────────────────

def _보고(결과: 검사결과) -> None:
    for e in 결과.오류:
        print(f"오류 [{e['코드']}] {e['위치']}: {e['내용']}")
    for e in 결과.경고:
        print(f"경고 [{e['코드']}] {e['위치']}: {e['내용']}")
    for s in 결과.건너뜀:
        print(f"건너뜀: {s}")
    print("통계:", json.dumps(결과.통계, ensure_ascii=False))
    print("검사 통과" if 결과.통과 else f"검사 실패: 오류 {len(결과.오류)}건")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="온톨로지 원천 JSON 검사")
    p.add_argument("사업키", nargs="*", help="schema.json 의 사업키. 없으면 파일이 있는 사업 전부")
    p.add_argument("--원천", default=str(원천_기본폴더), help="원천 폴더(기본 ontology/)")
    p.add_argument("--no-db", action="store_true", help="발췌 · 문서키 검사를 건너뛴다")
    p.add_argument("--owl-dir", default=None, help="생성한 OWL 을 저장할 폴더(기본: 저장하지 않음)")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    if a.self_test:
        return _자체시험()
    try:
        원천 = 원천_읽기(a.원천, a.사업키 or None)
    except 검사오류 as e:
        print(f"오류: {e}")
        return 1
    if a.no_db:
        결과, _ = 전체_검사(원천, None, OWL폴더=a.owl_dir)
    else:
        from _lib import db
        with db.connect(autocommit=True) as conn:
            with conn.cursor() as cur:
                결과, _ = 전체_검사(원천, cur, OWL폴더=a.owl_dir)
    _보고(결과)
    return 0 if 결과.통과 else 1


if __name__ == "__main__":
    sys.exit(main())
