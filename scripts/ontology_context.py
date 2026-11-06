#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""문맥 구성: 온톨로지 후보 + 검색 후보 + L3 → 판정 · 질문 프롬프트의 원문 블록 (docs/4_검색/4-4_검색_재설계.md ④).

새 경로의 orchestrate · question_generate 가 부른다(옛 `조문_후보()` 자리). LLM 을 쓰지 않는다. 조회 오류는 호출자에게 올린다(판단불가).

우선순위: L3 연결 조문 → 규범 근거 → 대상의 근거 조 전체 → 준용 깊이 1 → L3 미연결 전부 → 준용 깊이 2~3 → 검색 후보.
같은 조 · 항은 한 번만 넣고 경로를 합친다. 글자 상한을 넘으면 뒤에서 뺀다. L3 미연결 조문까지 못 넣으면 `L3_초과` 다(판단불가 + 화면 안내).
공개: 문맥_구성, 규범_요약, 근거경로들, 코드판정_문장맵, 항호_키.
실행: PYTHONIOENCODING=utf-8 python scripts/ontology_context.py --self-test
"""
from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assemble_context import 항_분할  # noqa: E402
from ontology_norm import 별지_행_내용, 조번호_표시  # noqa: E402

글자_상한 = 40_000

L3연결, 규범근거, 근거조전체, 준용1, L3미연결, 준용2, 검색후보 = 1, 2, 3, 4, 5, 6, 7
_블록제목 = (((L3연결,), "B1. 귀 기관 규정 (L3, 이 비목에 연결됨)"),
           ((규범근거, 근거조전체, 준용1), "B2. 이 비목에 적용되는 규정"),
           ((L3미연결,), "B2.5. 귀 기관 규정 (L3, 연결 확인 전)"),
           ((준용2,), "B2.7. 위 규정이 준용하는 조항"),
           ((검색후보,), "B3. 검색으로 보충한 규정"))
_항마커 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
모든비목 = "비목:창업/*"


def 항호_키(항호: str | None) -> str | None:
    """조문 ID 의 항호를 문장맵 키로 바꾼다. 별지 행은 그대로, `②표-4` · `②3호` 는 항 기호, 그 밖은 None(조 전체)."""
    if not 항호:
        return None
    if 항호.startswith("/"):
        return 항호
    return 항호[0] if 항호[0] in _항마커 else None


def _조_조각(본문: str | None) -> list[tuple[str | None, str]]:
    """조 본문 → (문장맵 키, 조각). 같은 항 기호가 되풀이되면 `#N` 을 붙인다(`llm_validate._항_추출` 과 같다)."""
    횟수: dict[str, int] = {}
    out: list[tuple[str | None, str]] = []
    for 항, 조각 in 항_분할(본문 or ""):
        키 = 항
        if 항:
            횟수[항] = 횟수.get(항, 0) + 1
            if 횟수[항] > 1:
                키 = f"{항}#{횟수[항]}"
        if 조각:
            out.append((키, 조각))
    return out


def _고른_조각(본문: str, 항호: str | None) -> list[tuple[str | None, str]]:
    """항호가 가리키는 조각. 못 찾으면 조 전체(근거를 빠뜨리지 않는다)."""
    키 = 항호_키(항호)
    if 키 and 키.startswith("/"):
        내용 = 별지_행_내용(본문, 키)
        if 내용:
            return [(키, 내용)]
    elif 키:
        # 표 안의 항 기호와 겹치면 키가 `③` · `③#2` 로 갈린다(제35조 표-4). 어느 쪽이 본문인지 모르므로 둘 다 넣는다
        고름 = [(k, c) for k, c in _조_조각(본문) if (k or "").split("#")[0] == 키]
        if 고름:
            return 고름
    return _조_조각(본문)


class _모음:
    """(문서, 조, 키) 단위로 항목을 모은다. 같은 조 · 항은 더 앞선 순위로 한 번만 남고 경로를 합친다."""

    def __init__(self) -> None:
        self.표: dict[tuple, dict] = {}
        self.조: set[tuple] = set()

    def 넣기(self, 순위: int, 종류: str, id_: int, 문서: str, 조번호: str, 헤더: str, 키: str | None, 조각: str,
           경로: list[str] | None = None) -> None:
        k = (문서, 조번호, 키)
        있음 = self.표.get(k)
        if 있음 is None:
            있음 = {"순위": 순위, "종류": 종류, "id": id_, "헤더": 헤더, "조각": 조각, "키": 키, "경로": []}
            self.표[k] = 있음
            self.조.add((문서, 조번호))
        있음["순위"] = min(있음["순위"], 순위)
        if 경로 and 경로 not in 있음["경로"]:
            있음["경로"].append(경로)

    def 있나(self, 문서: str, 조번호: str, 키: str | None) -> bool:
        return (문서, 조번호, 키) in self.표


def 문서_표시명(schema: dict | None) -> dict[str, str]:
    """doc_id → 화면에 쓰는 문서 이름."""
    return {(schema or {}).get("문서키", {}).get(k): v for k, v in ((schema or {}).get("문서 표시명") or {}).items()}


def _헤더(표시명: str | None, 조번호: str | None, 조제목: str | None) -> str:
    머리 = " ".join(x for x in (표시명, 조번호_표시(조번호)) if x)
    return f"{머리}({조제목})" if 조제목 else 머리


def _조문_읽기(cur, article_ids: list[int]) -> dict[int, dict]:
    if not article_ids:
        return {}
    행들 = cur.execute("""SELECT article_id, doc_id, "조번호", "조제목", "본문" FROM corpus.doc_articles
                          WHERE article_id = ANY(%s)""", (article_ids,)).fetchall()
    return {r[0]: {"article_id": r[0], "doc_id": r[1], "조번호": r[2], "조제목": r[3], "본문": r[4] or ""} for r in 행들}


def _온톨로지_넣기(모음: _모음, 근거행: list[dict], 조문: dict[int, dict], 표시명: dict[str, str]) -> None:
    for 행 in 근거행:
        글 = 조문.get(행.get("article_id"))
        if 글 is None:
            continue                                    # 활성 문서에 없거나 조문을 특정하지 못한 대상
        문서, 조번호, 본문 = 글["doc_id"], 글["조번호"], 글["본문"]
        헤더 = _헤더(표시명.get(문서) or 문서, 조번호, 글["조제목"])
        출처, 깊이, 항호, 경로 = 행.get("출처"), 행.get("깊이") or 0, 행.get("항호"), 행.get("경로")

        def 넣기(순위: int, 조각들: list[tuple[str | None, str]]) -> None:
            for 키, 조각 in 조각들:
                모음.넣기(순위, "article", 글["article_id"], 문서, 조번호, 헤더, 키, 조각, 경로)

        if 출처 == "규범근거":
            넣기(규범근거, _고른_조각(본문, 항호))
            if not (항호 or "").startswith("/"):        # 별지는 해당 행만. 표 전체는 다른 비목 칸까지 들어간다
                넣기(근거조전체, _조_조각(본문))
        elif 출처 == "개념근거":
            넣기(근거조전체, _고른_조각(본문, 항호) if (항호 or "").startswith("/") else _조_조각(본문))
        elif 깊이 <= 1:                                 # 준용 · 위임 깊이 1, 시작 조문을 가리키는 앞 조문
            넣기(준용1, _고른_조각(본문, 항호))
        else:
            넣기(준용2, _고른_조각(본문, 항호))


def _l3_넣기(모음: _모음, l3: list[dict], 대상노드: set[str]) -> None:
    for r in l3 or []:
        연결 = r.get("concept_ids")
        if 연결 is None:
            순위 = L3미연결
        elif not 연결 or (set(연결) & 대상노드) or 모든비목 in 연결:   # [] = 확인했고 해당 세목 없음(공통으로 항상 넣는다)
            순위 = L3연결
        else:
            continue                                    # 확인했고 다른 세목에 연결된 조문
        헤더 = f'{r["조번호"]}({r.get("조제목") or ""})'
        for 키, 조각 in _조_조각(r.get("본문")):
            모음.넣기(순위, "l3", r["article_id"], f'L3:{r["article_id"]}', r["조번호"], 헤더, 키, 조각)


def _검색_넣기(cur, 모음: _모음, 검색: dict | None, 표시명: dict[str, str]) -> None:
    """검색 청크와 참조 확장 1단. 온톨로지 후보에 없는 것만 남긴다."""
    청크들 = (검색 or {}).get("top5") or []
    if 청크들:
        행들 = cur.execute("""SELECT chunk_id, doc_id, "조번호", "조제목", "항호", text FROM corpus.chunks
                              WHERE chunk_id = ANY(%s)""", (청크들,)).fetchall()
        순서 = {c: i for i, c in enumerate(청크들)}
        for r in sorted(행들, key=lambda x: 순서.get(x[0], 999)):
            chunk_id, 문서, 조번호, 조제목, 항호, 글 = r
            헤더 = _헤더(표시명.get(문서) or 문서, 조번호, 조제목)
            for 항, 조각 in 항_분할(글 or ""):
                키 = 항 or 항호
                # 항 없는 청크는 그 조가 이미 들어와 있으면 겹친다. 조 단위 조각을 뒤에서 빠진 항만 넣는다(아래 참조 확장과 같다)
                if 키 is None and (문서, 조번호) in 모음.조:
                    continue
                if not 조각 or 모음.있나(문서, 조번호, 키):
                    continue
                모음.넣기(검색후보, "chunk", chunk_id, 문서, 조번호, 헤더, 키, 조각)
    참조 = (검색 or {}).get("참조확장") or []
    조문 = _조문_읽기(cur, list(참조))
    for aid in 참조:
        글 = 조문.get(aid)
        if 글 is None:
            continue
        헤더 = _헤더(표시명.get(글["doc_id"]) or 글["doc_id"], 글["조번호"], 글["조제목"])
        for 키, 조각 in _조_조각(글["본문"]):
            if not 모음.있나(글["doc_id"], 글["조번호"], 키):
                모음.넣기(검색후보, "article", aid, 글["doc_id"], 글["조번호"], 헤더, 키, 조각)


def _자르기(항목들: list[dict], 상한: int) -> tuple[list[dict], list[dict]]:
    """순위대로 줄 세운 항목을 뒤에서 빼 상한 안으로 맞춘다. (남긴 것, 뺀 것)."""
    합 = sum(len(x["조각"]) + len(x["헤더"]) for x in 항목들)
    남김 = list(항목들)
    뺌: list[dict] = []
    while 남김 and 합 > 상한:
        x = 남김.pop()
        합 -= len(x["조각"]) + len(x["헤더"])
        뺌.append(x)
    return 남김, 뺌


def 문맥_구성(cur, *, 근거행: list[dict], 대상노드: set[str] | list[str] | None = None, l3: list[dict] | None = None,
          검색: dict | None = None, schema: dict | None = None, 상한: int = 글자_상한) -> dict:
    """반환: {블록: [(제목, [(종류, id, 헤더, 조각, 키)])], 항목수, 글자수, 빠진수, L3_초과, 온톨로지항목수}.

    근거행은 ontology_lookup.근거_조문 결과다. `대상노드` 는 대상 · 상위 노드(L3 연결 판단). `l3` 항목의 `concept_ids` 는
    None = 미연결, [] = 확인했고 해당 세목 없음.
    """
    표시명 = 문서_표시명(schema)
    모음 = _모음()
    조문 = _조문_읽기(cur, sorted({r["article_id"] for r in 근거행 if r.get("article_id") is not None}))
    _l3_넣기(모음, l3 or [], set(대상노드 or ()))
    _온톨로지_넣기(모음, 근거행, 조문, 표시명)
    온톨로지항목수 = sum(1 for x in 모음.표.values() if x["순위"] in (규범근거, 근거조전체, 준용1, 준용2))
    _검색_넣기(cur, 모음, 검색, 표시명)
    항목들 = sorted(모음.표.values(), key=lambda x: x["순위"])            # 순위 안에서는 넣은 순서를 지킨다
    남김, 뺌 = _자르기(항목들, 상한)
    블록 = []
    for 순위들, 제목 in _블록제목:
        묶음 = [(x["종류"], x["id"], x["헤더"], x["조각"], x["키"]) for x in 남김 if x["순위"] in 순위들]
        if 묶음:
            블록.append((제목, 묶음))
    return {"블록": 블록, "항목수": len(남김), "글자수": sum(len(x["조각"]) + len(x["헤더"]) for x in 남김),
            "빠진수": len(뺌), "L3_초과": any(x["순위"] <= L3미연결 and 모음_L3(x) for x in 뺌), "온톨로지항목수": 온톨로지항목수}


def 모음_L3(x: dict) -> bool:
    return x["종류"] == "l3"


def _발췌(n: dict, 칸: str | None = None) -> str:
    근거 = n.get("근거") or []
    후보 = [g for g in 근거 if not 칸 or g.get("칸") == 칸] or list(근거)
    return (후보[0].get("발췌") or "").strip() if 후보 else ""


def 규범_요약(규범결과: dict, 게이트결과: dict, *, 분류표시: str | None = None, 확정: bool = False, l3있음: bool = False) -> str | None:
    """B4 문장. 코드가 비교를 끝낸 결과와 규범을 평문으로 적는다. 내부 ID 를 넣지 않는다."""
    규범들 = 규범결과.get("규범") or []
    제외 = set(게이트결과.get("제외규범") or [])
    쓴 = set(게이트결과.get("쓴규범") or [])
    조건구분 = {c["문장"]: c.get("구분") for c in 게이트결과.get("조건") or []}
    줄: list[str] = []
    if 분류표시:
        줄.append(f"분류: {분류표시} ({'사용자가 확정함' if 확정 else '추천값, 사용자 확정 전'})")
    for n in 규범들:
        종류, 글 = n.get("종류"), _발췌(n, "수치") or _발췌(n)
        if 종류 == "우선순위" or not 글 or n.get("norm_id") in 제외:
            continue
        if not n.get("verified") or n.get("해석미결"):
            줄.append(f"- 확정되지 않은 {종류} 규정: 「{글}」 코드가 비교하지 않았다. 원문을 읽고 판단한다.")
        elif 종류 in ("한도", "임계값") and n.get("norm_id") in 쓴:
            줄.append(f"- {종류}: 「{글}」 금액 비교를 끝냈고 넘지 않았다." if 종류 == "한도"
                     else f"- {종류}: 「{글}」 금액 비교를 끝냈다. 생긴 의무는 아래에 있다.")
        elif 종류 in ("한도", "임계값"):
            줄.append(f"- {종류}: 「{글}」 입력이 모자라거나 예외가 있어 비교하지 못했다.")
        elif 종류 == "금지":
            줄.append(f"- 금지: 「{글}」 조건에 해당하는지 원문과 사용자 답변으로 판단한다.")
        elif 종류 == "요건":
            줄.append(f"- 요건: 「{글}」 충족 여부를 판단한다.")
        elif 종류 == "허용":
            줄.append(f"- 허용: 「{글}」")
    for 문장, 구분 in 조건구분.items():
        if 구분 == "의무":
            줄.append(f"- 지켜야 할 의무: {문장}")
        elif 구분 in ("입력비움", "위임", "두기준", "미확정", "세목미정"):
            줄.append(f"- 확인 필요: {문장}")
    우선 = [n for n in 규범들 if n.get("종류") == "우선순위"]
    if l3있음 and any((n.get("조건") or {}).get("범위") == "상충시" for n in 우선):
        글 = next((_발췌(n) for n in 우선 if (n.get("조건") or {}).get("범위") == "상충시"), "")
        줄.append(f"- 적용 순서: 「{글}」 귀 기관 규정이 상위 규정보다 느슨하면 상위 규정으로 판정하고, 요약에 "
                 "「주관기관 안내는 허용하지만 관리기준이 우선」이라고 적는다. 더 엄격하면 귀 기관 규정을 따른다.")
    return "\n".join(줄) if 줄 else None


def 근거경로들(근거행: list[dict], schema: dict, 상한: int | None = 3) -> list[str]:
    """화면 "근거 경로" 문장. 경로가 있는 것을 먼저 쓰고, 다른 문장에 포함된 문장은 뺀다. 화면에는 상한까지만 낸다."""
    from ontology_lookup import 근거경로_문장

    문장들: list[str] = []
    for 행 in 근거행:
        문장 = 근거경로_문장(행.get("경로") or [], schema)
        if 문장 and 문장 not in 문장들:
            문장들.append(문장)
    문장들.sort(key=lambda s: -s.count("→"))
    남김: list[str] = []
    for s in 문장들:
        if not any(s == t or f"{s} →" in f"{t} →" for t in 남김):
            남김.append(s)
    return 남김[:상한] if 상한 else 남김


def 코드판정_문장맵(cur, 규범: dict) -> dict:
    """코드 판정에 쓴 규범의 근거를 인용 치환용 문장맵으로 바꾼다. 수치 → 조건 → 대상 순이다. LLM 이 없어 S 번호는 내부용이다."""
    순서 = {"수치": 0, "조건": 1, "대상": 2}
    문장맵: dict = {}
    이미: set[tuple] = set()
    for g in sorted(규범.get("근거") or [], key=lambda x: 순서.get(x.get("칸"), 9)):
        k = (g.get("doc_id"), g.get("조번호"), 항호_키(g.get("항호")))
        if k in 이미:
            continue
        이미.add(k)
        행 = cur.execute("""SELECT article_id FROM corpus.doc_articles WHERE doc_id = %s AND "조번호" = %s""",
                         (k[0], k[1])).fetchone()
        if 행:
            문장맵[f"S{len(문장맵) + 1:02d}"] = ("article", 행[0], k[2])
    return 문장맵


# ---------------------------------------------------------------- self-test

class _가짜커서:
    """조문 · 청크 조회만 흉내 낸다."""

    def __init__(self, 조문: list[tuple], 청크: list[tuple] | None = None) -> None:
        self.조문, self.청크, self.행 = 조문, 청크 or [], []

    def execute(self, sql: str, 인자=None):
        ids = set((인자 or [[]])[0])
        self.행 = ([r for r in self.조문 if r[0] in ids] if "doc_articles" in sql
                  else [r for r in self.청크 if r[0] in ids])
        return self

    def fetchall(self):
        return self.행


def _자체시험() -> int:
    제36조 = (1, "관리기준", "제36조", "지급수수료", "① 멘토링비는 1인 1일 30만원 한도 ② 수수료는 실비")
    제33조 = (2, "관리기준", "제33조", "비목", "① 비목은 표와 같다 ② 지침 제42조를 준용한다")
    제42조 = (3, "지침", "제42조", "지급수수료", "① 지침 본문")
    제50조 = (4, "지침", "제50조", "기타", "먼 조문")
    별지2 = (5, "관리기준", "별지2", "비목 해설", "| 비목 | 구분 | 내용 |\n|---|---|---|\n| 지급수수료 | 유의사항 | 소속 인력 불가 |\n| 재료비 | 유의사항 | 재료 내용 |")
    표 = {"문서키": {"모두의창업": "관리기준", "지침": "지침"}, "문서 표시명": {"모두의창업": "모두의 창업", "지침": "지침"}}
    cur = _가짜커서([제36조, 제33조, 제42조, 제50조, 별지2],
                 [(100, "지침", "제42조", "지급수수료", "①", "① 지침 본문"), (101, "지침", "제60조", "다른 조", None, "검색 조문")])
    행들 = [
        {"doc_id": "관리기준", "조번호": "제36조", "항호": "①", "article_id": 1, "깊이": 0, "출처": "규범근거", "경로": ["조:모두의창업#제36조①"]},
        {"doc_id": "관리기준", "조번호": "별지2", "항호": "/지급수수료/유의사항", "article_id": 5, "깊이": 0, "출처": "규범근거", "경로": ["조:모두의창업#별지2/지급수수료/유의사항"]},
        {"doc_id": "관리기준", "조번호": "제33조", "항호": "②", "article_id": 2, "깊이": 0, "출처": "준용_앞", "경로": ["조:모두의창업#제33조②"]},
        {"doc_id": "지침", "조번호": "제42조", "항호": None, "article_id": 3, "깊이": 1, "출처": "준용", "경로": ["조:모두의창업#제33조②", "조:지침#제42조"]},
        {"doc_id": "지침", "조번호": "제50조", "항호": None, "article_id": 4, "깊이": 2, "출처": "준용", "경로": ["조:지침#제42조", "조:지침#제50조"]},
        {"doc_id": None, "조번호": None, "항호": None, "article_id": None, "깊이": 1, "출처": "위임", "경로": ["미특정:가이드"], "건너뜀": "조문 미특정"},
    ]
    t: list[tuple[str, bool]] = []
    l3 = [{"article_id": 9, "조번호": "제1조", "조제목": "연결", "본문": "연결 조문", "concept_ids": ["세목:창업/지급수수료/멘토링비"]},
          {"article_id": 10, "조번호": "제2조", "조제목": "공통", "본문": "공통 조문", "concept_ids": []},
          {"article_id": 11, "조번호": "제3조", "조제목": "다른", "본문": "다른 세목 조문", "concept_ids": ["세목:창업/재료비/원재료"]},
          {"article_id": 12, "조번호": "제4조", "조제목": "미연결", "본문": "미연결 조문", "concept_ids": None}]
    m = 문맥_구성(cur, 근거행=행들, 대상노드={"세목:창업/지급수수료/멘토링비"}, l3=l3,
              검색={"top5": [100, 101], "참조확장": []}, schema=표)
    제목들 = [b[0] for b in m["블록"]]
    전체 = [i for _, 항목 in m["블록"] for i in 항목]
    글들 = [i[3] for i in 전체]
    t.append(("블록 순서: L3 연결 → 규범 → L3 미연결 → 준용 2~3 → 검색", [x[:4] for x in 제목들] == ["B1. ", "B2. ", "B2.5", "B2.7", "B3. "]))
    t.append(("L3 연결 · 공통은 B1", [i[3] for i in m["블록"][0][1]] == ["연결 조문", "공통 조문"]))
    t.append(("다른 세목에 연결된 L3 조문은 뺀다", "다른 세목 조문" not in 글들))
    t.append(("미연결 L3 는 B2.5", m["블록"][2][1][0][3] == "미연결 조문"))
    t.append(("규범 근거 항 먼저, 조 전체도 들어온다", 글들.index("① 멘토링비는 1인 1일 30만원 한도") < 글들.index("② 수수료는 실비")))
    t.append(("별지는 해당 행만", "소속 인력 불가" in 글들 and not any("재료 내용" in x for x in 글들)))
    t.append(("준용_앞은 해당 항만", "② 지침 제42조를 준용한다" in 글들 and "① 비목은 표와 같다" not in 글들))
    t.append(("같은 조 · 항은 한 번만(검색 청크 제42조① 빠짐)", 글들.count("① 지침 본문") == 1 and [i[0] for i in 전체 if i[3] == "① 지침 본문"] == ["article"]))
    t.append(("온톨로지에 없는 검색 청크는 B3", m["블록"][-1][1][0][3] == "검색 조문"))
    t.append(("미특정 행은 문맥에 안 들어가고 예외도 없다", m["항목수"] == len(전체)))
    표겹침 = "① 본문 하나\n표: ① 칸 ② 칸 ③ 칸\n② 본문 둘\n③ 본문 셋"
    t.append(("표 안 기호와 겹치는 항은 본문까지 고른다", any("본문 셋" in c for _, c in _고른_조각(표겹침, "③"))))
    t.append(("헤더는 문서 표시명", any(i[2] == "모두의 창업 제36조(지급수수료)" for i in 전체)))
    짧음 = 문맥_구성(cur, 근거행=행들, 대상노드=set(), 검색={"top5": [100, 101]}, schema=표, 상한=60)
    t.append(("상한 초과: 뒤에서 뺀다(검색 먼저)", 짧음["빠진수"] > 0 and "검색 조문" not in [i[3] for _, 항목 in 짧음["블록"] for i in 항목]))
    t.append(("상한 초과 + L3 없음 → L3_초과 아님", 짧음["L3_초과"] is False))
    넘침 = 문맥_구성(cur, 근거행=[], 대상노드=set(), schema=표, 상한=10,
                 l3=[{"article_id": 1, "조번호": "제1조", "조제목": "가", "본문": "가" * 40, "concept_ids": None}])
    t.append(("L3 미연결을 못 넣으면 L3_초과", 넘침["L3_초과"] is True and 넘침["항목수"] == 0))
    t.append(("항호 키", (항호_키("②표-4"), 항호_키("9호"), 항호_키("/비목/칸"), 항호_키(None)) == ("②", None, "/비목/칸", None)))
    조각 = _조_조각("① 하나 ② 둘 ① 다시")
    t.append(("되풀이 항 기호는 #2", [k for k, _ in 조각] == ["①", "②", "①#2"]))
    from ontology_lookup import 근거경로_문장  # noqa: F401
    t.append(("근거 경로: 긴 경로 먼저, 포함된 문장은 뺀다",
              근거경로들([{"경로": ["조:지침#제42조"]}, {"경로": ["조:모두의창업#제33조②", "조:지침#제42조"]}], 표)
              == ["모두의 창업 제33조② → 지침 제42조"]))
    t.append(("근거 경로 · 헤더: 부칙 제1조로 읽힌다",
              근거경로들([{"경로": ["조:지침#부칙 제2조", "미특정:사업별 세부관리기준"]}], 표) == ["지침 부칙 제2조 → 사업별 세부관리기준"]
              and _헤더("지침", "부칙 제1조", None) == "지침 부칙 제1조"))
    요약 = 규범_요약({"규범": [
        {"norm_id": "a", "종류": "한도", "verified": True, "근거": [{"칸": "수치", "발췌": "1인 1일 30만원 한도"}]},
        {"norm_id": "b", "종류": "우선순위", "verified": True, "조건": {"범위": "상충시"}, "근거": [{"칸": "대상", "발췌": "본 관리기준을 우선"}]},
        {"norm_id": "c", "종류": "금지", "verified": False, "근거": [{"칸": "대상", "발췌": "미확정 금지"}]}]},
        {"쓴규범": ["a"], "조건": [{"문장": "공제 후 집행", "구분": "의무"}]}, 분류표시="지급수수료 > 멘토링비", 확정=True, l3있음=True)
    t.append(("B4: 비교 끝남 · 의무 · 우선순위 · 미확정 표시, 내부 ID 없음",
              "넘지 않았다" in 요약 and "공제 후 집행" in 요약 and "관리기준이 우선" in 요약 and "확정되지 않은" in 요약 and "규범:" not in 요약))
    실패 = [x for x, ok in t if not ok]
    for x, ok in t:
        print("ok  " if ok else "FAIL", x)
    print(f"selftest: {len(t) - len(실패)}/{len(t)} 통과")
    return 1 if 실패 else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_자체시험())
    print(__doc__)
