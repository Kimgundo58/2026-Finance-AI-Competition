# -*- coding: utf-8 -*-
"""온톨로지 원천의 공통 규칙: 발췌 정규화와 조문 · 규범 ID 풀이 (docs/5_룰/5-8b_원천_JSON_형식.md).

extraction_compare · ontology_input · ontology_check · ontology_load 가 같은 함수를 쓴다.
검사는 "정규화(발췌) ⊂ 정규화(본문)" 이고, 원천 JSON 의 발췌도 정규화한 형태로 저장한다.
DB · LLM 을 쓰지 않는다.
"""
from __future__ import annotations

import re
import unicodedata

# 모든 비목에 걸리는 노드. ontology_fund · ontology_context · ontology_lookup · ontology_load · ontology_check 가 쓴다
모든비목 = "비목:창업/*"
_따옴표 = str.maketrans({"“": '"', "”": '"', "″": '"', "‘": "'", "’": "'"})
# 줄 전체가 페이지 표식이거나 본문 사이에 끼어든 `- 14 -`. 숫자 범위 표기(10-20)는 공백이 없어 걸리지 않는다.
_페이지표식 = re.compile(r"(?:(?<=\s)|^)-\s*\d{1,3}\s*-(?=\s|$)")
_공백 = re.compile(r"\s+")


def 발췌_정규화(글: str | None) -> str:
    """유니코드 NFC → 따옴표 통일 → 페이지 표식 제거 → 공백 · 줄바꿈 연속을 공백 하나로 → 양끝 공백 제거."""
    s = unicodedata.normalize("NFC", 글 or "").translate(_따옴표)
    s = _페이지표식.sub(" ", s)
    return _공백.sub(" ", s).strip()


def 발췌_포함(발췌: str | None, 본문: str | None) -> bool:
    """정규화한 발췌가 정규화한 본문의 부분 문자열인가. 빈 발췌는 근거가 아니므로 거짓이다."""
    e = 발췌_정규화(발췌)
    return bool(e) and e in 발췌_정규화(본문)


_RE_조문ID = re.compile(r"^조:(?P<문서키>[^#]+)#(?P<나머지>.+)$")
# DB 조번호는 같은 번호가 문서에 두 번 나오면 `제35조[2]` 처럼 뒤에 [n] 을 붙인다(조번호의 일부).
_RE_조번호 = re.compile(r"^(제\d+조(?:의\d+)?(?:\[\d+\])?)")
# 부칙은 DB 조번호가 `부칙` 이거나 `부칙 제1조` 다. 뒤의 `제1조` 는 항호가 아니라 조번호의 일부다.
# `별지서식`(서식을 한 행에 모은 문서) · `별표1의2` · `붙임3[2]` 도 DB 조번호 그대로다.
_RE_첨부 = re.compile(r"^((?:별지서식|별지|별표|붙임)\s*\d*(?:의\d+)?(?:\[\d+\])?|부칙(?:\s*제\d+조(?:의\d+)?)?)")
_RE_책자 = re.compile(r"^((?:참고\d+-)?p\d+-\d+)")
# 공고문 · 관리기준 뒤의 참고 자료(`참고1`). 책자 쪽(`참고1-p3-2`)은 _RE_책자 가 먼저 잡는다.
_RE_참고 = re.compile(r"^(참고\d+)")
# 집행지침은 편 · 장 단위다(`제2편Ⅱ-12` · `표지`). 조가 아니라 뒤에 항호가 없다.
_RE_편 = re.compile(r"^(제\d+편(?:[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:-\d+)?)?|표지)")


def 조문ID_풀이(조문ID: str) -> dict:
    """`조:모두의창업#제36조①` → {문서키, 조번호, 항호}. `#` 뒤의 조번호를 뺀 나머지가 항호다.

    형식이 맞지 않으면 ValueError.
    """
    m = _RE_조문ID.match((조문ID or "").strip())
    if not m:
        raise ValueError(f"조문 ID 형식이 아니다: {조문ID!r}")
    문서키, 나머지 = m.group("문서키"), m.group("나머지")
    for 패턴 in (_RE_조번호, _RE_첨부, _RE_책자, _RE_참고, _RE_편):
        n = 패턴.match(나머지)
        if n:
            조번호 = n.group(1).strip()
            항호 = 나머지[n.end():].strip() or None
            return {"문서키": 문서키, "조번호": 조번호, "항호": 항호}
    raise ValueError(f"조번호를 찾지 못했다: {조문ID!r}")


