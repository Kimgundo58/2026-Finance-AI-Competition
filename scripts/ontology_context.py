#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""문맥 구성: 온톨로지 후보 + 검색 후보 + L3 → 판정 · 질문 프롬프트의 원문 블록 (docs/4_검색/4-4_검색_재설계.md ④).

새 경로의 orchestrate · question_generate 가 부른다(옛 `조문_후보()` 자리). LLM 을 쓰지 않는다. 조회 오류는 호출자에게 올린다(확인 필요, 실패).

우선순위: L3 연결 조문 → 규범 근거 → 대상의 근거 조 전체 → 준용 깊이 1 → L3 미연결 전부 → 자금 적용 미정 공통 조 → 준용 깊이 2~3 → 검색 후보.
같은 조 · 항은 한 번만 넣고 경로를 합친다. 글자 상한을 넘으면 뒤에서 뺀다. L3 미연결 조문이나 B2.6(금지 문장이 든다)까지 못 넣으면 `L3_초과` 다(확인 필요 + 화면 안내).
다른 자금의 조문(ontology_fund)은 온톨로지 후보 · 검색 후보 어느 쪽으로 들어와도 뺀다. L3 는 거르지 않는다.
최종 판정의 근거한정 모드는 조회한 조·항만 넣고 조 전체 자동 확장과 검색 보충을 하지 않는다.
공개: 문맥_구성, 규범_요약, 근거경로들, 코드판정_문장맵, 항호_키.
실행: PYTHONIOENCODING=utf-8 python scripts/ontology_context.py --self-test
"""
from __future__ import annotations

import sys
import re
import unicodedata
from pathlib import Path

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assemble_context import 항_분할  # noqa: E402
import ontology_fund  # noqa: E402
import ontology_table_evidence  # noqa: E402
from ontology_gate import _자연어조건, 경로밖금지_구분, 판단_조건  # noqa: E402
from ontology_norm import 모든비목, 별지_행_내용, 조번호_표시, 발췌_정규화, 발췌_포함, 확정_규범  # noqa: E402

글자_상한 = 60_000    # 2026-11-10 운영: 모두의 창업 기관 L3 를 넣은 창업활동비 문맥 최대 55,524자. 40,000 이면 L3상한초과 확인 필요

L3연결, 규범근거, 근거조전체, 준용1, L3미연결, 공통미정, 품목표, 준용2, 검색후보 = 1, 2, 3, 4, 5, 6, 6.5, 7, 8
_블록제목 = (((L3연결,), "B1. 귀 기관 규정 (L3, 이 비목에 연결됨)"),
           ((규범근거, 근거조전체, 준용1), "B2. 이 비목에 적용되는 규정"),
           ((L3미연결,), "B2.5. 귀 기관 규정 (L3, 연결 확인 전)"),
           ((공통미정,), "B2.6. 사업비 공통 규정 (이 자금에 적용되는지 정해지지 않음)"),
           ((품목표,), "B2.65. 품목 표 (입력 품목과 같은 물건인지 대조)"),
           ((준용2,), "B2.7. 위 규정이 준용하는 조항"),
           ((검색후보,), "B3. 검색으로 보충한 규정"))
_항마커 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


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


def _표_항호(항호: str | None) -> bool:
    return bool(re.fullmatch(r'[①②]?표-\d+', 항호 or ''))


def _표_범위(본문: str, 조번호: str, 항호: str) -> tuple[int, int]:
    if 항호.startswith('②'):
        return ontology_table_evidence.내부_표_구간(본문, 조번호, 항호)
    return ontology_table_evidence.앞쪽_표_구간(본문, 조번호, 항호)


def _근거_조각(본문: str, 항호: str | None, 조번호: str | None = None) -> list[tuple[str | None, str]]:
    """조회한 위치만 읽는다. 위치를 못 찾았다고 조 전체로 넓히지 않는다.

    항호가 없는 근거는 원래 조 단위 근거다. 호·표의 항 기호는 해당 항을
    보존하고, 별지 경로와 반복 항 번호는 인용 문장맵에서 같은 위치를 쓴다.
    """
    if not 본문 or not 본문.strip():
        raise ValueError("선택 근거의 원문이 비어 있음")
    if not 항호:
        # 항호가 없는 출처는 조 전체가 지정 범위다. 본문 속 표·목록의
        # ①②가 발췌 문장 중간에 있어도 구조상 별도 항으로 분해하지 않는다.
        return [(None, 본문)]
    if _표_항호(항호):
        시작, 끝 = _표_범위(본문, 조번호, 항호)
        return [(None, 본문[시작:끝])]
    키 = 항호_키(항호)
    if 키 is None:
        # 항 기호 없이 호만 있는 위치(`2호` · `10호` · `2호의2`)는 문장맵 키가 없다. 조 전체가 범위다.
        # `ontology_evidence._위치_확인` 이 같은 값을 통과시키므로 여기서 확인 필요로 막지 않는다(리뷰 28-8)
        return [(None, 본문)]
    if 키.startswith("/"):
        내용 = 별지_행_내용(본문, 키)
        if 내용:
            return [(키, 내용)]
    else:
        조각들 = _조_조각(본문)
        if "#" in 항호:
            고름 = [(k, c) for k, c in 조각들 if k == 항호]
        else:
            고름 = [(k, c) for k, c in 조각들 if (k or "").split("#")[0] == 키]
        if 고름:
            return 고름
    raise ValueError("선택 근거의 원문 위치를 확인하지 못함")


def _정규화_원문위치(원문: str) -> tuple[str, list[tuple[int, int]]]:
    """발췌 검사와 같은 정규화를 하되 각 글자의 원문 좌표를 보존한다."""
    따옴표 = str.maketrans({"“": '"', "”": '"', "″": '"', "‘": "'", "’": "'"})
    글자: list[str] = []
    좌표: list[tuple[int, int]] = []
    i = 0
    while i < len(원문):
        시작 = i
        i += 1
        # NFC 결합 문자와 한글 자모는 원문 범위를 함께 잡는다.
        def 자모(c: str) -> bool:
            n = ord(c)
            return 0x1100 <= n <= 0x11FF or 0xA960 <= n <= 0xA97F or 0xD7B0 <= n <= 0xD7FF
        while i < len(원문) and (unicodedata.combining(원문[i]) or (자모(원문[i - 1]) and 자모(원문[i]))):
            i += 1
        for c in unicodedata.normalize("NFC", 원문[시작:i]).translate(따옴표):
            글자.append(c)
            좌표.append((시작, i))
    중간 = "".join(글자)
    if 중간 != unicodedata.normalize("NFC", 원문).translate(따옴표):
        raise ValueError("원문 정규화 위치를 확인하지 못함")
    페이지 = re.compile(r"(?:(?<=\s)|^)-\s*\d{1,3}\s*-(?=\s|$)")
    문, 위치, 다음 = [], [], 0
    for match in 페이지.finditer(중간):
        문.extend(중간[다음:match.start()])
        위치.extend(좌표[다음:match.start()])
        문.append(" ")
        위치.append((좌표[match.start()][0], 좌표[match.end() - 1][1]))
        다음 = match.end()
    문.extend(중간[다음:])
    위치.extend(좌표[다음:])
    결과, 범위 = [], []
    for c, (start, end) in zip(문, 위치):
        if c.isspace():
            if 결과 and 결과[-1] == " ":
                범위[-1] = (범위[-1][0], end)
            else:
                결과.append(" ")
                범위.append((start, end))
        else:
            결과.append(c)
            범위.append((start, end))
    if 결과 and 결과[0] == " ":
        결과.pop(0)
        범위.pop(0)
    if 결과 and 결과[-1] == " ":
        결과.pop()
        범위.pop()
    정규화 = "".join(결과)
    if 정규화 != 발췌_정규화(원문):
        raise ValueError("원문 정규화 결과가 일치하지 않음")
    return 정규화, 범위


def _발췌_구간(조각들: list[tuple[str | None, str]], 발췌들: list[str],
           전체키: set[str | None] | None = None, *, 중복모두: bool = False) -> dict[str | None, list[tuple[int, int]]]:
    """축소할 발췌는 유일한 연속 구간, 전체를 보존할 곳은 포함 여부를 확인한다."""
    정규화된 = [(키, 글, *_정규화_원문위치(글)) for 키, 글 in 조각들]
    결과: dict[str | None, list[tuple[int, int]]] = {}
    for 발췌 in dict.fromkeys(발췌들):
        찾을말 = 발췌_정규화(발췌)
        if not 찾을말:
            raise ValueError("선택 근거의 발췌가 비어 있음")
        발견: list[tuple[str | None, int, int]] = []
        for 키, _글, 정규화, 위치 in 정규화된:
            시작 = 정규화.find(찾을말)
            while 시작 >= 0:
                발견.append((키, 위치[시작][0], 위치[시작 + len(찾을말) - 1][1]))
                시작 = 정규화.find(찾을말, 시작 + 1)
        if not 발견:
            raise ValueError("선택 발췌의 원문 위치가 없거나 모호함")
        if len(발견) > 1:
            if 전체키 is not None and all(키 in 전체키 for 키, _, _ in 발견):
                continue
            if not 중복모두:
                raise ValueError("선택 발췌의 원문 위치가 없거나 모호함")
            for 키, 시작, 끝 in 발견:
                결과.setdefault(키, []).append((시작, 끝))
            continue
        키, 시작, 끝 = 발견[0]
        결과.setdefault(키, []).append((시작, 끝))
    return 결과


class _모음:
    """(문서, 조, 키) 단위로 항목을 모은다. 같은 조 · 항은 더 앞선 순위로 한 번만 남고 경로를 합친다."""

    def __init__(self) -> None:
        self.표: dict[tuple, dict] = {}
        self.조: set[tuple] = set()

    def 넣기(self, 순위: int, 종류: str, id_: int, 문서: str, 조번호: str, 헤더: str, 키: str | None, 조각: str,
           경로: list[str] | None = None, 선택위치: set[tuple] | None = None) -> None:
        k = (문서, 조번호, 키)
        있음 = self.표.get(k)
        if 있음 is None:
            있음 = {"순위": 순위, "종류": 종류, "id": id_, "헤더": 헤더, "조각": 조각, "키": 키, "경로": []}
            self.표[k] = 있음
            self.조.add((문서, 조번호))
        있음["순위"] = min(있음["순위"], 순위)
        if 경로 and 경로 not in 있음["경로"]:
            있음["경로"].append(경로)
        if 선택위치:
            있음.setdefault('선택위치', set()).update(선택위치)

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


def _온톨로지_넣기(모음: _모음, 근거행: list[dict], 조문: dict[int, dict], 표시명: dict[str, str], *, 근거한정: bool = False) -> None:
    for 행 in 근거행:
        글 = 조문.get(행.get("article_id"))
        if 글 is None:
            if 근거한정 and 행.get("건너뜀") != "조문 아님":
                raise ValueError("선택 근거의 활성 원문을 확인하지 못함")
            continue                                    # 활성 문서에 없거나 조문을 특정하지 못한 대상
        문서, 조번호, 본문 = 글["doc_id"], 글["조번호"], 글["본문"]
        헤더 = _헤더(표시명.get(문서) or 문서, 조번호, 글["조제목"])
        출처, 깊이, 항호, 경로 = 행.get("출처"), 행.get("깊이") or 0, 행.get("항호"), 행.get("경로")

        def 넣기(순위: int, 조각들: list[tuple[str | None, str]]) -> None:
            for 키, 조각 in 조각들:
                모음.넣기(순위, "article", 글["article_id"], 문서, 조번호, 헤더, 키, 조각, 경로)

        if 근거한정:
            순위 = (품목표 if 행.get("품목표") else 공통미정 if 행.get("공통미정") else
                  규범근거 if 출처 == "규범근거" else 근거조전체 if 출처 == "개념근거" else
                  준용1 if 깊이 <= 1 else 준용2)
            넣기(순위, _근거_조각(본문, 항호, 조번호))
            continue

        if 행.get("품목표"):
            넣기(품목표, _고른_조각(본문, 항호))
        elif 행.get("공통미정"):
            넣기(공통미정, _조_조각(본문) if 출처 == "규범근거" else _고른_조각(본문, 항호))   # 항은 뒤에서 다른 자금 항을 뺀다
        elif 출처 == "규범근거":
            넣기(규범근거, _고른_조각(본문, 항호))
            if not (항호 or "").startswith("/"):        # 별지는 해당 행만. 표 전체는 다른 비목 칸까지 들어간다
                넣기(근거조전체, _조_조각(본문))
        elif 출처 == "개념근거":
            넣기(근거조전체, _고른_조각(본문, 항호) if (항호 or "").startswith("/") else _조_조각(본문))
        elif 깊이 <= 1:                                 # 준용 · 위임 깊이 1, 시작 조문을 가리키는 앞 조문
            넣기(준용1, _고른_조각(본문, 항호))
        else:
            넣기(준용2, _고른_조각(본문, 항호))


def _발췌근거_넣기(모음: _모음, 근거행: list[dict], 조문: dict[int, dict], 표시명: dict[str, str],
             근거발췌: dict[tuple, list[str]]) -> None:
    """선택된 근거의 조·항·표 범위를 DB 원문에서 보존한다."""
    선택 = set()
    for 위치 in 근거발췌:
        if not isinstance(위치, tuple) or len(위치) != 3:
            raise ValueError("선택 근거 위치가 잘못됨")
        선택.add((위치[0], 위치[1], 위치[2] or None))
    처리 = set()
    항목 = {}
    for 행 in 근거행:
        글 = 조문.get(행.get("article_id"))
        if 글 is None:
            if 행.get("건너뜀") != "조문 아님":
                raise ValueError("선택 근거의 활성 원문을 확인하지 못함")
            continue
        문서, 조번호, 본문 = 글["doc_id"], 글["조번호"], 글["본문"]
        위치 = (문서, 조번호, 행.get("항호") or None)
        if 위치 in 선택:
            처리.add(위치)
        elif 행.get("출처") == "규범근거":
            raise ValueError("선택 규범의 근거 위치가 없음")
        표범위 = _표_범위(본문, 조번호, 행['항호']) if _표_항호(행.get('항호')) else None
        조각들 = [(None, 본문)] if 표범위 else _근거_조각(본문, 행.get("항호"), 조번호)
        출처, 깊이 = 행.get("출처"), 행.get("깊이") or 0
        순위 = (품목표 if 행.get("품목표") else 공통미정 if 행.get("공통미정") else
              규범근거 if 출처 == "규범근거" else 근거조전체 if 출처 == "개념근거" else
              준용1 if 깊이 <= 1 else 준용2)
        for 키, 원문 in 조각들:
            묶음키 = (문서, 조번호, 키)
            기존 = 항목.get(묶음키)
            if 기존 is None:
                기존 = {"원문": 원문, "전체": False, "구간": [], "순위": 순위,
                      "id": 글["article_id"], "헤더": _헤더(표시명.get(문서) or 문서, 조번호, 글["조제목"]), "경로": [], "선택위치": set()}
                항목[묶음키] = 기존
            elif 기존["원문"] != 원문 or 기존["id"] != 글["article_id"]:
                raise ValueError("선택 근거의 원문 위치가 중복됨")
            기존["순위"] = min(기존["순위"], 순위)
            if 표범위:
                기존["구간"].append(표범위)
            else:
                기존["전체"] = True
            if 위치 in 선택:
                기존['선택위치'].add(위치)
            if 행.get("경로") and 행["경로"] not in 기존["경로"]:
                기존["경로"].append(행["경로"])
    if 선택 - 처리:
        raise ValueError("선택 규범의 근거 위치를 찾지 못함")
    for (문서, 조번호, 키), 데이터 in 항목.items():
        if 데이터["전체"]:
            내용 = 데이터["원문"]
        else:
            구간들 = sorted(set(데이터["구간"]))
            if not 구간들:
                raise ValueError("선택 근거의 원문 구간이 없음")
            내용 = "\n\n".join(데이터['원문'][start:end] for start, end in 구간들)
        for 경로 in 데이터["경로"] or [None]:
            모음.넣기(데이터["순위"], "article", 데이터["id"], 문서, 조번호, 데이터["헤더"], 키, 내용, 경로,
                  선택위치=데이터['선택위치'])


def _l3_넣기(모음: _모음, l3: list[dict], 대상노드: set[str], 시작노드: str | None = None) -> None:
    for r in l3 or []:
        연결 = r.get("concept_ids")
        if 연결 is None:
            순위 = L3미연결
        elif (not 연결 or (set(연결) & 대상노드) or 모든비목 in 연결   # [] = 확인했고 해당 세목 없음(공통으로 항상 넣는다)
              or (시작노드 and 시작노드 != 모든비목 and 시작노드 in (r.get("연결상위") or ()))):   # 비목만 골랐고 그 아래 세목에 연결한 조
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


# L3 중복 제거(2026-11-12 사용자 결정). 모두의 창업 기관 2곳의 L3 는 사업 안내 책자 PDF 라 같은 사업 L1/L2 청크와 글자 기준 약 80% 가
# 중복이었고(운영 DB), 판정 프롬프트 70,467자 중 L3 블록이 31,214자였다. 같은 글이 두 문장 ID 로 들어가 제목 한 줄 조각이 인용됐다
_중복_n = 8
_중복_비율 = 0.9
_공백기호 = re.compile(r"[\s|\-·,.()\[\]*:※#>「」『』\"'`~]+")
# 숫자 · 한글 수사 · 부정 · 강도 말이 L1/L2 짝 조각에 없으면 L3 가 더 엄격하거나 다를 수 있어 남긴다
_다를수있는말 = re.compile(r"\d[\d,.]*|[일이삼사오육칠팔구십백천만억]+\s*(?:원|일|회|명|개월|년|건|시간)"
                       r"|않|없|아니|불가|금지|제외|반드시|사전|미리|이내|이상|이하|초과|미만")


def _정규화_글(글: str) -> str:
    return _공백기호.sub("", unicodedata.normalize("NFKC", 글 or ""))


def _n그램(글: str) -> set[str]:
    return {글[i:i + _중복_n] for i in range(len(글) - _중복_n + 1)}


def _다를수있는_말(글: str) -> set[str]:
    return {m.replace(",", "").replace(" ", "") for m in _다를수있는말.findall(unicodedata.normalize("NFKC", 글 or ""))}


def _L3중복_빼기(모음: _모음) -> None:
    """확인 전 L3(B2.5) 조각 가운데 B2 의 L1/L2 조각 하나와 같은 글(near-duplicate)을 뺀다. 같은 글이라 정보는 줄지 않는다.

    짝: B2 조각(순위가 L3미연결보다 앞이라 글자 상한으로 L3 보다 먼저 빠지지 않는다) 하나. 사용자가 연결을 확인한 L3(B1)는 빼지 않는다.
    중복: 정규화한 문자 8-gram 의 90% 이상이 그 짝 조각에 있고(8자보다 짧으면 짝 글에 그대로 있고),
    숫자 · 한글 수사 · 부정(않 · 없 · 불가 · 금지 …) · 강도(반드시 · 사전 · 이내 …) 말이 모두 짝 조각에도 있다."""
    짝들 = [(_정규화_글(x["조각"]), _다를수있는_말(x["조각"])) for x in 모음.표.values()
           if x["종류"] != "l3" and x["순위"] < L3미연결]
    if not 짝들:
        return
    짝들 = [(글, _n그램(글), 말) for 글, 말 in 짝들]
    for k, x in list(모음.표.items()):
        if x["종류"] != "l3" or x["순위"] != L3미연결:
            continue
        글 = _정규화_글(x["조각"])
        if not 글:
            continue
        그램, 말 = _n그램(글), _다를수있는_말(x["조각"])
        for 짝글, 짝그램, 짝말 in 짝들:
            if 말 - 짝말:
                continue
            if (len(그램 & 짝그램) / len(그램) >= _중복_비율) if 그램 else 글 in 짝글:
                del 모음.표[k]
                break


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


def _다른자금_빼기(모음: _모음, 자금: str | None, schema: dict | None) -> None:
    """온톨로지 · 검색 · 참조 확장 어느 쪽으로 들어왔든 다른 자금의 조 · 항을 뺀다(L3 제외). 금지 문장 항은 빼지 않고 B2.6 으로 옮긴다(ontology_fund)."""
    if not 자금:
        return
    문서키표 = {v: k for k, v in ((schema or {}).get("문서키") or {}).items()}
    for 표키, x in list(모음.표.items()):
        문서, 조번호, 키 = 표키
        문서키 = 문서키표.get(문서)
        if x["종류"] == "l3":
            continue
        if ontology_fund.금지문장_항인가(자금, 문서키, 조번호, 키):
            if not x.get("보조"):           # 검색을 넣은 뒤 한 번 더 도므로 이미 옮긴 항은 다시 자르지 않는다
                x.update(순위=공통미정, 조각=ontology_fund.금지문장_조각(문서키, 조번호, 키, x["조각"]), 보조=True)
        elif ontology_fund.다른_자금(자금, 문서키, 조번호, 키):
            del 모음.표[표키]


def _선택발췌_보존확인(모음: _모음, 근거발췌: dict[tuple, list[str]]) -> None:
    """자금 필터 뒤에도 선택된 각 근거 위치의 원문이 남아 있어야 한다."""
    for 문서, 조번호, 항호 in 근거발췌:
        위치 = (문서, 조번호, 항호 or None)
        if not any(v.get("조각") and 위치 in v.get('선택위치', set())
                   for (d, a, _), v in 모음.표.items() if d == 문서 and a == 조번호):
            raise ValueError("선택 규범의 원문 위치가 문맥에 보존되지 않음")


def 검색청크_거르기(cur, 청크들: list[int], 자금: str | None, schema: dict | None) -> list[int]:
    """검색 RRF 순위에서 `_다른자금_빼기` 가 조각을 하나도 남기지 않을 청크를 뺀다(순서 유지). 상위 10 을 자르기 전에 쓴다(retrieve.검색 `거르기`).

    청크를 `_검색_넣기` 와 같이 항으로 나눠, 글이 있는 조각이 모두 다른 자금이고 금지 문장 항이 아닐 때만 뺀다. 자금이 없으면 그대로 돌려준다.
    """
    if not 자금 or not 청크들:
        return 청크들
    문서키표 = {v: k for k, v in ((schema or {}).get("문서키") or {}).items()}
    행들 = {r[0]: r for r in cur.execute('SELECT chunk_id, doc_id, "조번호", "조제목", "항호", text FROM corpus.chunks WHERE chunk_id = ANY(%s)',
                                        (list(청크들),)).fetchall()}

    def 뺄까(cid: int) -> bool:
        r = 행들.get(cid)
        if r is None:
            return False
        _, 문서, 조번호, _, 항호, 글 = r
        문서키 = 문서키표.get(문서)
        키들 = [항 or 항호 for 항, 조각 in 항_분할(글 or "") if 조각]
        return bool(키들) and all(ontology_fund.다른_자금(자금, 문서키, 조번호, 키)
                                and not ontology_fund.금지문장_항인가(자금, 문서키, 조번호, 키) for 키 in 키들)

    return [c for c in 청크들 if not 뺄까(c)]


def 문맥_구성(cur, *, 근거행: list[dict], 대상노드: set[str] | list[str] | None = None, l3: list[dict] | None = None,
          검색: dict | None = None, schema: dict | None = None, 상한: int = 글자_상한, 자금: str | None = None,
          시작노드: str | None = None, 근거한정: bool = False, 근거발췌: dict[tuple, list[str]] | None = None) -> dict:
    """반환: {블록: [(제목, [(종류, id, 헤더, 조각, 키)])], 항목수, 글자수, 빠진수, L3_초과, 온톨로지항목수}.

    항목수 · 온톨로지항목수는 근거 항목만 센다. 대조 재료(금지 문장 항 · 품목 표)만 있으면 항목수 0 이라 후보 없음으로 끝난다.
    근거행은 ontology_lookup.근거_조문 결과다. `대상노드` 는 대상 · 상위 노드(L3 연결 판단). `l3` 항목의 `concept_ids` 는
    None = 미연결, [] = 확인했고 해당 세목 없음. `연결상위`(l3_load.조문_연결조회)에 `시작노드` 가 있으면 고른 비목 아래 세목에
    연결한 조라서 연결 조문으로 넣는다. `자금` 은 확정 비목의 자금(ontology_fund.경로_자금)이고 다른 자금 조문을 뺀다.
    """
    표시명 = 문서_표시명(schema)
    모음 = _모음()
    조문 = _조문_읽기(cur, sorted({r["article_id"] for r in 근거행 if r.get("article_id") is not None}))
    _l3_넣기(모음, l3 or [], set(대상노드 or ()), 시작노드)
    if 근거한정 and 근거발췌 is not None:
        _발췌근거_넣기(모음, 근거행, 조문, 표시명, 근거발췌)
    elif 근거한정:
        _온톨로지_넣기(모음, 근거행, 조문, 표시명, 근거한정=True)
    else:
        _온톨로지_넣기(모음, 근거행, 조문, 표시명)
    _다른자금_빼기(모음, 자금, schema)
    온톨로지항목수 = sum(1 for x in 모음.표.values() if x["순위"] in (규범근거, 근거조전체, 준용1, 공통미정, 준용2) and not x.get("보조"))
    if not 근거한정:
        _검색_넣기(cur, 모음, 검색, 표시명)
    _다른자금_빼기(모음, 자금, schema)
    _L3중복_빼기(모음)
    if 근거한정 and 근거발췌 is not None:
        _선택발췌_보존확인(모음, 근거발췌)
    항목들 = sorted(모음.표.values(), key=lambda x: x["순위"])            # 순위 안에서는 넣은 순서를 지킨다
    남김, 뺌 = _자르기(항목들, 상한)
    블록 = []
    for 순위들, 제목 in _블록제목:
        묶음 = [(x["종류"], x["id"], x["헤더"], x["조각"], x["키"]) for x in 남김 if x["순위"] in 순위들]
        if 묶음:
            블록.append((제목, 묶음))
    return {"블록": 블록, "항목수": sum(1 for x in 남김 if x["순위"] != 품목표 and not x.get("보조")), "글자수": sum(len(x["조각"]) + len(x["헤더"]) for x in 남김),
            "빠진수": len(뺌), "근거초과": 근거한정 and bool(뺌),
            "L3_초과": any((x["순위"] <= L3미연결 and 모음_L3(x)) or x["순위"] == 공통미정 for x in 뺌), "온톨로지항목수": 온톨로지항목수}


def 모음_L3(x: dict) -> bool:
    return x["종류"] == "l3"


_칸말 = {"수치": "숫자 기준", "대상": "대상", "조건": "조건", "결과": "결과", "예외": "예외"}
_원문확인 = "연결된 규정의 원문을 확인한다"


def _발췌(n: dict, 칸: str | None = None) -> str:
    """구조화한 규범 설명. 원문은 별도 조·항·표 문맥에서 제공한다.

    결과 문장이 없으면 근거 발췌(DB 원문 그대로, 가장 긴 것)를 쓴다(2026-11-13: 「10쪽의 대상 원문을 확인한다」 로는 판정 LLM 이 대조할 글이 없다).
    발췌도 없으면 근거 위치로 구분되는 문구(`제36조②의 숫자 기준 원문을 확인한다`)를 쓴다. 칸을 주면 그 칸의 근거만 본다.
    문장도 근거 위치도 없으면 빈 글이다. 거르는 쪽(경로 밖 · 상황 · 품목 표)은 이 값으로 실제 문구가 있는 규범만 본다(리뷰 28-16).
    """
    결과 = n.get("결과") or {}
    if isinstance(결과, dict):
        for 키 in ("문장", "조건문장"):
            if isinstance(결과.get(키), str) and 결과[키].strip():
                return 결과[키].strip()
    근거 = n.get("근거") or []
    if isinstance(근거, dict):                                  # 원천 JSON 모양도 받는다
        근거 = [{**g, "칸": k} for k, v in 근거.items() for g in v or []]
    근거 = [g for g in 근거 if isinstance(g, dict) and g.get("조번호")]
    고름 = [g for g in 근거 if g.get("칸") == 칸] if 칸 else 근거
    if not 고름:
        return ""
    발췌들 = [" ".join(str(g.get("발췌") or "").split()) for g in 고름]
    if any(발췌들):
        return max(발췌들, key=len)
    g = 고름[0]
    항호 = str(g.get("항호") or "")
    위치 = f'{조번호_표시(g["조번호"])}{"" if 항호.startswith("/") else 항호}'
    return f"{위치}의 {_칸말.get(칸 or g.get('칸'), '')} 원문을 확인한다".replace("의  원문", "의 원문")


# 분류 출처 문구(재설계 11-11: 사용자는 분류를 고르지 않고 ③ 분류가 하나를 정한다). 「유사」 는 판정 LLM 이 분류가 맞는지도 본다
_분류_유사 = ("분류 단계가 비슷한 품목으로 추정함. 이 지출이 이 분류의 집행 범위에 실제로 드는지도 원문으로 판단한다. "
          "들지 않거나 다른 비목에 더 가깝다면 가능으로 답하지 말고 요약에 그 이유를 쓴다")


def 규범_요약(규범결과: dict, 게이트결과: dict, *, 분류표시: str | None = None, 확정: bool = False, l3있음: bool = False,
          분류확신: str | None = None) -> str | None:
    """B4 문장. 코드가 비교를 끝낸 결과와 규범을 평문으로 적는다. 내부 ID 를 넣지 않는다.

    `분류확신` 은 품목 노드를 고른 확신(동일 | 유사, orchestrate 의 적용["품목노드"]). 유사면 분류가 맞는지도 판단하게 한다."""
    규범들 = 규범결과.get("규범") or []
    제외 = set(게이트결과.get("제외규범") or [])
    규범표 = {n.get("norm_id"): n for n in 규범들}     # 금지의 예외(단서) 규범 발췌를 같은 줄에 적는다
    쓴 = set(게이트결과.get("쓴규범") or [])
    줄: list[str] = []
    if 분류표시:
        출처 = ("추천값, 사용자 확정 전" if not 확정 else _분류_유사 if 분류확신 == "유사" else "분류 단계에서 정함")
        줄.append(f"분류: {분류표시} ({출처})")
    자연어답 = 게이트결과.get("자연어답") or {}         # 5-14 B 자연어 조건 확인 질문의 답(게이트가 다시 검사한 것)

    def 답꼬리(n: dict) -> str:
        """답한 자연어 조건을 적는 꼬리. 답이 없으면 빈 글(지금 문장 그대로)."""
        a = 자연어답.get(n.get("norm_id"))
        if not a:
            return ""
        말 = [*(f"「{x}」에 해당한다" for x in a["예"]), *(f"「{x}」에 해당하지 않는다" for x in a["아니오"])]
        return f" 확인 질문에 사용자가 답한 조건(진술, 증빙 아님): {' · '.join(말)}." if 말 else ""

    for n in 규범들:
        종류, 글 = n.get("종류"), _발췌(n, "수치") or _발췌(n) or _원문확인
        if 종류 == "우선순위" or not 글 or n.get("norm_id") in 제외:
            continue
        if not 확정_규범(n):
            # 2026-11-12(사용자 결정 A): 사업 공통 금지 · 미검증 기준 줄은 코드 상한을 걸지 않는다. 금지는 해당 여부를 판정 LLM 이 반드시 대조한다
            줄.append(f"- 확정되지 않은 {종류} 규정: {글} 코드가 비교하지 않았다. 원문을 읽고 판단한다."
                     + (" 지출이 이 금지에 해당하고 원문의 예외에도 들지 않으면 불가로 판정하고, 해당하지 않으면 판단에 쓰지 않는다." if 종류 == "금지" else ""))
        elif 종류 in ("한도", "임계값") and n.get("norm_id") in 쓴:
            # 한도는 게이트가 남긴 비교 결과대로만 쓴다. 넘었거나 부가세 기준으로 갈린 한도에 「넘지 않았다」 를 쓰지 않는다(리뷰 28-15)
            비교 = (게이트결과.get("한도비교") or {}).get(n.get("norm_id"))
            줄.append((f"- {종류}: {글} 금액 비교를 끝냈고 넘지 않았다." if 종류 == "한도" and 비교 == "안넘음"
                      else f"- {종류}: {글} 금액 비교를 끝냈고 넘었다. 넘어도 집행 자체를 막는 기준은 아니라 원문에 적힌 결과(초과분 처리 등)대로 판단한다." if 종류 == "한도" and 비교 == "넘음"
                      else f"- {종류}: {글} 부가세 포함 여부에 따라 넘을 수도 있다. 넘지 않았다고 보지 않는다." if 종류 == "한도" and 비교 == "갈림"
                      else f"- {종류}: {글} 코드가 비교 결과를 남기지 않았다. 원문을 읽고 판단한다." if 종류 == "한도"
                      else f"- {종류}: {글} 금액 비교를 끝냈다. 생긴 의무는 아래에 있다." if n.get("norm_id") in (게이트결과.get("발동임계값") or [])
                      else f"- {종류}: {글} 금액 비교를 끝냈다. 이 금액은 기준에 닿지 않아 이 기준으로 생기는 의무는 없다.") + 답꼬리(n))
        elif 종류 in ("한도", "임계값"):
            줄.append(f"- {종류}: {글} 입력이 모자라거나 예외가 있어 비교하지 못했다.{답꼬리(n)}")
        elif 종류 == "금지" and (자연어답.get(n.get("norm_id")) or {}).get("접힘"):
            조건들 = " · ".join(f"「{x}」" for x in 자연어답[n["norm_id"]]["아니오"])
            줄.append(f"- 금지: {글} 사용자가 이 금지의 조건 {조건들}에 해당하지 않는다고 답했다(해당 없음, 사용자 답). "
                     "진술이라 증빙으로 확인되기 전이다. 지출 내용이 이 조건에 해당한다는 사실이 따로 드러나면 그것으로 판단한다.")
        elif 종류 == "금지":
            진술 = (게이트결과.get("진술평가") or {}).get(n.get("norm_id"))
            # 답한 값을 줄에 적는다. 발췌가 다른 호 조각이어도 무엇에 답했는지 보이게 한다(리뷰 3c R1)
            답들 = [v for c in ((n.get("조건") or {}).get("구조화") or [] if isinstance(n.get("조건"), dict) else [])
                  if (v := (게이트결과.get("답변") or {}).get(c.get("항목")))]
            답말 = "(" + " · ".join(dict.fromkeys(str(v["항목"]) + ": " + str(v["답"]) for v in 답들)) + ")" if 답들 else ""
            예외글 = [g for x in n.get("예외") or [] if (g := _발췌(규범표.get(x) or {}))]
            남은것 = (f" 이 금지에는 예외가 있다: {' / '.join(f'{g}' for g in 예외글)}. 예외에 해당하는지까지 원문과 답변으로 판단한다." if 예외글
                    else " 이 금지에는 예외가 있다. 예외에 해당하는지까지 원문과 답변으로 판단한다." if n.get("예외")
                    else f" 답으로 확인하지 않은 조건 「{_자연어조건(n)}」이 더 있다. 그 조건까지 원문과 답변으로 판단한다." if _자연어조건(n) else "")
            줄.append((f"- 금지: {글} 사용자가 이 조건{답말}에 해당한다고 답했다. 진술이다.{남은것}" if 진술 is True and 남은것
                      else f"- 금지: {글} 사용자가 이 조건{답말}에 해당한다고 답했다. 진술이지만 해당하면 집행할 수 없다고 판단한다." if 진술 is True
                      else f"- 금지: {글} 사용자가 이 조건{답말}에 해당하지 않는다고 답했다. 진술이라 증빙으로 확인되기 전이다." if 진술 is False
                      else f"- 금지: {글} 조건에 해당하는지 원문과 사용자 답변으로 판단한다. 해당하고 원문의 예외에도 들지 않으면 불가로 판정하고, 해당하지 않으면 판단에 쓰지 않는다.") + 답꼬리(n)
                     # 자연어 조건에 「예」 라고 답했는데 예외가 있는 금지: 코드 불가가 아니다. 예외 해당 여부는 판정 LLM 이 가른다(5-14 B 조건 충족 진술)
                     + (남은것 if 진술 is not True and n.get("예외") and (자연어답.get(n.get("norm_id")) or {}).get("예") else ""))
        elif 종류 == "요건":
            줄.append(f"- 요건: {글} 충족 여부를 판단한다.")
        elif 종류 == "허용":
            줄.append(f"- 허용: {글}")
    품목들 = 규범결과.get("경로밖_품목")
    밖 = [(n, 경로밖금지_구분(n, 품목들) if n.get("종류") == "금지" else None) for n in 규범결과.get("경로밖_품목규범") or [] if _발췌(n)]
    if 밖:
        줄.append("- 입력한 품목에는 고른 분류 밖의 품목 규정이 따로 있다. 코드는 이 규정으로 판정하지 않았다. 지출이 이 품목에 해당하는지 원문으로 판단한다:")
        for n, 구분 in 밖:
            자연어 = _자연어조건(n)
            꼬리 = (f"{f' 조건: 「{자연어}」.' if 자연어 else ''} 지출이 이 조건에 해당할 때만 집행할 수 없다고 판단한다. 해당한다는 것이 확인되지 않으면 이 문장만으로 불가라고 쓰지 않는다." if 구분 == "행위"
                  else " 해당하면 집행할 수 없다고 판단한다. 원문의 단서를 충족한다는 것이 답변이나 원문으로 확인되지 않으면 구입 · 집행할 수 있다고 쓰지 않는다."
                  if 구분 else "")
            글 = _발췌(n)
            줄.append(f"  - {n.get('종류')}: {글}{꼬리}")
    상황 = [n for n in 규범결과.get("상황규범") or [] if _발췌(n)]
    if 상황:
        줄.append("- 질문에 드러난 상황(협약 변경 · 거래 상대 등)에 걸린 규정이 따로 있다. 코드는 이 규정으로 판정하지 않았다. 질문 글에 이 상황이 적혀 있으면 "
                 "이 규정으로 판단하고(금지에 해당하면 불가), 적혀 있지 않으면 판단에 쓰지 않는다:")
        for n in 상황:
            자연어 = _자연어조건(n)
            글 = _발췌(n)
            줄.append(f"  - {n.get('종류')}: {글}{f' 조건: 「{자연어}」.' if 자연어 else ''}")
    표행 = list(dict.fromkeys(_발췌(n) for n in 규범결과.get("품목표") or [] if _발췌(n)))
    if 표행:
        줄.append("- 입력한 품목 표현이 규정의 품목 이름과 글자가 같지 않아 코드가 품목 규정을 찾지 못했다. 아래 금지 품목에 같은 물건이 있는지 대조한다. "
                 "같은 물건이면 그 금지로 판단하고, 같은 물건인지 확실하지 않으면 집행할 수 있다고 쓰지 않는다. 이 목록의 금지를 다른 물건까지 넓혀 쓰지 않는다:")
        줄.extend(f"  - {글}" for 글 in 표행)
    # 자연어 조건 답(키 「자연어:」)은 위 금지 줄이 「해당 없음(사용자 답)」 으로 이미 적는다(5-14 B)
    if 선택답 := [v for k, v in (게이트결과.get("답변") or {}).items() if not str(k).startswith("자연어:")]:
        줄.append("- 사용자가 선택 칸에 답한 것(진술, 증빙 아님): " + " · ".join(f"{v['항목']}: {v['답']}" for v in 선택답))
    # 조건 줄(2026-11-13 사용자): 판정 LLM 이 번호마다 조건판단(절차 · 사실 · 해당없음 / 충족 · 불충족 · 모름)을 낸다. 원문 문장과 시점을 같이 준다
    판단줄 = 판단_조건(게이트결과)
    if 판단줄:
        줄.append("- 조건 줄: 이 지출에 걸린 조건이다. 번호(C1…)마다 조건판단에 하나씩 답한다. 번호는 조건판단에만 쓰고 요약에는 쓰지 않는다.")
        for c in 판단줄:
            원문 = " ".join(str(c.get("근거발췌") or "").split())
            원문 = 원문 if len(원문) <= 300 else f"{원문[:299]}…"
            줄.append(f"  - {c['번호']} [{_조건줄_말(c)} · 시점: {c.get('시점') or '정해지지 않음'}] {c['문장']}"
                     + (f" (원문: 「{원문}」)" if 원문 and 발췌_정규화(원문) not in 발췌_정규화(c["문장"]) else ""))
    판단키 = {c["조건키"] for c in 판단줄}
    for c in 게이트결과.get("조건") or []:
        문장, 구분 = c["문장"], c.get("구분")
        if c.get("조건키") in 판단키:
            continue
        if c.get("코드상한") and c.get("상한") and c.get("금지") and not any(k in ("한도", "임계값") for k in c.get("규범종류") or []):
            # 검수 전 금지 줄(ontology_gate 판단_조건 밖): 조건판단을 받지 않고 코드가 상한(확인 필요)을 건다
            줄.append(f"- 아직 검수되지 않은 금지 규정(코드가 확인 필요로 둔다): {문장}")
        elif c.get("코드상한") and c.get("상한"):
            # 한도 · 임계값 줄(ontology_gate 판단_조건 밖): 조건판단을 받지 않고 코드가 상한(확인 필요)을 건다
            줄.append(f"- 코드가 금액을 비교한 한도 · 기준 금액(넘을 수 있거나 검수 전이라 코드가 확인 필요로 둔다): {문장}")
        elif 구분 == "의무":
            줄.append(f"- 해당할 때만 지키는 의무: {문장}" if c.get("해당시") else f"- 지켜야 할 의무: {문장}")
        elif 구분 == "다른수범자":
            줄.append(f"- 사용자가 아닌 다른 주체의 의무: {문장}")
        elif 구분 in ("입력비움", "위임", "두기준", "미확정", "세목미정", "미확정한도"):
            줄.append(f"- 확인 필요: {문장}")
        elif 구분 == "기관기준":
            줄.append(f"- 기관 규정의 숫자 기준(코드가 계산, 이 숫자만으로 불가로 보지 않았다): {문장}")
        elif 구분 == "누적출처":
            줄.append(f"- 코드가 기록으로 계산한 누적 금액(사용자 기록, 증빙 아님): {문장}")
    if 규범결과.get("공통미정"):
        줄.append("- B2.6 사업비 공통 규정은 이 자금에 적용되는지 정해지지 않았다. 코드가 이 규정으로 불가나 금액 판정을 내리지 않았다. 이 자금의 기준으로 판단하고, 공통 규정만을 근거로 절차나 서류를 요구하지 않는다. "
                 "공통 규정의 금지 조항은 안내 책자가 같은 지출을 직접 정하지 않았고 지출이 그 금지에 해당하며 원문의 예외에도 들지 않으면 불가로 판정하고, 해당하지 않으면 판단에 쓰지 않는다.")
    if 규범결과.get("금지미정"):
        줄.append("- B2.6 의 「집행할 수 없다」 · 「구매할 수 없다」 · 「불가」 금지 문장(차량 임차 · 양산 금형 · 재하청 · 위약금 · 전대차 등)도 이 자금에 적용되는지 정해지지 않았다. "
                 "안내 책자가 같은 지출을 직접 정하면 책자를 따른다. 책자에 없고 지출이 그 금지 문장에 해당하면 집행할 수 있다고 쓰지 않고 불가로 판정한다. 해당하지 않는 지출에는 근거로 쓰지 않는다.")
    # 주관기관 규정(L3)과의 적용 순서는 L3 를 하위로 적은 우선순위 규범(제4조②)만 말한다. 상충시 규범이 L1 · L2 사이에도 있다
    l3순서 = next((n for n in 규범들 if n.get("종류") == "우선순위" and (n.get("조건") or {}).get("범위") == "상충시"
                  and (n.get("조건") or {}).get("하위") == "L3"), None)
    if l3있음 and l3순서:
        줄.append(f"- 적용 순서: {_발췌(l3순서) or _원문확인} 귀 기관 규정이 상위 규정보다 느슨하면 상위 규정으로 판정하고, 요약에 "
                 "「주관기관 안내는 허용하지만 관리기준이 우선」이라고 적는다. 더 엄격하면 귀 기관 규정을 따른다.")
    if 줄:
        줄.append("- 요약에 금액을 쓸 때는 입력 금액과 이 판정에 인용한 규정 원문의 금액만 쓴다. 다른 비목 기준이나 예시의 금액을 옮겨 쓰지 않는다.")
    return "\n".join(줄) if 줄 else None


def _조건줄_말(c: dict) -> str:
    """조건 줄 구분을 판정 LLM 이 읽는 말로. 내부 구분 이름을 그대로 쓰지 않는다."""
    구분 = c.get("구분")
    if 구분 == "금지조건" or (구분 == "미확정규범" and c.get("금지")):
        return "금지 · 사용자가 해당한다고 답함(진술)" if c.get("진술") == "해당" else "금지"
    if 구분 == "답변확인":
        return "금지 · 사용자가 해당하지 않는다고 답함(진술)"
    if 구분 == "다른수범자":
        return "사용자가 아닌 다른 주체의 의무"
    return "해당할 때만 지키는 의무" if c.get("해당시") else "의무"


def _핵심_조문(규범들: list[dict] | None) -> set[tuple]:
    """한도 · 금지를 낸 규범의 근거 조문 (doc_id, 조번호, 항 키). 자금 전체에 걸린 규범(`…/*`)은 이 지출의 핵심이 아니라 뺀다."""
    return {(g.get("doc_id"), g.get("조번호"), 항호_키(g.get("항호")))
            for n in 규범들 or [] if n.get("종류") in ("한도", "금지") and not any(str(v).split("@")[0].endswith("/*") for v in n.get("대상") or [])
            for g in n.get("근거") or []}


def 근거경로들(근거행: list[dict], schema: dict, 상한: int | None = 3, 규범들: list[dict] | None = None) -> list[str]:
    """화면 "근거 경로" 문장. 한도 · 금지를 낸 규범의 근거 조문을 먼저, 확정 노드에서 시작한 경로(가까운 노드, 긴 경로 순)를 다음에, 공통 경로를 뒤에 쓴다.

    미특정 노드(`미특정:…`)로 끝나는 경로는 같은 묶음의 맨 뒤로 보낸다. 다른 문장에 포함된 문장은 뺀다. 화면에는 상한까지만 낸다.
    """
    from ontology_lookup import 근거경로_문장

    핵심 = _핵심_조문(규범들)
    우선: list[tuple[tuple, str]] = []
    공통: list[tuple[bool, str]] = []

    def 미특정끝(경로: list[str]) -> bool:
        return bool(경로) and str(경로[-1]).startswith("미특정:")

    for 행 in 근거행:
        if 행.get("공통미정") or 행.get("품목표") or 행.get("우선순위근거"):
            continue                                    # 적용 미정 규범 · 대조 재료 · 우선순위 규범 근거(문맥 후보만)의 경로는 화면에 내지 않는다
        핵심행 = (행.get("doc_id"), 행.get("조번호"), 항호_키(행.get("항호"))) in 핵심
        시작 = 행.get("우선경로") or (행.get("경로") if 핵심행 else None)
        if 시작:
            문장 = 근거경로_문장(시작, schema)
            if 문장:
                우선.append(((not 핵심행, 미특정끝(시작), 행.get("경로순위") or 0), 문장))
        문장 = 근거경로_문장(행.get("경로") or [], schema)
        if 문장 and 문장 not in (s for _, s in 공통):
            공통.append((미특정끝(행.get("경로") or []), 문장))
    남김: list[str] = []

    def 포함(짧은: str, 긴: str) -> bool:
        return 짧은 == 긴 or f"{짧은} →" in f"{긴} →"

    for k, s in sorted(dict.fromkeys(우선), key=lambda x: (x[0], -x[1].count("→"))):   # 같은 순위는 행 순서(조문 ID 순)를 지킨다
        if any(포함(s, t) for t in 남김):
            continue
        자리 = next((i for i, t in enumerate(남김) if 포함(t, s)), None)   # 먼저 넣은 짧은 경로를 품는 긴 경로면 그 자리에 넣는다
        if 자리 is None:
            남김.append(s)
        elif not k[1]:                                  # 미특정으로 끝나는 경로로는 바꾸지 않는다
            남김[자리] = s
    for _, s in sorted(공통, key=lambda x: (x[0], -x[1].count("→"))):
        if not any(포함(s, t) for t in 남김):
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
    세목l3 = [{**l3[0], "연결상위": ["비목:창업/지급수수료"]}, {**l3[2], "연결상위": ["비목:창업/재료비"]}]
    비목만 = 문맥_구성(cur, 근거행=[], 대상노드={"비목:창업/지급수수료", 모든비목}, l3=세목l3, schema=표, 시작노드="비목:창업/지급수수료")
    t.append(("P4-5: 비목만 고른 판정은 그 아래 세목에 연결한 조를 B1 에 넣고 다른 비목 조는 뺀다",
              [i[3] for _, 항목 in 비목만["블록"] for i in 항목] == ["연결 조문"]))
    형제 = 문맥_구성(cur, 근거행=[], 대상노드={"세목:창업/지급수수료/보험료", "비목:창업/지급수수료", 모든비목}, l3=세목l3, schema=표,
               시작노드="세목:창업/지급수수료/보험료")
    t.append(("P4-5: 다른 세목(형제)에 연결한 조는 넣지 않는다", 형제["블록"] == []))
    t.append(("규범 근거 항 먼저, 조 전체도 들어온다", 글들.index("① 멘토링비는 1인 1일 30만원 한도") < 글들.index("② 수수료는 실비")))
    t.append(("별지는 해당 행만", "소속 인력 불가" in 글들 and not any("재료 내용" in x for x in 글들)))
    t.append(("준용_앞은 해당 항만", "② 지침 제42조를 준용한다" in 글들 and "① 비목은 표와 같다" not in 글들))
    t.append(("같은 조 · 항은 한 번만(검색 청크 제42조① 빠짐)", 글들.count("① 지침 본문") == 1 and [i[0] for i in 전체 if i[3] == "① 지침 본문"] == ["article"]))
    t.append(("온톨로지에 없는 검색 청크는 B3", m["블록"][-1][1][0][3] == "검색 조문"))
    t.append(("미특정 행은 문맥에 안 들어가고 예외도 없다", m["항목수"] == len(전체)))
    표겹침 = "① 본문 하나\n표: ① 칸 ② 칸 ③ 칸\n② 본문 둘\n③ 본문 셋"
    t.append(("표 안 기호와 겹치는 항은 본문까지 고른다", any("본문 셋" in c for _, c in _고른_조각(표겹침, "③"))))
    t.append(("헤더는 문서 표시명", any(i[2] == "모두의 창업 제36조(지급수수료)" for i in 전체)))
    미정만 = 문맥_구성(cur, 근거행=[{**행들[0], "공통미정": True}], schema=표)
    t.append(("공통 미정 근거 항도 온톨로지 항목으로 센다", 미정만["온톨로지항목수"] > 0 and 미정만["항목수"] > 0))
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
    핵심규범 = [{"종류": "한도", "대상": ["세목:창업/멘토링비"], "근거": [{"doc_id": "관리기준", "조번호": "제36조", "항호": "①"}]},
              {"종류": "한도", "대상": ["비목:창업/*"], "근거": [{"doc_id": "지침", "조번호": "제42조", "항호": None}]}]
    핵심행들 = [{"doc_id": "지침", "조번호": "제42조", "항호": None, "경로": ["조:모두의창업#제33조②", "조:지침#제42조"], "우선경로": ["조:모두의창업#제33조②", "조:지침#제42조"], "경로순위": 0},
              {"doc_id": "관리기준", "조번호": "제36조", "항호": "①", "경로": ["조:모두의창업#제36조①"]},
              {"경로": ["조:모두의창업#제36조②", "미특정:소득세법 시행령"], "우선경로": ["조:모두의창업#제36조②", "미특정:소득세법 시행령"], "경로순위": 0}]
    경로결과 = 근거경로들(핵심행들, 표, 규범들=핵심규범)
    t.append(("근거 경로: 한도 근거 조문이 먼저, 미특정 끝 경로는 뒤, 자금 전체 규범은 핵심이 아니다", 경로결과[0] == "모두의 창업 제36조①" and 경로결과[1].endswith("지침 제42조") and 경로결과[-1].endswith("소득세법 시행령")))
    t.append(("근거 경로 · 헤더: 부칙 제1조로 읽힌다",
              근거경로들([{"경로": ["조:지침#부칙 제2조", "미특정:사업별 세부관리기준"]}], 표) == ["지침 부칙 제2조 → 사업별 세부관리기준"]
              and _헤더("지침", "부칙 제1조", None) == "지침 부칙 제1조"))
    공통행 = {"경로": ["조:모두의창업#제36조①", "조:지침#제42조", "조:모두의창업#제33조②"]}
    t.append(("근거 경로: 확정 노드 경로가 화살표 많은 공통 경로보다 먼저",
              근거경로들([공통행, {"경로": ["조:모두의창업#제36조②"], "우선경로": ["조:모두의창업#제36조②"], "경로순위": 0}], 표)
              == ["모두의 창업 제36조②", "모두의 창업 제36조① → 지침 제42조 → 모두의 창업 제33조②"]))
    t.append(("근거 경로: 가까운 노드부터, 상한은 공통을 먼저 민다",
              근거경로들([공통행, {"경로": ["조:지침#제42조"], "우선경로": ["조:지침#제42조"], "경로순위": 1},
                     {"경로": ["조:모두의창업#제36조②"], "우선경로": ["조:모두의창업#제36조②"], "경로순위": 0}], 표, 상한=2)
              == ["모두의 창업 제36조②", "지침 제42조"]))
    t.append(("근거 경로: 먼저 넣은 짧은 경로를 품는 긴 경로로 바꾼다",
              근거경로들([{"경로": ["조:지침#제42조"], "우선경로": ["조:지침#제42조"], "경로순위": 0},
                     {"경로": ["조:지침#제42조", "조:지침#제50조"], "우선경로": ["조:지침#제42조", "조:지침#제50조"], "경로순위": 1}], 표)
              == ["지침 제42조 → 지침 제50조"]))
    요약 = 규범_요약({"규범": [
        {"norm_id": "a", "종류": "한도", "verified": True, "근거": [{"칸": "수치", "발췌": "1인 1일 30만원 한도"}]},
        {"norm_id": "b0", "종류": "우선순위", "verified": True, "조건": {"범위": "상충시", "상위": "L2", "하위": "L1"}, "근거": [{"칸": "대상", "발췌": "지침과 달리 정하는 사항"}]},
        {"norm_id": "b", "종류": "우선순위", "verified": True, "조건": {"범위": "상충시", "상위": "L2", "하위": "L3"}, "결과": {"문장": "본 관리기준을 우선"}, "근거": [{"칸": "대상", "발췌": "저장 발췌"}]},
        {"norm_id": "c", "종류": "금지", "verified": False, "근거": [{"칸": "대상", "발췌": "미확정 금지"}]}]},
        {"쓴규범": ["a"], "한도비교": {"a": "안넘음"}, "조건": [{"문장": "공제 후 집행", "구분": "의무"}, {"문장": "주관기관이 확인", "구분": "다른수범자"}]},
        분류표시="지급수수료 > 멘토링비", 확정=True, l3있음=True)
    t.append(("B4: 비교 끝남 · 의무 · 우선순위 · 미확정 표시, 내부 ID 없음",
              "넘지 않았다" in 요약 and "공제 후 집행" in 요약 and "관리기준이 우선" in 요약 and "확정되지 않은" in 요약 and "규범:" not in 요약))
    한도만 = {"규범": [{"norm_id": "a", "종류": "한도", "verified": True, "결과": {"문장": "1인 1일 30만원 한도"}}]}
    B4 = lambda 비교: 규범_요약(한도만, {"쓴규범": ["a"], **({"한도비교": {"a": 비교}} if 비교 else {})}) or ""
    t.append(("28-15: B4 는 넘었거나 갈렸거나 기록이 없는 한도에 「넘지 않았다」 를 쓰지 않는다",
              "넘지 않았다." in B4("안넘음") and "넘었다" in B4("넘음") and "넘을 수도" in B4("갈림")
              and all("금액 비교를 끝냈고 넘지 않았다" not in B4(x) for x in ("넘음", "갈림", None))))
    위치한도 = lambda id_, 조, 항: {"norm_id": id_, "종류": "한도", "verified": True, "근거": [{"칸": "대상", "조번호": "제1조"}, {"칸": "수치", "조번호": 조, "항호": 항}]}
    t.append(("결과 문장이 없으면 근거 발췌(가장 긴 것)를 쓰고 위치 문구는 쓰지 않는다",
              _발췌({"norm_id": "y", "근거": [{"칸": "대상", "조번호": "p10-1", "발췌": "잔액은 현금으로"}, {"칸": "대상", "조번호": "p10-1", "발췌": "잔액은 현금으로 돌려받을 수 없습니다."}]})
              == "잔액은 현금으로 돌려받을 수 없습니다."))
    요약3 = 규범_요약({"규범": [위치한도("p", "제36조", "②"), 위치한도("q", "참고2-p29-1", None)], "품목표": [{"norm_id": "r", "종류": "금지", "근거": [{"칸": "대상", "조번호": 조}]} for 조 in ("제5조", "제6조")] + [{"norm_id": "t", "종류": "금지"}]},
                   {"쓴규범": ["p", "q"], "한도비교": {"p": "안넘음", "q": "안넘음"}}) or ""
    t.append(("28-16: 결과 문장이 없는 규범은 근거 위치(칸)로 구분해 쓴다 · 품목 표 줄이 하나로 뭉치지 않는다 · 근거도 없으면 거른다",
              "제36조②의 숫자 기준 원문" in 요약3 and "참고2 29쪽의 숫자 기준 원문" in 요약3
              and "  - 제5조의 대상 원문을 확인한다" in 요약3 and "  - 제6조의 대상 원문을 확인한다" in 요약3 and _원문확인 not in 요약3
              and _발췌({"norm_id": "z", "종류": "금지"}) == "" and _발췌({"norm_id": "z", "근거": [{"칸": "대상", "조번호": "제3조"}]}, "수치") == ""))
    t.append(("B4: 적용 순서는 L3 를 하위로 적은 규범의 설명을 쓴다", "적용 순서: 본 관리기준을 우선" in 요약 and "저장 발췌" not in 요약 and "지침과 달리" not in 요약))
    t.append(("B4: 다른 수범자의 의무도 판정 문맥에 남는다", "다른 주체의 의무: 주관기관이 확인" in 요약))
    조건g = {"조건": [{"문장": "결제 전에 승인을 받으세요", "구분": "의무", "시점": "집행 전", "조건키": "k1", "상한": True, "금지": False,
                       "근거발췌": "50만원이 넘으면 사전에 승인을 받아야 한다"},
                      {"문장": "증빙을 내세요", "구분": "의무", "시점": "집행 후", "조건키": "k2", "상한": False, "공통": True, "금지": False}]}
    요약C = 규범_요약({"규범": []}, 조건g) or ""
    t.append(("B4: 조건 줄에 번호 · 시점 · 원문 문장, 상한을 걸지 않는 공통 줄은 번호 없이 지금처럼",
              "C1 [의무 · 시점: 집행 전] 결제 전에 승인을 받으세요 (원문: 「50만원이 넘으면 사전에 승인을 받아야 한다」)" in 요약C
              and "C2" not in 요약C and "지켜야 할 의무: 증빙을 내세요" in 요약C))
    한도g = {"조건": [{"문장": "1건 30만원을 넘을 수 없습니다.", "구분": "미확정규범", "시점": None, "조건키": "k3", "상한": True, "금지": False,
                       "코드상한": True, "근거발췌": "1건 30만원 이하"}]}
    요약H = 규범_요약({"규범": []}, 한도g) or ""
    t.append(("B4: 한도 · 임계값 줄(코드상한)은 조건판단 번호 없이 코드가 확인 필요로 둔다고 적는다(R_v043 H1)",
              "C1" not in 요약H and "코드가 확인 필요로 둔다): 1건 30만원을 넘을 수 없습니다." in 요약H))
    금지g = {"조건": [{"문장": "상품권은 살 수 없어요", "구분": "미확정규범", "시점": None, "조건키": "k4", "상한": True, "금지": True,
                       "코드상한": True, "규범종류": ["금지"], "근거발췌": "상품권 구입 불가"}]}
    요약F = 규범_요약({"규범": []}, 금지g) or ""
    t.append(("B4: 검수 전 금지 줄(코드상한)은 조건판단 번호 없이 검수 전 금지로 적는다(R_v043_fix N1)",
              "C1" not in 요약F and "아직 검수되지 않은 금지 규정(코드가 확인 필요로 둔다): 상품권은 살 수 없어요" in 요약F
              and "금액을 비교한" not in 요약F))
    요약2 = 규범_요약({"규범": [], "경로밖_품목규범": [{"norm_id": "x", "종류": "금지", "결과": {"문장": "키보드 불가"}, "근거": [{"칸": "대상", "발췌": "원문 발췌"}]}]},
                  {"조건": []}, 분류표시="창업활동비 > 재료구입비", 확정=True)
    t.append(("B4: 경로 밖 품목 금지 설명 · 단서 지시, 내부 ID 없음", "키보드 불가" in 요약2 and "원문 발췌" not in 요약2 and "단서를 충족" in 요약2 and "규범:" not in 요약2))
    요약3 = 규범_요약({"규범": [{"norm_id": "b0", "종류": "우선순위", "조건": {"범위": "상충시", "하위": "L1"}, "근거": [{"칸": "대상", "발췌": "지침과 달리"}]}]}, {"조건": []}, l3있음=True)
    t.append(("B4: L3 를 하위로 적은 규범이 없으면 적용 순서 줄을 쓰지 않는다", 요약3 is None))
    # 2-3: 다른 자금 조문은 어느 경로로 들어와도 빠지고, 공통 미정 조는 뒤쪽 블록이다
    표2 = {"문서키": {"모두의창업": "관리", "지침": "지침", "안내책자": "책자"}, "문서 표시명": {"모두의창업": "모두의 창업", "지침": "지침", "안내책자": "안내 책자"}}
    책자 = (21, "책자", "p11-1", "사용 가능 비목", "책자 본문")
    제37조 = (22, "관리", "제37조", "여비", "① 관리 여비")
    제47조 = (23, "지침", "제47조", "사업비 집행", "① 운영요령대로 ② 온라인시스템 ③ 사전검토 ④ 목적 외 금지 ⑥ 사업비카드")
    cur2 = _가짜커서([책자, 제37조, 제47조], [(200, "관리", "제37조", "여비", "①", "① 관리 여비"), (201, "책자", "p11-1", "사용 가능 비목", None, "책자 본문")])
    책자행 = {"doc_id": "책자", "조번호": "p11-1", "항호": None, "article_id": 21, "깊이": 0, "출처": "규범근거", "경로": ["조:안내책자#p11-1"]}
    미정행 = {"doc_id": "지침", "조번호": "제47조", "항호": "①", "article_id": 23, "깊이": 0, "출처": "규범근거", "경로": ["조:지침#제47조①"], "공통미정": True}
    창 = 문맥_구성(cur2, 근거행=[책자행, 미정행], 대상노드=set(), l3=[{"article_id": 30, "조번호": "제37조", "조제목": "여비", "본문": "기관 여비", "concept_ids": None}],
               검색={"top5": [200], "참조확장": [22]}, schema=표2, 자금="창업활동자금")
    창글 = [i[3] for _, 항목 in 창["블록"] for i in 항목]
    t.append(("창업활동자금: 검색 · 참조 확장의 시제품제작비 조문이 빠진다", "① 관리 여비" not in 창글 and "책자 본문" in 창글))
    t.append(("창업활동자금: 제47조 ②③⑥ 만 빠지고 ①④ 는 공통 블록에 남는다", "② 온라인시스템" not in 창글 and "③ 사전검토" not in 창글 and "⑥ 사업비카드" not in 창글
              and "④ 목적 외 금지" in 창글 and any("B2.6" in b[0] and any("④ 목적 외" in i[3] for i in b[1]) for b in 창["블록"])))
    t.append(("L3 는 조번호가 같아도 거르지 않는다", "기관 여비" in 창글))
    시 = 문맥_구성(cur2, 근거행=[{**책자행, "article_id": 21}], 대상노드=set(), 검색={"top5": [200, 201]}, schema=표2, 자금="시제품제작비")
    시글 = [i[3] for _, 항목 in 시["블록"] for i in 항목]
    t.append(("시제품제작비: 책자가 빠지고 관리기준 조문은 남는다", "책자 본문" not in 시글 and "① 관리 여비" in 시글))
    무 = 문맥_구성(cur2, 근거행=[책자행], 대상노드=set(), 검색={"top5": [200]}, schema=표2)
    t.append(("자금을 주지 않으면 거르지 않는다", {"책자 본문", "① 관리 여비"} <= {i[3] for _, 항목 in 무["블록"] for i in 항목}))
    # P5-1 보강 2: 상위 10 을 자르기 전에 다른 자금 청크를 뺀다. 자금 없음은 같은 목록 그대로
    순위 = [200, 201, 999]
    t.append(("검색청크_거르기: 자금 없으면 같은 목록 그대로", 검색청크_거르기(cur2, 순위, None, 표2) is 순위))
    t.append(("검색청크_거르기: 시제품제작비는 책자 청크를 빼고 순서를 지킨다(모르는 청크는 남김)", 검색청크_거르기(cur2, 순위, "시제품제작비", 표2) == [200, 999]))
    t.append(("검색청크_거르기: 창업활동자금은 관리기준 여비 청크를 뺀다", 검색청크_거르기(cur2, 순위, "창업활동자금", 표2) == [201, 999]))
    import retrieve as _r
    원래 = (_r.임베딩, _r.dense, _r.sparse, _r.참조확장_수집, _r.DANGLING_SQL)
    받은: list = []
    try:
        _r.임베딩 = lambda q: "v"
        _r.dense = lambda *a, **k: [(c, 0.9) for c in range(1, 31)]
        _r.sparse = lambda *a, **k: []
        _r.참조확장_수집 = lambda cur, top: (받은.append(list(top)) or ([], [], []))
        짝수만 = lambda ids: [c for c in ids if c % 2 == 0]
        거름 = _r.검색(None, "q", None, top_k=10, 거르기=짝수만)["top5"]
        그대로 = _r.검색(None, "q", None, top_k=10)["top5"]
    finally:
        _r.임베딩, _r.dense, _r.sparse, _r.참조확장_수집, _r.DANGLING_SQL = 원래
    t.append(("retrieve.검색 거르기: 거른 뒤 상위 10 · 참조 확장도 그 10 으로", 거름 == list(range(2, 21, 2)) and 받은[0] == 거름))
    t.append(("retrieve.검색 거르기 없음: 전과 같은 상위 10", 그대로 == list(range(1, 11)) and 받은[1] == 그대로))
    # 리뷰 P5-1 보강 지적 3: 일부 항만 다른 자금인 청크는 남긴다(문맥에서 그 항만 빠진다, E88). 모든 항이 다른 자금이면 뺀다
    섞임 = _가짜커서([], [(301, "지침", "제47조", "사업비 집행", None, "① 공통 집행 문장이다.\n② 온라인시스템 집행 문장이다."),
                       (302, "지침", "제47조", "사업비 집행", None, "② 온라인시스템 집행 문장이다.\n③ 사업비카드 문장이다.")])
    t.append(("검색청크_거르기: 창업활동자금은 일부 항만 다른 자금인 청크를 남기고 모든 항이 다른 자금인 청크만 뺀다",
              검색청크_거르기(섞임, [301, 302], "창업활동자금", 표2) == [301]))
    t.append(("검색청크_거르기: 창업활동자금 금지 문장 항 청크는 남긴다",검색청크_거르기(_가짜커서([], [(300, "지침", "제42조", "지급수수료", None, "⑦ 기자재임차비 안내 1. 차량 등의 임차 경비는 집행할 수 없다.")]), [300], "창업활동자금", 표2) == [300]))
    제42조 = (24, "지침", "제42조", "지급수수료", "① 멘토링비 안내 ② 기술이전비 안내 ⑦ 기자재임차비 안내 1. 차량 등의 임차 경비는 집행할 수 없다. 2. 기자재 임차료는 협약기간에 한하여 집행할 수 있다. ⑨ 다른 항")
    cur3 = _가짜커서([책자, 제42조], [(300, "지침", "제42조", "지급수수료", None, "⑦ 기자재임차비 안내 1. 차량 등의 임차 경비는 집행할 수 없다.")])
    금지행 = {"doc_id": "지침", "조번호": "제42조", "항호": "⑦", "article_id": 24, "깊이": 0, "출처": "금지미정", "경로": ["조:지침#제42조⑦"], "공통미정": True}
    금 = 문맥_구성(cur3, 근거행=[책자행, 금지행], 대상노드=set(), schema=표2, 자금="창업활동자금")
    금글 = {제목[:5]: [i[3] for i in 항목] for 제목, 항목 in 금["블록"]}
    t.append(("금지 문장 항: 창업활동자금 B2.6 에 금지 부분만, 같은 조 다른 항은 없다", 금글.get("B2.6.") == ["차량 등의 임차 경비는 집행할 수 없다."] and "멘토링비 안내" not in str(금글)))
    t.append(("금지 문장 항: 항목수에 세지 않는다(후보 없음 판단)", 금["항목수"] == 1 and 문맥_구성(cur3, 근거행=[금지행], 대상노드=set(), schema=표2, 자금="창업활동자금")["항목수"] == 0))
    시금 = 문맥_구성(cur3, 근거행=[{**금지행, "공통미정": False, "출처": "규범근거"}], 대상노드=set(), schema=표2, 자금="시제품제작비")
    t.append(("금지 문장 항: 시제품 경로는 원문 항 그대로 · B2.6 아님", not any(b[0].startswith("B2.6") for b in 시금["블록"]) and "2. 기자재 임차료" in str(시금["블록"])))
    검금 = 문맥_구성(cur3, 근거행=[책자행], 대상노드=set(), 검색={"top5": [300]}, schema=표2, 자금="창업활동자금")
    t.append(("금지 문장 항: 검색으로 들어와도 B2.6 에 금지 부분만", {제목[:5]: [i[3] for i in 항목] for 제목, 항목 in 검금["블록"]}.get("B2.6.") == ["차량 등의 임차 경비는 집행할 수 없다."]))
    금짧음 = 문맥_구성(cur3, 근거행=[책자행, 금지행], 대상노드=set(), l3=[{"article_id": 40, "조번호": "제1조", "조제목": "기관", "본문": "가" * 50, "concept_ids": None}], schema=표2, 자금="창업활동자금", 상한=95)
    t.append(("리뷰 11: B2.6 이 글자 상한으로 빠지면 L3_초과다(L3 는 다 들어가도)", any(제목.startswith("B2.5") for 제목, _ in 금짧음["블록"]) and not any(제목.startswith("B2.6.") for 제목, _ in 금짧음["블록"]) and 금짧음["L3_초과"] is True))
    t.append(("리뷰 11: B2.6 까지 다 들어가면 L3_초과가 아니다", 문맥_구성(cur3, 근거행=[책자행, 금지행], 대상노드=set(), l3=[{"article_id": 40, "조번호": "제1조", "조제목": "기관", "본문": "가" * 50, "concept_ids": None}], schema=표2, 자금="창업활동자금")["L3_초과"] is False))
    t.append(("순위: B2.6 은 L3 미연결 뒤 · 준용 2 앞, 항목수에 공통미정이 든다", L3미연결 < 공통미정 < 준용2 and any(제목.startswith("B2.6.") for 제목, _ in 창["블록"])))
    t.append(("다른 자금을 뺀 뒤 항목이 없으면 항목수 0", 문맥_구성(cur2, 근거행=[], 대상노드=set(), 검색={"top5": [200]}, schema=표2, 자금="창업활동자금")["항목수"] == 0))
    t.append(("근거 경로: 공통 미정 행은 화면 경로에 쓰지 않는다", 근거경로들([책자행, 미정행], 표2) == ["안내 책자 11쪽"]))
    요약4 = 규범_요약({"규범": [{"norm_id": "c", "종류": "금지", "verified": False, "근거": [{"칸": "대상", "발췌": "미확정"}]}], "공통미정": [{"norm_id": "x", "종류": "의무"}]},
                  {"조건": [{"문장": "안내 책자의 한도(1인 2,000,000원)를 넘습니다", "구분": "미확정한도"}]})
    t.append(("B4: 공통 미정 안내 · 미확정 한도 초과 · 금액 지시, 내부 ID 없음", "정해지지 않았다" in 요약4 and "확인 필요: 안내 책자의 한도" in 요약4 and "요약에 금액을" in 요약4 and "규범:" not in 요약4))
    # 리뷰 3c R1: 선택 칸 「해당」 답. 예외 · 묻지 않은 조건이 남은 금지는 「집행할 수 없다고 판단한다」 지시 대신 예외를 함께 준다. 답한 값을 줄에 적는다
    조건 = {"구조화": [{"항목": "거래처유형", "연산": "=", "값": "배우자"}], "자연어": []}
    금지들 = [{"norm_id": "e1", "종류": "금지", "verified": True, "예외": ["h1"], "조건": 조건, "결과": {"문장": "배우자에게 집행할 수 없다"}, "근거": [{"칸": "대상", "발췌": "원문 발췌"}]},
            {"norm_id": "h1", "종류": "허용", "verified": True, "결과": {"문장": "소속 직원이면 여비는 집행할 수 있다"}, "근거": [{"칸": "대상", "발췌": "원문 발췌"}]},
            {"norm_id": "e2", "종류": "금지", "verified": True, "조건": {**조건, "자연어": ["재직 중"]}, "결과": {"문장": "재직 기업에 집행할 수 없다"}, "근거": [{"칸": "대상", "발췌": "원문 발췌"}]},
            {"norm_id": "e3", "종류": "금지", "verified": True, "조건": 조건, "결과": {"문장": "친족에게 집행할 수 없다"}, "근거": [{"칸": "대상", "발췌": "원문 발췌"}]}]
    답g = {"조건": [], "진술평가": {"e1": True, "e2": True, "e3": True}, "답변": {"거래처유형": {"항목": "거래 상대", "답": "배우자"}}}
    줄5 = {x: next((l for l in (규범_요약({"규범": 금지들}, 답g) or "").splitlines() if 글 in l), "") for x, 글 in (("e1", "배우자에게"), ("e2", "재직 기업"), ("e3", "친족에게"))}
    t.append(("B4 해당 답: 예외 붙은 금지는 예외 설명을 주고 단정 지시 없음 · 자연어 조건은 그 조건을 준다 · 예외 없는 금지는 단정 · 답한 값을 적는다",
              "예외가 있다: 소속 직원이면 여비는 집행할 수 있다" in 줄5["e1"] and "집행할 수 없다고 판단한다" not in 줄5["e1"]
              and "「재직 중」이 더 있다" in 줄5["e2"] and "집행할 수 없다고 판단한다" not in 줄5["e2"]
              and "해당하면 집행할 수 없다고 판단한다" in 줄5["e3"] and all("이 조건(거래 상대: 배우자)에 해당한다고" in v for v in 줄5.values())))
    # 리뷰 28-8: 호만 있는 위치는 조 전체로 읽는다(근거 위치 확인과 같은 기준). 없는 항 기호는 여전히 막는다
    호본문 = "다음 각 호의 비용을 지원한다.\n1. 재료비\n2. 외주용역비\n10. 기타"
    try:
        _근거_조각(호본문, "③", "제5조")
        항없음_막힘 = False
    except ValueError:
        항없음_막힘 = True
    t.append(("28-8: 「2호」 · 「10호」 · 「2호의2」 근거는 조 전체 · 없는 항은 오류",
              all(_근거_조각(호본문, h, "제5조") == [(None, 호본문)] for h in ("2호", "10호", "2호의2")) and 항없음_막힘))
    # L3 중복 제거(2026-11-12): L1/L2 와 같은 글은 빼고, 숫자가 다르거나 다른 글이면 남긴다
    중복 = _모음()
    중복.넣기(규범근거, "article", 1, "doc", "11쪽", "책자 11쪽", None,
            "⑤ 여비·회의비 | 멘토 또는 아이디어 고도화와 관련된 외부인과의 회의에 소요되는 회의비 | 1일 숙박비 한도 100,000원")
    for i, 글 in enumerate(["⑤ 여비·회의비",
                           "멘토 또는 아이디어 고도화와 관련된 외부인과의 회의에 소요되는 회의비",
                           "1일 숙박비 한도 80,000원 멘토 또는 아이디어 고도화와 관련된 외부인과의 회의",
                           "회의실 대여료는 사전에 멘토기관 승인을 받아 집행한다",
                           "멘토 또는 아이디어 고도화와 관련된 외부인과의 회의에 소요되지 않는 회의비",
                           "멘토 또는 아이디어 고도화와 관련된 외부인과의 회의에 소요되는 회의비 오만원 이내"], 1):
        중복.넣기(L3미연결, "l3", 100 + i, f"L3:{100 + i}", f"단락{i}", f"단락{i}()", None, 글)
    중복.넣기(L3연결, "l3", 200, "L3:200", "단락연결", "단락연결()", None, "⑤ 여비·회의비")
    _L3중복_빼기(중복)
    남은L3 = sorted(x["id"] for x in 중복.표.values() if x["종류"] == "l3")
    t.append(("L3 중복 제거: 같은 글(짧은 제목 포함)은 빼고 숫자 · 부정 · 한글 수사가 다르거나 다른 글, 연결 확인한 L3 는 남긴다",
              남은L3 == [103, 104, 105, 106, 200] and any(x["종류"] == "article" for x in 중복.표.values())))
    실패 =[x for x, ok in t if not ok]
    for x, ok in t:
        print("ok  " if ok else "FAIL", x)
    print(f"selftest: {len(t) - len(실패)}/{len(t)} 통과")
    return 1 if 실패 else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_자체시험())
    print(__doc__)
