# -*- coding: utf-8 -*-
"""index_guard 회귀 테스트.

    python tests/test_index_guard.py      (pytest 없이도 돈다)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# 🔴 2026-09-05 index_guard.py 가 scripts/archive/eval/ 로 이관됐다 — 서빙 경로에 안 쓰여서.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "archive" / "eval"))
from index_guard import IndexGuardError, assert_indexable, is_indexable  # noqa: E402

BLOCKED = [
    ("archive/구_데이터/L4_기관규정/서울대_연구비관리규정.pdf", None),
    ("2026_Finance_DATA_FOR_RAG/_골든셋/별첨4.txt", None),
    ("2026_Finance_DATA_FOR_RAG/_테스트_L3/건국대.pdf", None),
    ("법령 PDF/L1_법령/_범위밖_보류/고등교육법.xml", None),
    (r"archive\구_데이터\x.pdf", None),          # 윈도우 역슬래시
    ("2026_Finance_DATA_FOR_RAG/창진원/x.pdf", "L4"),  # 레이어로도 막힌다
    # 🔴 L3 는 2026-08-31 부터 차단이다 (`index_guard.BLOCKED_LAYERS`).
    #    L3 는 `corpus.chunks` 가 아니라 `tenant.l3_articles` 로 가고 검색 없이 통째로
    #    로드된다 (CLAUDE.md 「검색 대상의 경계」). 판정 인덱스에 들어가는 순간
    #    남의 기관 규정이 다른 기관 판정에 섞일 수 있어 구조적으로 막았다.
    #    이 줄은 그때까지 ALLOWED 에 남아 있었다 — **테스트가 구 계약을 붙들고 있었고,
    #    가드는 의도대로 동작하고 있었다** (2026-09-01, ai-14 가 발견해 넘김).
    ("uploads/기관_KU/산학협력단_연구비규정.pdf", "L3"),
]

ALLOWED = [
    ("2026_Finance_DATA_FOR_RAG/창진원/초기창업패키지_세부관리기준_2025.pdf", "L2"),
    ("법령 PDF/L1_법령/중소기업창업지원법.xml", "L1"),
]


def test_blocked():
    for path, layer in BLOCKED:
        assert not is_indexable(path, layer), f"막았어야 한다: {path}"
        try:
            assert_indexable(path, layer)
        except IndexGuardError:
            pass
        else:
            raise AssertionError(f"예외가 났어야 한다: {path}")


def test_allowed():
    for path, layer in ALLOWED:
        assert is_indexable(path, layer), f"통과했어야 한다: {path}"
        assert_indexable(path, layer)


if __name__ == "__main__":
    test_blocked()
    test_allowed()
    print(f"통과 — 차단 {len(BLOCKED)}건 / 허용 {len(ALLOWED)}건")