def 규범ID_풀이(규범ID: str) -> dict:
    """`규범:모두의창업/제36조①/한도/1` → {범위, 조문키, 종류, 순번}. 조문키 안에 `/` 가 있어도 뒤에서 자른다."""
    s = (규범ID or "").strip()
    if not s.startswith("규범:"):
        raise ValueError(f"규범 ID 형식이 아니다: {규범ID!r}")
    조각 = s[3:].split("/")
    if len(조각) < 4:
        raise ValueError(f"규범 ID 조각이 모자란다: {규범ID!r}")
    return {"범위": 조각[0], "조문키": "/".join(조각[1:-2]), "종류": 조각[-2], "순번": 조각[-1]}


def 개념ID_풀이(개념ID: str) -> dict:
    """`세목:창업/지급수수료/멘토링비@모두의창업` → {종류, 경로, 이름, 사업키}."""
    s = (개념ID or "").strip()
    if ":" not in s:
        raise ValueError(f"개념 ID 형식이 아니다: {개념ID!r}")
    종류, 경로 = s.split(":", 1)
    사업키 = None
    if "@" in 경로:
        경로, 사업키 = 경로.rsplit("@", 1)
    return {"종류": 종류, "경로": 경로, "이름": 경로.rsplit("/", 1)[-1], "사업키": 사업키}


