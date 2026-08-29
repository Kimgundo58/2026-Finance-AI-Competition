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

BLOCKED_LAYERS: frozenset[str] = frozenset({"L4", "L5"})


def reject_reason(path: str, layer: str | None = None) -> str | None:
    """거부 사유를 돌려준다. 넣어도 되면 None."""
    norm = str(path).replace("\\", "/")
    for seg in BLOCKED_PATHS:
        if seg in norm:
            return f"경로 블랙리스트 `{seg}`"
    if layer and layer.upper() in BLOCKED_LAYERS:
        return f"레이어 `{layer}` 는 판정 인덱스 대상이 아니다 (테스트 전용)"
    return None


def is_indexable(path: str, layer: str | None = None) -> bool:
    return reject_reason(path, layer) is None


def assert_indexable(path: str, layer: str | None = None) -> None:
    """넣으면 안 되는 것이면 예외. 조용히 지나가지 않는다."""
    why = reject_reason(path, layer)
    if why is not None:
        raise IndexGuardError(f"판정 인덱스 투입 거부 — {why}: {path}")
