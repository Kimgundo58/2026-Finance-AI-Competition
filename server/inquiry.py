# -*- coding: utf-8 -*-
"""판단불가일 때 사용자가 주관기관에 보낼 문의 초안을 만든다.

LLM 을 부르지 않는다 — 판정이 이미 쥔 값(정규화·인용·전제)으로 코드가 문장을 조립한다.
없는 값은 지어내지 않고 그 문장을 통째로 뺀다. 남길 본문이 없으면 `None`.
규정 원문은 옮기지 않는다 — 조번호·조제목까지다.
"""
from __future__ import annotations

import re

# doc_id 앞의 레이어 접두사와 뒤의 날짜 토큰만 떼어 읽을 수 있는 규범명으로 만든다.
_레이어접두 = re.compile(r"^L[1-4]_")
_뒤날짜 = re.compile(r"[_\s]\d{8}$")

인사 = "안녕하세요."
맺음 = "확인 부탁드립니다. 감사합니다."


def _규범명(doc_id) -> str:
    """`L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223`
       → `중소기업창업 지원사업 통합관리지침 제14차개정`"""
    s = str(doc_id or "").strip()
    if not s:
        return ""
    s = _레이어접두.sub("", s)
    s = _뒤날짜.sub("", s)
    return s.replace("_", " ").strip()


def 금액표기(금액) -> str | None:
    """`2500000` → `250만원` · 만 단위로 안 떨어지면 `2,500,000원` · 없으면 `None`(0원이 아니라 모른다)."""
    if 금액 is None or isinstance(금액, bool):
        return None
    try:
        n = int(round(float(금액)))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n % 10000 == 0:
        만 = n // 10000
        if 만 % 10000 == 0:                       # 1억 단위까지만 접는다
            return f"{만 // 10000}억원"
        return f"{만:,}만원"
    return f"{n:,}원"


def _인용문장(인용: list) -> str | None:
    """「{규범명} {조번호}({조제목}) 기준을 확인했습니다.」 — 최대 2건. 조번호 없는 인용은 건너뛴다."""
    조각 = []
    for c in 인용 or []:
        if not isinstance(c, dict):
            continue
        조번호 = str(c.get("조번호") or "").strip()
        if not 조번호:
            continue
        제목 = str(c.get("조제목") or "").strip()
        규범 = _규범명(c.get("doc_id"))
        한건 = " ".join(x for x in (규범, 조번호) if x)
        if 제목:
            한건 += f"({제목})"
        # 한 조가 여러 항으로 인용되면 조번호·조제목이 같다 — 같은 조를 두 번 적지 않는다.
        if 한건 in 조각:
            continue
        조각.append(한건)
        if len(조각) == 2:
            break
    if not 조각:
        return None
    return f"{' · '.join(조각)} 기준을 확인했습니다."


def _전제블록(전제: list) -> list[str]:
    """확인이 필요한 사실 목록. `사실` 은 한 글자도 안 바꾼다."""
    사실들 = []
    for t in 전제 or []:
        사실 = (t.get("사실") if isinstance(t, dict) else t)
        사실 = str(사실 or "").strip()
        if 사실 and 사실 not in 사실들:
            사실들.append(사실)
    if not 사실들:
        return []
    return ["아래 사항이 확인되지 않아 자체적으로 판단하기 어렵습니다."] + [
        f"- {x}" for x in 사실들]


def 문의초안(정규화: dict, 판정: str, 인용: list, 전제: list,
           사업명: str | None) -> str | None:
    """판단불가 판정을 주관기관에 보낼 문의로 옮긴다. 판단불가가 아니거나 본문이 비면 `None`."""
    if 판정 != "판단불가":
        return None

    정규화 = 정규화 or {}
    사업 = str(사업명 or 정규화.get("사업명") or "").strip()
    머리 = f"{인사} {사업} 참여기업입니다." if 사업 else 인사

    품목 = str(정규화.get("품목") or "").strip()
    돈 = 금액표기(정규화.get("금액"))
    본문: list[str] = []
    if 품목:
        # 금액이 없으면 품목이 물건이 아니라 질문일 수 있어 「집행이」 어투를 안 쓴다.
        if 돈:
            본문.append(f"{품목}({돈}) 집행이 사업비로 가능한지 문의드립니다.")
        else:
            본문.append(f"「{품목}」에 대해 사업비 집행이 가능한지 문의드립니다.")
    elif 돈:
        본문.append(f"{돈} 집행이 사업비로 가능한지 문의드립니다.")

    근거 = _인용문장(인용)
    if 근거:
        본문.append(근거)
    전제줄 = _전제블록(전제)
    if 전제줄:
        본문 += 전제줄
    elif 본문:
        # 전제가 없으면 이유가 통째로 빠진다. 전제를 지어내지 않고 「판단이 안 선다」만 적는다.
        본문.append("이 기준만으로는 해당 여부를 자체적으로 판단하기 어렵습니다.")

    # 인사말·맺음말만 남은 초안은 만들지 않는다.
    if not 본문:
        return None
    return "\n\n".join([머리, "\n".join(본문), 맺음])