def _칸이름(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def 별지_행(본문: str) -> list[tuple[str, str]]:
    """별지 마크다운 표를 (`<비목>/<칸>`, 내용) 으로 푼다. 첫 칸이 비면 앞 비목, 둘째 칸이 비면 앞 칸의 이어짐이다."""
    행: dict[str, list[str]] = {}
    비목, 칸 = "", ""
    두칸_증빙표 = False
    for 줄 in 본문.splitlines():
        if not 줄.lstrip().startswith("|"):
            두칸_증빙표 = False
            continue
        셀 = [c.strip() for c in 줄.strip().strip("|").split("|")]
        if set("".join(셀)) <= set("-: "):
            continue
        # 페이지를 넘길 때 반복되는 세 칸 표 머리글은 앞 행의 비목·칸을 바꾸지 않는다.
        if len(셀) == 3 and _칸이름(셀[0]) == '비목' and _칸이름(셀[1]) == '주요내용' and not 셀[2]:
            continue
        if len(셀) == 2 and _칸이름(셀[0]) == '비목' and _칸이름(셀[1]) in ('집행증빙서류', '기준'):
            이전표 = 두칸_증빙표
            두칸_증빙표 = True
            if not 이전표:
                비목, 칸 = '', ''
            continue
        if len(셀) == 2 and 두칸_증빙표:
            if 셀[0]:
                비목 = _칸이름(셀[0])
            if 비목 and 셀[1]:
                행.setdefault(비목, []).append(셀[1])
            continue
        if len(셀) >= 3 and 셀[0] == '비목':
            두칸_증빙표 = False
        if len(셀) < 3 or 셀[0] == "비목":
            continue
        a, b, 내용 = 셀[0], 셀[1], " | ".join(셀[2:])
        if a:
            비목, 칸 = _칸이름(a), ""
        if b:
            칸 = _칸이름(b)
        if not 내용:
            continue
        키 = f"{비목}/{칸}" if 칸 else 비목
        행.setdefault(키, []).append(내용)
    return [(k, " ".join(v)) for k, v in 행.items()]


def 별지_행_내용(본문: str, 항호: str, 대체: bool = True) -> str | None:
    """조문 ID 의 별지 행 키(`/지급수수료/유의사항`)에 해당하는 칸 내용. 없으면 None.

    대체=True 이면 괄호 뺀 비목 이름·합쳐진 기준 칸으로도 찾는다. 위치 검증은 이 대체 칸에
    저장 발췌가 있는지 따로 확인한다(ontology_evidence._위치_확인).
    """
    키 = (항호 or "").lstrip("/")
    행들 = dict(별지_행(본문 or ""))
    찾음 = 행들.get(키)
    if not 찾음 and 대체:
        # 표의 비목 칸이 「기계장치 (공구·기구,비품, SW등)」처럼 괄호 설명을 붙인 경우.
        # 괄호를 뺀 이름이 하나의 행에만 맞을 때만 그 행을 쓴다.
        def 괄호뺌(k):
            return "/".join(re.sub(r"\([^)]*\)", "", 칸) for 칸 in k.split("/"))
        맞음 = [v for k, v in 행들.items() if 괄호뺌(k) == 괄호뺌(키)]
        if len(맞음) == 1:
            찾음 = 맞음[0]
    if (not 찾음 and 대체 and 키.endswith('/유의사항') and 키 != '교육훈련비/유의사항'
            and (본문 or '').count('창업중심대학 창업기업 사업비 집행기준') == 1):
        # 창업중심대학 참고3 표는 외주용역비만 유의사항 칸이 따로 있고, 나머지 비목은
        # 유의사항(※ 문장)을 같은 비목의 「기준」 칸에 합쳤다. 유의사항 칸이 없는 비목만 기준 칸을 쓴다.
        비목 = 키[:-len('/유의사항')]
        if not any(re.sub(r"\([^)]*\)", "", k.split("/")[0]) == 비목 and k.endswith('/유의사항') for k in 행들):
            return 별지_행_내용(본문, f'/{비목}/기준')
    if not 찾음 and 키 == '교육훈련비/유의사항' and (본문 or '').count('창업중심대학 창업기업 사업비 집행기준') == 1:
        # 참고3 복원표는 교육훈련비 유의사항을 같은 비목의 기준 셀에 합쳤다.
        # 빈 유의사항과 바로 뒤 다음 비목이 모두 명시된 유일한 표만 허용한다.
        줄들 = (본문 or '').splitlines()
        셀들 = [[c.strip() for c in 줄.strip().strip('|').split('|')]
                if 줄.lstrip().startswith('|') else [] for 줄 in 줄들]
        후보 = []
        for i in range(len(셀들) - 2):
            a, b, c = 셀들[i:i + 3]
            if (len(a) == len(b) == len(c) == 3 and _칸이름(a[0]) == '교육훈련비'
                    and _칸이름(a[1]) == '기준' and a[2] and not b[0]
                    and _칸이름(b[1]) == '유의사항' and not b[2]
                    and _칸이름(c[0]) == '광고선전비' and _칸이름(c[1]) == '기준' and c[2]):
                후보.append(a[2])
        if len(후보) == 1:
            return 후보[0]
    if 찾음:
        # 초격차 참고2는 표의 기술이전 세부항목이 같은 조의 평문으로
        # 이동되어 있다. 두 소항목 표제가 유일할 때만 해당 구간을 잇는다.
        if 키 == '지급수수료' and (본문 or '').count('[참고2] 초격차 창업기업 사업비 집행기준') == 1:
            시작표제 = '· (기술이전비)'
            끝표제 = '· (학회·세미나 참가비)'
            if 본문.count(시작표제) == 본문.count(끝표제) == 1:
                시작, 끝 = 본문.index(시작표제), 본문.index(끝표제)
                if 시작 < 끝:
                    찾음 += ' ' + 본문[시작:끝].strip()
        return 찾음
    # 참고3: 유일한 집행기준 제목 바로 뒤의 고지 문단만 지정한다.
    if 키 == '집행기준':
        제목 = '창업중심대학 창업기업 사업비 집행기준'
        if (본문 or '').count(제목) == 1:
            뒤 = 본문.split(제목, 1)[1].lstrip()
            문단 = re.split(r'\n\s*\n', 뒤, maxsplit=1)[0].strip()
            if 문단.startswith('※'):
                return 문단
    if 키 == '사업비지급제한및환수':
        표들 = re.findall(r'(?m)^\|\s*구분\s*\|\s*내용\s*\|[^\n]*\n(?:\|[^\n]*\|[^\n]*(?:\n|$))+', 본문 or '')
        후보 = [표 for 표 in 표들 if re.search(r'(?m)^\|\s*지급불가\s*대상\s*\|', 표)
              and re.search(r'(?m)^\|\s*사업비\s*환수\s*\|', 표)]
        return 후보[0].strip() if len(후보) == 1 else None
    if 키 == '사업비집행및처리기간':
        if (본문 or '').count('□ 사업비 집행 및 처리기간') != 1:
            return None
        표들 = re.findall(r'(?m)^\|\s*구분\s*\|\s*내용\s*\|[^\n]*\n(?:\|[^\n]*\|[^\n]*(?:\n|$))+', 본문 or '')
        후보 = [표 for 표 in 표들 if re.search(r'(?m)^\|\s*사업비\s*집행\s*(?:가능기간|기간)\s*\|', 표)]
        return 후보[0].strip() if len(후보) == 1 else None
    if 키 == '사업비집행·청구':
        if (본문 or '').count('□ 사업비 집행·청구') != 1:
            return None
        표들 = re.findall(r'(?m)^\|\s*구분\s*\|\s*\|\s*내용\s*\|[^\n]*\n(?:\|[^\n]*\|[^\n]*(?:\n|$))+', 본문 or '')
        유의 = [표 for 표 in 표들 if re.search(r'(?m)^\|\s*유\s*의\s*사\s*항\s*\|', 표)]
        금액 = [표 for 표 in 표들 if re.search(r'(?m)^\|\s*금\s*액\s*별\s*\|', 표)]
        if len(유의) == len(금액) == 1 and 유의[0] != 금액[0]:
            return 유의[0].strip() + '\n' + 금액[0].strip()
        return None
    if 키 in ('지급수수료/기술이전비', '지급수수료/기계장치수리비',
             '지급수수료/기자재임차비', '지급수수료/사무실임차료', '지급수수료/시험·인증비'):
        # 비목·기준 표의 해당 그룹 안에서 번호가 붙은 소항목을 찾는다.
        묶음, 활성 = [], False
        for 줄 in (본문 or '').splitlines():
            if not 줄.lstrip().startswith('|'):
                if not 줄.strip():
                    continue
                if 활성:
                    break
                continue
            셀 = [c.strip() for c in 줄.strip().strip('|').split('|')]
            if len(셀) < 3:
                continue
            if set(''.join(셀)) <= set('-: '):
                continue
            if 셀[0]:
                if 활성 and _칸이름(셀[0]) == '비목':
                    continue
                if 활성:
                    break
                활성 = _칸이름(셀[0]) == '지급수수료' and '유의사항' in _칸이름(셀[1])
            if 활성:
                묶음.extend(x for x in 셀[2:] if x)
        글 = ' '.join(묶음)
        이름 = 키.rsplit('/', 1)[-1]
        이름식 = r'\s*'.join(map(re.escape, 이름))
        식 = re.compile(r'(?<!\d)(\d{1,2})\)\s*' + 이름식 + r'\s*[:：]')
        찾음 = list(식.finditer(글))
        if len(찾음) != 1:
            return None
        시작 = 찾음[0].start()
        다음 = re.search(r'(?<!\d)' + str(int(찾음[0].group(1)) + 1) + r'\)\s*[^:：]{1,50}[:：]', 글[찾음[0].end():])
        끝 = 찾음[0].end() + 다음.start() if 다음 else len(글)
        return 글[시작:끝].strip()
    # 참고5: 이동된 3열 표의 그룹명과 명시적 부모 절을 함께 확인한다.
    if 키 in ('사업비집행·청구/유의사항', '사업비집행·청구/결제수단별',
             '사업비집행·청구/금액별', '사업비집행·청구/항목별'):
        if (본문 or '').count('□ 사업비 집행·청구') != 1:
            return None
        그룹 = 키.rsplit('/', 1)[-1]
        수집, 활성, 머리, 결과 = [], False, False, []
        for 줄 in 본문.splitlines():
            if not 줄.lstrip().startswith('|'):
                if 수집:
                    결과.append(수집)
                    수집 = []
                활성 = 머리 = False
                continue
            셀 = [c.strip() for c in 줄.strip().strip('|').split('|')]
            if len(셀) != 3:
                continue
            if _칸이름(셀[0]) == '구분' and not 셀[1] and _칸이름(셀[2]) == '내용':
                머리 = True
                continue
            if not 머리 or set(''.join(셀)) <= set('-: '):
                continue
            if 셀[0]:
                if 수집:
                    결과.append(수집)
                    수집 = []
                활성 = _칸이름(셀[0]) == 그룹
            if 활성 and 셀[2]:
                수집.append(' | '.join(x for x in 셀[1:] if x))
        if 수집:
            결과.append(수집)
        if len(결과) == 1:
            return ' '.join(결과[0])
    return None


def 조번호_표시(조번호: str | None) -> str | None:
    """화면에 낼 조번호. 쪽 단위로 복원한 문서의 내부 표기(`p9-1`, `참고2-p29-1`)를 쪽 표기로 바꾼다."""
    if not 조번호:
        return 조번호
    m = re.fullmatch(r"(?:(참고\d+)-)?p(\d+)-\d+", 조번호.strip())
    if not m:
        return 조번호
    return f"{m.group(1)} {m.group(2)}쪽" if m.group(1) else f"{m.group(2)}쪽"
