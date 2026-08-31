# -*- coding: utf-8 -*-
"""판정 인덱스 투입 게이트 — "실수로 안 넣기"가 아니라 "넣을 수 없게" 만든다.

CLAUDE.md 는 예전부터 *"인덱서는 경로 블랙리스트를 코드로 강제한다"* 고 선언했으나
2026-08-28 확인 결과 **실제 코드에는 없었다.** 여기가 그 구현이다.

거부 대상
  archive/          구 74파일 데이터셋 · 폐기 문서. 이력 추적용
  _골든셋/           창진원 공식 답변. 넣으면 정답 유출
  _테스트_L3/        L3 업로드 파이프라인 테스트 입력
  _범위밖_보류/      위임 추적에 딸려온 무관 규범 161건
  layer == "L4"     타 대학·타 기관 규정 24건. 남의 학교 규정이 사용자 판정에
                    인용되는 순간 그 자체가 오답이다

L4 는 `documents` 에는 남긴다 — L3 파이프라인 테스트(파싱 게이트 통과율·참조 해소율·
멀티테넌시 누수)에 입력으로 쓴다. 다만 index_target 은 무조건 False 다.
"""
from __future__ import annotations


class IndexGuardError(RuntimeError):
    """판정 인덱스에 넣으면 안 되는 것을 넣으려 했다."""


BLOCKED_PATHS: tuple[str, ...] = (
    "archive/",
    "_골든셋/",
    "_테스트_L3/",
    "_범위밖_보류/",
)

# 🔴 2026-08-31 L3 추가 (E 세션 BLOCKED · A 처리).
#    `CLAUDE.md` 의 "L3 는 다른 테이블이라 누수가 구조적으로 불가능하다" 는 L3 가
#    `corpus.chunks` 에 **절대 안 들어갈 때만** 참이다. 그전까지는 어떤 인제스천 경로가
#    layer='L3' 로 태깅해 `stage0_ingest` 를 태우면 `index_target=True` 로 통과했다.
#    현재 chunks 는 L1 20,030 · L2 495 로 깨끗해서 **지표에 안 나타나는 무음 구멍**이었다.
BLOCKED_LAYERS: frozenset[str] = frozenset({"L3", "L4", "L5"})

# 같은 거부라도 이유가 다르다. L4·L5 는 "남의 규정이라 테스트 전용" 이고,
# L3 는 "우리 기관 규정이지만 **가는 테이블이 다르다**" 이다. 한 문장으로 뭉치면
# 나중에 이 메시지를 본 사람이 L3 를 통과시키려 든다.
_거부문구 = {"L3": "레이어 `L3` 는 tenant.l3_articles 로 간다 — 검색 인덱스 대상이 아니다"}


def reject_reason(path: str, layer: str | None = None) -> str | None:
    """거부 사유를 돌려준다. 넣어도 되면 None."""
    norm = str(path).replace("\\", "/")
    for seg in BLOCKED_PATHS:
        if seg in norm:
            return f"경로 블랙리스트 `{seg}`"
    if layer and layer.upper() in BLOCKED_LAYERS:
        return _거부문구.get(layer.upper(),
                            f"레이어 `{layer}` 는 판정 인덱스 대상이 아니다 (테스트 전용)")
    return None


def is_indexable(path: str, layer: str | None = None) -> bool:
    return reject_reason(path, layer) is None


def assert_indexable(path: str, layer: str | None = None) -> None:
    """넣으면 안 되는 것이면 예외. 조용히 지나가지 않는다."""
    why = reject_reason(path, layer)
    if why is not None:
        raise IndexGuardError(f"판정 인덱스 투입 거부 — {why}: {path}")
