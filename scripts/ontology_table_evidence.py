# -*- coding: utf-8 -*-
"""조문 앞에 배치된 명시적 마크다운 표의 원문 위치만 찾는다."""
from __future__ import annotations

import re


def 내부_표_구간(본문: str, 조번호: str, 항호: str) -> tuple[int, int]:
    """본문 ② 다음에 배치된 표를 명시 참조·표제·페이지 경계로 확인한다."""
    항 = re.fullmatch(r'②표-(\d+)', 항호 or '')
    if not 항 or not re.fullmatch(r'제\d+조(?:의\d+)?', 조번호 or ''):
        raise ValueError('내부 표 근거 위치 형식 오류')
    if not isinstance(본문, str) or not re.match(r'\s*' + re.escape(조번호) + r'\s*\(', 본문):
        raise ValueError('내부 표 조문 위치 불일치')
    번호 = re.escape(항.group(1))
    제목식 = re.compile(r'<\s*표\s*-\s*' + 번호 + r'\s*>')
    항시작 = list(re.finditer(r'(?m)^\s*②\s+', 본문))
    if not 항시작:
        raise ValueError('내부 표 상위 항 없음')
    상위 = 항시작[0].start()
    참조 = 제목식.search(본문, 상위)
    if not 참조:
        return _괄호_표제_구간(본문, 상위, 번호)
    # 앞의 참조 <표-N> 하나 다음에 표제 < 표-N >가 와야 한다.
    제목 = 제목식.search(본문, 참조.end())
    if not 제목 or len(제목식.findall(본문[상위:제목.start()])) != 1:
        raise ValueError('내부 표 제목 누락')
    경계식 = re.compile(r'(?m)^\s*-\s*\d+\s*-\s*\n(?=③\s+)')
    경계들 = list(경계식.finditer(본문, 제목.end()))
    호경계 = False
    if not 경계들:
        # ②의 제1호 안에 표가 있으면 다음 제2호가 표를 끝낸다.
        # 제3호까지 이어지는 형식과 유일한 제2호를 확인한다.
        후보 = list(re.finditer(r'(?m)^2\.\s+\S', 본문[제목.end():]))
        셋 = list(re.finditer(r'(?m)^3\.\s+\S', 본문[제목.end():]))
        첫호 = list(re.finditer(r'(?m)^1\.\s+\S', 본문[상위:제목.start()]))
        if len(첫호) == len(후보) == len(셋) == 1 and 후보[0].start() < 셋[0].start():
            경계들 = [re.compile(r'(?m)^2\.\s+\S').search(본문, 제목.end())]
            호경계 = True
    if len(경계들) != 1:
        raise ValueError('내부 표 끝 경계가 없거나 여러 개임')
    경계 = 경계들[0]
    if 제목식.search(본문, 제목.end(), 경계.start()):
        raise ValueError('내부 표 끝 경계 불명')
    다음 = 본문[경계.end():]
    다음항끝 = re.search(r'(?m)^④\s+', 다음)
    다음항 = 다음[:다음항끝.start()] if 다음항끝 else 다음
    if not 호경계 and not 제목식.search(다음항):
        raise ValueError('내부 표 뒤의 상위 항 확인 실패')
    표본문 = 본문[제목.start():경계.start()]
    if not all(re.search(r'(?m)^\s*' + x, 표본문) for x in ('①', '②', '③')):
        raise ValueError('내부 표 항목 구조 불명')
    return 제목.start(), 경계.start()


def _괄호_표제_구간(본문: str, 상위: int, 번호: str) -> tuple[int, int]:
    """② 본문이 「아래 표-N」으로 가리키고 표제가 `< (표-N) 제목 >`인 형식(초격차 제18조).

    표제는 조문 전체에서 하나, ② 안에서 표제 앞에 평문 참조가 있어야 하고,
    표는 표제 다음 줄부터 바로 다음 ③ 항 앞까지다. 하나라도 어긋나면 추측하지 않는다.
    """
    제목식 = re.compile(r'<\s*\(\s*표\s*-\s*' + 번호 + r'\s*\)[^<>\n]*>')
    제목들 = list(제목식.finditer(본문))
    if len(제목들) != 1 or 제목들[0].start() < 상위:
        raise ValueError('내부 표 참조 또는 표제 불일치')
    제목 = 제목들[0]
    if not re.search(r'표\s*-\s*' + 번호 + r'(?!\d)', 본문[상위:제목.start()]):
        raise ValueError('내부 표 참조 없음')
    다음항 = list(re.finditer(r'(?m)^③\s+', 본문[제목.end():]))
    if not 다음항 or re.search(r'(?m)^[②③]\s+', 본문[상위 + 1:제목.start()]):
        raise ValueError('내부 표 끝 경계 불명')
    끝 = 제목.end() + 다음항[0].start()
    if not 본문[제목.end():끝].strip():
        raise ValueError('내부 표 내용 없음')
    return 제목.start(), 끝


def 앞쪽_표_구간(본문: str, 조번호: str, 항호: str) -> tuple[int, int]:
    """`①표-N` 또는 `표-N`과 조문이 명시한 유일한 앞쪽 표의 원문 좌표.

    표가 조문 앞에 위치하는 복원 형식만 허용한다. 구조가 다른 문서는
    추측하지 않고 실패한다. 반환 구간은 DB 원문 자체의 연속 부분이다.
    """
    항 = re.fullmatch(r"([①②③④⑤⑥⑦⑧⑨⑩])?표-(\d+)", 항호 or "")
    if not 항 or not re.fullmatch(r"제\d+조(?:의\d+)?", 조번호 or ""):
        raise ValueError("표 근거 위치 형식 오류")
    if not isinstance(본문, str):
        raise ValueError("표 원문 없음")

    위치 = 0
    줄들 = 본문.splitlines(keepends=True)
    for 줄 in 줄들:
        if not 줄.lstrip().startswith("|"):
            break
        위치 += len(줄)
    표 = 본문[:위치]
    if not 표 or len(표.splitlines()) < 3:
        raise ValueError("조문 앞 연속 표 없음")
    # 서로 다른 표가 이어진 형식은 식별하지 않는다. 반복 머리글은 허용한다.
    if len(re.findall(r"(?m)^[ \t]*\|[ \t]*:?-{2,}:?[ \t]*\|", 표)) != 1:
        raise ValueError("마크다운 표 구분선이 없거나 여러 개임")
    조문 = re.match(r"\s*" + re.escape(조번호) + r"\s*\([^\n]*\)\s*", 본문[위치:])
    if not 조문:
        raise ValueError("표 뒤 조문 위치 불일치")
    나머지 = 본문[위치 + 조문.end():]
    if not 나머지 or 나머지[0] not in '①②③④⑤⑥⑦⑧⑨⑩' or (항.group(1) and not 나머지.startswith(항.group(1))):
        raise ValueError("표를 가리키는 항 위치 불일치")
    # 표 내부 기호는 이 검사 대상이 아니다. 조문 안의 다음 항 이전만 확인한다.
    끝 = re.search(r"[①②③④⑤⑥⑦⑧⑨⑩]", 나머지[1:])
    항본문 = 나머지[:끝.start() + 1] if 끝 else 나머지
    번호 = re.escape(항.group(2))
    참조 = re.compile(r"표\s*-\s*" + 번호 + r"(?!\d)")
    제목 = re.compile(r"<\s*표\s*-\s*" + 번호 + r"(?!\d)[^>]*>")
    if len(참조.findall(항본문)) != 2 or len(제목.findall(항본문)) != 1 or len(참조.findall(나머지)) != 2:
        raise ValueError("표 번호 또는 표제 불일치")
    if len(re.findall(r"<\s*표\s*-\s*\d+[^>]*>", 본문)) != 1:
        raise ValueError("여러 표의 위치를 구분할 수 없음")
    return 0, 위치
