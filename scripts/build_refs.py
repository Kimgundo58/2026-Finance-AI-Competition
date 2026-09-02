# -*- coding: utf-8 -*-
"""참조 그래프 추출 → `_refs.json`.

RAG 의 존재 이유가 이것이다 (구현.md 원칙 5). 사용자 문서가
*"통합관리지침 제33조부터 제42조까지에 따른다"* 로 끝나면 그 문서만으로는 답이 없다.
가리키는 곳을 따라가 실제 조항에 닿아야 한다.

한 행 = 엣지 하나. 그래프 DB 는 쓰지 않는다 — 깊이 3, 재귀 CTE 로 밀리초다.
스키마: db/init/01_schema.sql 의 `refs` 테이블. 상세: RAG.md §4-3

해소 상태 3종
    resolved  대상이 규정 모음에 있고 조번호도 맞는다
    shifted   조번호가 구판이라 조제목으로 재매칭했다  (실측 다수)
    dangling  대상이 규정 모음에 없다 → 판정 시점이 아니라 **업로드 시점에 알린다**

실행:
    python scripts/build_refs.py                  전체
    python scripts/build_refs.py --src 창진원      일부만
    python scripts/build_refs.py --show 예비창업    결과 확인

🔴 **`dst_doc_id` 를 여기서 `doc_id` 로 정규화하지 않는다 — 일부러가 아니라 아직 안 한 것이다.**
   현재 이 스크립트는 dst 에 세 갈래를 그대로 넣는다:
     · 파일 경로 (`법령 PDF/L1_법령/법인세법시행령`)  ← 177행 계열
     · 약칭 (`L1_통합관리지침_제14차`)                ← 196·201행 계열
     · 정상 doc_id
   그래서 적재하면 resolved 의 절반이 `documents` 에 없는 dst 를 가리키고,
   **refs 참조 확장이 `폐포전용` 문서에 도달하지 못한다** (2026-08-31 실측: 도달 0건).
   `RAG.md` §4-2 의 `retrieval_scope` 재태깅이 이 참조 확장을 안전망으로 전제하므로 치명적이다.

   ⚠️ **이 스크립트를 다시 돌려 적재했다면 반드시 이어서 실행할 것:**
       PYTHONIOENCODING=utf-8 python scripts/normalize_refs.py --apply
   멱등이고, 매칭 실패는 조용히 넘기지 않고 목록으로 보고한다.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import db  # noqa: E402
import pdftext  # noqa: E402  문자중복 레이어 + 2단 조판 자동 처리
from stage0_articles import split_articles  # noqa: E402  섹션분리 + 3단 대체 경로
from scope import 범위밖_조                   # noqa: E402  모두의창업 제3편 로컬트랙 컷

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "2026_Finance_DATA_FOR_RAG" / "_refs.json"

# ── 참조 문형 ────────────────────────────────────────────────────────────
# 다단 레이아웃 대비: 공백을 전부 없앤 사본에서 매칭한다(build_precedence.py 와 같은 수법).
PATTERNS = [
    # "지침 제33조부터 제42조까지"  → 범위 참조
    ("범위", re.compile(r"(지침|요령|관리기준|기준|법|시행령|시행규칙)제(\d+)조(?:의(\d+))?부터제(\d+)조(?:의(\d+))?까지")),
    # "지침 제27조제3항"           → 단일 조 참조
    ("조",   re.compile(r"(지침|요령|관리기준|기준)제(\d+)조(?:의(\d+))?(?:제(\d+)항)?")),
    # "「국가연구개발혁신법」"        → 외부 규범 참조
    ("규범", re.compile(r"[「『]([^」』]{4,45})[」』]")),
    # "별표 2" / "붙임 3" / "별지 7"
    ("별표", re.compile(r"(별표|붙임|별지|참고)제?(\d+)")),
    # "이 지침에서 정하지 아니한 사항은 ~ 에 따름"  → 미규정 위임
    ("미규정위임", re.compile(r"정하지아니한사항")),
]

# 지침 조번호 → 조제목 (제14차 현행). shifted 판정의 기준표.
# 출처: RAG.md §3-3 실측표
지침_조제목 = {
    "제12차": {"33": "창업기업등 사업비 비목", "34": "재료비", "35": "외주용역비",
               "36": "기계장치", "42": "광고선전비"},
    "제13차": {"39": "창업기업등 사업비 비목", "40": "재료비", "41": "외주용역비",
               "42": "기계장치"},
    "제14차": {"36": "창업기업등 사업비 비목", "37": "재료비", "38": "외주용역비",
               "39": "기계장치, 공구·기구", "45": "광고선전비"},
}


# ── 문서별 약칭 사전 ────────────────────────────────────────────────────
# 🔴 2026-09-01 — 「법」 을 끊긴 참조로 버리던 것을 고쳤다.
#    법령 문서는 **자기 안에서 약칭을 정의한다**. 법제처 편집 관례라 예외가 거의 없다:
#        제1조(목적) 이 요령은 「중소기업창업 지원법」(이하 "법"이라 한다) 제12조에 따른…
#        제2조 … 「중소기업창업 지원법 시행령」(이하 "영"이라 한다) 제38조제3항…
#              … 「보조금 관리에 관한 법률」(이하 "보조금법"이라 한다) 제33조의2…
#    그래서 "어느 법인지 확정 불가" 가 아니다 — **그 문서를 읽으면 적혀 있다.**
#    이걸 안 걷으면 「중소기업창업 지원법」 제28~31조(사업비 관련 상위 근거)가
#    규정 모음에 멀쩡히 있는데도 참조가 끊겨 판정이 B등급으로 내려앉는다 (실측 1건).
#
#    ⚠️ `RE_약칭`(위)은 이 괄호를 **지우는** 정규식이다. 순서가 중요하다 —
#       약칭은 지우기 전에 걷어야 한다.
RE_약칭정의 = re.compile(
    r"[「『]([^」』\n]{4,60})[」』]\s*[」』]?\s*[(（]\s*이하\s*"
    r"['\"‘’“”]?([가-힣]{1,12})['\"‘’“”]?\s*(?:이라|라)\s*(?:한다|함)")

# 약칭으로 인정하지 않는 것 — 규범이 아니라 개념을 가리킨다.
# ("이하 '지원사업'이라 한다" 처럼 「」 없이 붙는 것은 정규식이 이미 거른다)
약칭_제외 = {"이하", "약칭", "이법", "동법"}


def 수집_약칭(전체본문: str) -> dict[str, str]:
    """문서가 스스로 정의한 약칭 → 정식 규범명."""
    표: dict[str, str] = {}
    for m in RE_약칭정의.finditer(re.sub(r"\s+", " ", 전체본문 or "")):
        정식, 약 = m.group(1).strip(), m.group(2).strip()
        if 약 in 약칭_제외 or len(약) < 1:
            continue
        # 먼저 나온 정의가 이긴다. 같은 약칭을 두 번 정의하는 문서는 없다시피 하고,
        # 있다면 앞의 것(대개 제1조 목적)이 그 문서의 기준이다.
        표.setdefault(약, 정식)
    return 표


def 경계_ok(t: str, imap: list[int], m) -> bool:
    """매치 앞이 한글이면 **긴 규범명의 꼬리를 잘라 먹은 것**이다.

    "부가가치세법 제10조" 에서 토큰 `법` 이 걸리면 「부가가치세법」이 아니라
    그 문서의 「법」(예: 중소기업창업 지원법)으로 해소돼 **엉뚱한 조문이 근거가 된다.**
    공백을 지운 사본에서는 앞뒤가 붙어 판별이 안 되므로 **원문 좌표(imap)로 되돌려** 본다.
    """
    i = imap[m.start()]
    return i == 0 or not ("가" <= t[i - 1] <= "힣")


def squash(t: str) -> tuple[str, list[int]]:
    buf, idx = [], []
    for i, ch in enumerate(t):
        if not ch.isspace():
            buf.append(ch)
            idx.append(i)
    return "".join(buf), idx


# 규범명 정규화. 세 가지를 걷어내야 규정 모음과 대조가 된다 (2026-08-30).
#   약칭 괄호  「중소기업창업 지원사업 운영요령(이하 "운영요령"이라 한다)」
#   파일명 구분자  L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223
#   판본 꼬리표    …_제14차개정_20251223 · (제2024-101호)(20241206)
# 이걸 안 하면 **규정 모음에 있는 규범도 끊긴 참조로 샌다.** 실측: 현행 9문서가 인용한
# 끊긴 참조 34종 중 4종이 실제로는 보유분이었다.
RE_약칭 = re.compile(r"\s*[(（]\s*이하[^)）]*[)）]\s*")
RE_판본꼬리 = re.compile(r"(_제\d+차[^_]*)?(_?\d{8})?$")


def norm_규범(s: str) -> str:
    s = RE_약칭.sub("", s or "")
    s = re.sub(r"^L\d_", "", s)                    # 파일명 레이어 접두
    s = RE_판본꼬리.sub("", s)
    s = re.sub(r"[(（][^)）]*[)）]", "", s)          # 남은 괄호 주석(호수·시행일)
    s = re.sub(r"[(（][^)）]*$", "", s)             # 닫히지 않은 꼬리 (조판으로 잘린 경우)
    # 공백·언더스코어·가운뎃점. 가운뎃점은 코드포인트가 갈린다 —
    # 인용은 `·`(U+00B7), 법령 제명은 `ㆍ`(U+318D) 다. 눈으로는 같아 보여서
    # 「대·중소기업 상생협력 촉진에 관한 법률」이 계속 끊긴 참조이었다.
    s = re.sub(r"[\s_·ㆍ‧․∙⋅•\-—]", "", s)
    return s.strip("「」『』‘’\"'.,")


def load_corpus_names() -> dict[str, str]:
    """규정 모음이 보유한 규범명 → **doc_id**. 해소 가능 여부의 판정 기준.

    🔴 2026-08-31 수정 — 값을 **파일 경로에서 `doc_id` 로 바꿨다.**

    초판은 `법령 PDF/L1_법령/법인세법시행령` 같은 경로를 돌려줬고 그게 그대로
    `refs.dst_doc_id` 에 들어갔다. `documents.doc_id` 는 `L1_법인세법시행령_20260227`
    이라 **조인이 안 된다.** 실측 결과 resolved 17,386건 중 8,668건(50%)이 이 상태였고,
    그래서 **refs 참조 확장이 `폐포전용` 문서에 도달하지 못했다** (도달 0건).
    `RAG.md` §4-2 의 `retrieval_scope` 재태깅이 이 참조 확장을 안전망으로 전제하므로 치명적이다.

    DB 를 읽어 실제 `doc_id` 를 쓴다. DB 가 없으면 경로로 되돌아가되 **경고한다** —
    그 산출물은 적재 후 `normalize_refs.py --apply` 를 반드시 태워야 한다.
    """
    names: dict[str, str] = {}
    try:
        with db.connect(connect_timeout=5) as conn:
            for (doc_id,) in conn.execute("SELECT doc_id FROM corpus.documents").fetchall():
                # doc_id 는 `L1_<제명>_<날짜>` 또는 제명 그대로다. 둘 다 제명으로 접어 등록한다.
                몸통 = doc_id[3:] if doc_id.startswith("L1_") else doc_id
                if len(몸통) > 9 and 몸통[-8:].isdigit() and 몸통[-9] == "_":
                    몸통 = 몸통[:-9]
                names.setdefault(norm_규범(몸통), doc_id)
                names.setdefault(norm_규범(doc_id), doc_id)
        return names
    except Exception as e:
        print(f"⚠️ documents 를 못 읽어 파일 경로로 대체한다 ({type(e).__name__}). "
              f"적재 후 반드시 `python scripts/normalize_refs.py --apply` 를 돌릴 것.")

    src = ROOT / "법령 PDF" / "_law_sources.json"
    if src.exists():
        for k in json.loads(src.read_text(encoding="utf-8")):
            names[norm_규범(k)] = f"법령 PDF/L1_법령/{k}"
    for f in (ROOT / "2026_Finance_DATA_FOR_RAG").rglob("*.pdf"):
        names.setdefault(norm_규범(f.stem), str(f.relative_to(ROOT)))
    return names


# 인용 표기 -> 규정 모음의 실제 제명 (2026-08-30 실측).
# 🔴 이게 없으면 **가진 법도 참조가 안 이어진다.** 세부관리기준이 「근로자직업능력개발법」
#    이라고 쓸 때 우리는 「국민 평생 직업능력 개발법」으로 갖고 있어서, 참조 확장이
#    거기서 끊긴다 — RAG 가 그 법을 못 가져온다는 뜻이다.
#    수집 단계(`fetch_missing_norms.py`)와 같은 실측표다.
# 통합관리지침 제14차의 실제 doc_id. 약칭("L1_통합관리지침_제14차")을 넣으면
# documents 와 조인이 안 된다 — 위 load_corpus_names 주석 참조.
지침_DOCID = "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"

별칭 = {
    # 제명 개정 — 세부관리기준이 구 제명으로 인용한다
    "근로자직업능력개발법": "국민평생직업능력개발법",
    # 약칭 인용 — 정식 제명이 훨씬 길다
    "외부감사법시행령": "주식회사등의외부감사에관한법률시행령",
    "외부감사법": "주식회사등의외부감사에관한법률",
    # 원문 오기 — 그런 이름의 규범은 없다
    "중소기업기술개발지원사업관리지침": "중소기업기술개발지원사업운영요령",
    "중소기업지원사업운영요령": "중소기업창업지원사업운영요령",
    # 조판 사고 — 다른 컬럼 글자가 끼어들었다
    "공공기관의운영에관한비율법률": "공공기관의운영에관한법률",
}


def _계열(s: str) -> str:
    """법 / 시행령 / 시행규칙 중 무엇인가. 부분일치가 계열을 넘나드는 걸 막는 열쇠다.

    🔴 2026-09-01 — 「법인세법」인용 425건이 **「법인세법시행령」에 붙어 있었다.**
       본법이 규정 모음에 없어서(시행령·시행규칙만 수집됨) 부분일치가 길이차 3으로
       시행령을 골랐다. 없는 걸 없다고 해야 수집 결손이 드러난다 — 조용히 옆 문서로
       대체하면 **엉뚱한 조문이 근거로 인용되고**, 결손은 영원히 안 보인다.
    """
    for 접미 in ("시행규칙", "시행령", "시행규정"):
        if s.endswith(접미):
            return 접미
    return "법"


def resolve_규범(name: str, corpus: dict[str, str]) -> tuple[str, str | None]:
    """규범명이 규정 모음에 있나. (해소상태, 경로)"""
    key = norm_규범(name)
    if not key or len(key) < 4:
        return "dangling", None
    key = 별칭.get(key, key)
    if key in corpus:
        return "resolved", corpus[key]
    # 부분 일치 (「…법」 vs 「…법률」, 파일명에 판본이 더 붙은 경우 등).
    # 짧은 쪽이 긴 쪽에 통째로 들어가야 한다 — 길이 차 상한은 두되,
    # 파일명 쪽이 길어지는 경우가 많아 비대칭으로 잡는다.
    best = None
    for k, v in corpus.items():
        if _계열(k) != _계열(key):
            continue                      # 「법인세법」이 「법인세법시행령」에 붙는 걸 막는다
        if key in k or k in key:
            gap = abs(len(k) - len(key))
            if gap <= 12 and (best is None or gap < best[0]):
                best = (gap, v)
    return ("resolved", best[1]) if best else ("dangling", None)


# 기본 문형이 이미 잡는 토큰. 여기 있는 것은 동적 패턴에서 빼야 엣지가 두 번 안 생긴다.
기본토큰 = {"지침", "요령", "관리기준", "기준", "법", "시행령", "시행규칙"}
# 앞이 한글이면 잘라먹은 것으로 보고 버릴 토큰 (`경계_ok`). 「부가가치세법」→「법」 사고 방지.
경계검사_토큰 = {"법", "시행령", "시행규칙", "영"}


def _동적패턴(약칭표: dict[str, str]):
    """문서가 스스로 정의한 약칭만으로 문형을 만든다. 정의가 없으면 아무것도 안 잡는다.

    🔴 범위와 조의 토큰 집합이 **일부러 다르다.**
      · 범위(`…부터 …까지`) 는 기본 문형이 이미 `법|시행령|시행규칙` 을 잡는다 → 빼야 중복이 없다
      · 조(`법 제51조제6항`) 는 기본 문형이 `지침|요령|관리기준|기준` 만 잡는다 →
        `법|시행령|시행규칙` 을 **여기서 처음 잡는다.** 종전에는 이 참조가 통째로 유실됐다
        (실측 26,600건 · 그중 22,848건이 문서 자체 정의로 해소 가능).
    """
    범위토큰 = sorted((k for k in 약칭표 if k not in 기본토큰), key=len, reverse=True)
    조토큰 = sorted((k for k in 약칭표 if k not in ("지침", "요령", "관리기준", "기준")),
                   key=len, reverse=True)
    out = []
    if 범위토큰:
        alt = "|".join(re.escape(k) for k in 범위토큰)
        out.append(("약칭범위",
                    re.compile(rf"({alt})제(\d+)조(?:의(\d+))?부터제(\d+)조(?:의(\d+))?까지")))
    if 조토큰:
        alt = "|".join(re.escape(k) for k in 조토큰)
        out.append(("약칭조", re.compile(rf"({alt})제(\d+)조(?:의(\d+))?(?:제(\d+)항)?")))
    return out or None


def _약칭해소(e: dict, 토큰: str, 약칭표: dict[str, str], corpus: dict[str, str]) -> None:
    """약칭 → 정식명 → 규정 모음. 못 닿으면 **왜 못 닿았는지**를 남기고 끊긴 참조 유지."""
    정식 = (약칭표 or {}).get(토큰)
    if not 정식:
        e.update(해소상태="dangling", 보정근거="규범명이 앞에서 잘림 — 문맥으로 확정 필요")
        return
    st, path = resolve_규범(정식, corpus)
    if st == "resolved":
        e.update(해소상태="resolved", dst_doc_id=path,
                 보정근거=f'문서 자체 정의 「{정식}」(이하 "{토큰}") 로 해소')
    else:
        # 정식명은 알아냈는데 규정 모음에 그 규범이 없다. 이건 진짜 결손이고,
        # 위의 "문맥으로 확정 필요" 와 섞으면 수집 대상 목록이 오염된다.
        e.update(해소상태="dangling",
                 보정근거=f'약칭 "{토큰}" → 「{정식}」 — 코퍼스에 없는 규범')


def scan(text: str, doc_id: str, corpus: dict[str, str],
         src_조번호: str = "문서전체", 약칭표: dict[str, str] | None = None,
         동적: list | None = None) -> list[dict]:
    t = re.sub(r"\s+", " ", text)
    sq, imap = squash(t)
    edges: list[dict] = []
    seen: set[tuple] = set()

    def 원문(m) -> str:
        a, b = imap[m.start()], imap[m.end() - 1] + 1
        return re.sub(r"\s+", " ", t[a:b])

    # 🔴 범위 문형을 **먼저** 태운다. "법 제28조부터 제31조까지" 는 조 문형에도
    #    "법 제28조" 로 걸려서, 순서를 안 잡으면 같은 참조가 엣지 두 개가 된다
    #    (dst 는 같은 제28조다 — 정보가 아니라 중복이다).
    문형 = PATTERNS + (동적 or [])
    문형 = [x for x in 문형 if x[0].endswith("범위")] + [x for x in 문형 if not x[0].endswith("범위")]
    범위span: list[tuple[int, int]] = []

    for kind, pat in 문형:
        for m in pat.finditer(sq):
            # 약칭 토큰은 앞이 한글이면 긴 규범명의 꼬리다 — 버린다 (`경계_ok` 주석 참조)
            if kind.startswith("약칭") or (kind in ("범위", "조") and m.group(1) in 경계검사_토큰):
                if not 경계_ok(t, imap, m):
                    continue
            if kind.endswith("범위"):
                범위span.append((m.start(), m.end()))
            elif kind.endswith("조") and any(a <= m.start() < b for a, b in 범위span):
                continue                      # 범위 참조가 이미 먹은 자리
            raw = 원문(m)
            key = (kind, raw)
            if key in seen:
                continue
            seen.add(key)

            e = {"src_doc_id": doc_id, "src_조번호": src_조번호, "참조문자열": raw, "관계": None,
                 "dst_doc_id": None, "dst_조번호": None,
                 "해소상태": "dangling", "보정근거": None}

            if kind == "규범":
                st, path = resolve_규범(m.group(1), corpus)
                # 「보조금법」처럼 **약칭을 「」 안에 넣어** 쓰는 문서가 있다. 정식 제명으로는
                # 규정 모음에 없지만 그 문서가 스스로 정의해 뒀다 — 한 번 더 물어본다.
                if st == "dangling" and (약칭표 or {}).get(m.group(1).strip()):
                    정식 = 약칭표[m.group(1).strip()]
                    st2, path2 = resolve_규범(정식, corpus)
                    if st2 == "resolved":
                        st, path = st2, path2
                        e["보정근거"] = f'문서 자체 정의 「{정식}」(이하 "{m.group(1).strip()}") 로 해소'
                e.update(관계="인용", dst_doc_id=path, 해소상태=st)
            elif kind == "별표":
                e.update(관계="별표참조", dst_doc_id=doc_id,
                         dst_조번호=f"{m.group(1)}{m.group(2)}", 해소상태="resolved")
            elif kind == "미규정위임":
                # 🔴 `resolved` 가 아니다 (2026-09-01 정정). "정하지 아니한 사항은 상위
                #    규범에 따른다" 는 **어느 조인지 지목하지 않는다** — dst 가 비어 있다.
                #    resolved 는 "대상이 규정 모음에 있고 조번호도 맞다" 는 뜻이므로 거짓말이었고,
                #    실측 10건이 dst NULL 인 채 resolved 로 세어지고 있었다.
                #    참조 확장 CTE 는 `dst_조번호 IS NOT NULL` 로 어차피 걸러내므로 판정 영향은 없다.
                #    다만 업로드 시점 끊긴 참조 안내(CLAUDE.md)에서 이건 결손이 아니라
                #    법령의 정상 문형이다 — 소비자는 `관계='미규정위임'` 으로 걸러 쓴다.
                e.update(관계="미규정위임", 해소상태="dangling",
                         보정근거="상위 규범으로 위임(조 미지정). 게이팅(파이프라인 §6.2)의 근거")
            else:  # 조 / 범위 / 약칭조 / 약칭범위
                target = m.group(1)
                조 = m.group(2)
                e.update(관계="준용" if target in ("지침", "요령") else "인용",
                         dst_조번호=f"제{조}조")
                if kind.startswith("약칭"):
                    _약칭해소(e, target, 약칭표, corpus)
                    edges.append(e)
                    continue
                # 지침 참조는 판본 어긋남을 검사한다 (파이프라인 §2.5)
                if target == "지침":
                    for 판, tbl in 지침_조제목.items():
                        if 판 != "제14차" and 조 in tbl:
                            현행 = [k for k, v in 지침_조제목["제14차"].items() if v == tbl[조]]
                            if 현행:
                                e.update(해소상태="shifted",
                                         # 약칭이 아니라 실제 doc_id 를 쓴다 (위 주석)
                                         dst_doc_id=지침_DOCID,
                                         dst_조번호=f"제{현행[0]}조",
                                         보정근거=f"{판} 조번호로 표기됨 → 조제목 '{tbl[조]}' 로 재매칭")
                                break
                    else:
                        e.update(해소상태="resolved", dst_doc_id=지침_DOCID)
                elif target == "요령":
                    # 「중소기업창업 지원사업 운영요령」(중기부고시 2024-101). 규정 모음에 있다.
                    e.update(해소상태="resolved",
                             dst_doc_id="L1_중소기업창업지원사업운영요령_20241206")
                elif target in ("관리기준", "기준"):
                    # 자기 문서 내부 참조 ("본 관리기준 제N조")
                    e.update(관계="인용", 해소상태="resolved", dst_doc_id=doc_id,
                             보정근거="자기 문서 내부 참조")
                elif target in ("법", "시행령", "시행규칙"):
                    # 🔴 2026-09-01 — "어느 법인지 확정 불가" 를 걷었다.
                    #    그 문서의 제1·2조가 「…」(이하 "법"이라 한다) 로 **직접 정의**한다.
                    #    정의가 없는 문서에서만 종전대로 끊긴 참조이다.
                    _약칭해소(e, target, 약칭표, corpus)
            edges.append(e)
    return edges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="")
    ap.add_argument("--show", default="")
    args = ap.parse_args()

    corpus = load_corpus_names()
    print(f"코퍼스 규범명 {len(corpus)}건 로드")
    for must in ("중소기업창업지원사업운영요령", "중소기업창업지원법"):
        if must not in corpus:
            print(f"  ! 경고: 코퍼스에 '{must}' 가 없다 — 해소가 dangling 으로 샌다")

    # 🔴 2026-08-30 — 입력을 PDF 에서 **Stage 0 산출물**로 바꿨다.
    #    이전에는 여기서 PDF 를 다시 파싱했다. 세 가지가 문제였다:
    #      1. 법령 XML 219건이 대상에서 통째로 빠졌다 (`*.pdf` 만 훑었다).
    #         법령 간 상호참조가 없으면 참조 확장이 L2 안에서 끝난다 — RAG 의 존재 이유가 반토막
    #      2. Stage 0 이 이미 판 것을 다시 판다. 6분이 통째로 중복이다
    #      3. 두 곳의 파싱 결과가 어긋날 수 있다. 실제로 `max_pages=60` 때문에
    #         긴 문서는 뒷부분 참조가 수집되지 않았다
    #    `데이터 전처리 파이프라인.md` §2 가 "0.7 은 Stage 0 산출물만 읽는다" 로 정한 그대로다.
    S0 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_stage0_articles.json"
    if not S0.exists():
        sys.exit(f"Stage 0 산출물이 없다: {S0}\n  먼저 `python scripts/stage0_run.py` 를 돌릴 것")
    stage0 = json.loads(S0.read_text(encoding="utf-8"))

    all_edges, per_doc, skipped, deduped = [], {}, [], []
    strat_count = Counter()
    약칭_문서수, 약칭_누적 = 0, Counter()
    items = [(k, v) for k, v in stage0.items() if not args.src or args.src in k]
    total = len(items)
    for n, (stem, d) in enumerate(items, 1):
        arts = d.get("articles") or []
        strategy = d.get("strategy") or "?"
        if not arts:
            skipped.append("%s (조 0개)" % stem[:50])
            continue
        # 문서 전체가 아니라 **조 단위**로 스캔한다. src_조번호 가 비면
        # RAG.md §4-3 참조 확장 SQL 의 `(src_doc_id, src_조번호)` 조인이 성립하지 않는다.
        # 폐지 조문은 참조 원천이 아니다 — 효력이 없으므로 따라가면 안 된다.
        # 범위 밖(모두의창업 제3편 로컬트랙)은 참조도 수집하지 않는다.
        # 위임 계통이 다르므로(상위가 신사업창업사관학교 운영지침) 이 조들의 참조를 참조 확장에
        # 남겨두면 일반·기술트랙 판정에서 범위 밖 규범이 딸려온다. 2026-08-31 추가.
        밖 = 범위밖_조(stem, arts)
        # 🔴 약칭표는 **문서 전체**에서 한 번 걷는다. 정의는 제1·2조에 있고 쓰이는 곳은
        #    제20조·제51조다 — 조 단위로 스캔하면서 조마다 걷으면 영원히 못 만난다.
        #    삭제·범위밖 조도 정의 원천으로는 읽는다 (정의가 거기 있을 수 있고,
        #    여기서 만드는 건 엣지가 아니라 사전이다).
        약칭표 = 수집_약칭(" ".join(a.get("본문") or "" for a in arts))
        동적 = _동적패턴(약칭표)
        if 약칭표:
            약칭_문서수 += 1
            약칭_누적.update(약칭표.keys())
        edges = []
        for a in arts:
            if a.get("삭제") or a["조번호"] in 밖:
                continue
            edges += scan(a["본문"], stem, corpus, src_조번호=a["조번호"],
                          약칭표=약칭표, 동적=동적)
        # 레이어를 엣지에 붙인다. 끊긴 참조 비율을 레이어별로 봐야 신호가 산다 —
        # L1 법령끼리의 인용은 규정 모음 경계(219 규범) 밖으로 나가면 당연히 끊긴 참조이고
        # 그게 전체의 40%다. 뭉뚱그리면 L2 의 진짜 해소 실패가 묻힌다.
        lay = d.get("layer")
        for e in edges:
            e["src_layer"] = lay
        all_edges += edges
        per_doc[stem] = len(edges)
        strat_count[strategy] += 1
        if n % 25 == 0 or n == total:
            print("  [%3d/%d] 누적 %d엣지" % (n, total, len(all_edges)), flush=True)

    st = Counter(e["해소상태"] for e in all_edges)
    rel = Counter(e["관계"] for e in all_edges)
    doc = {
        "생성": "scripts/build_refs.py",
        "기준일": "2026-08-27",
        "문서수": len(per_doc),
        "엣지수": len(all_edges),
        "해소상태": dict(st),
        "해소상태_레이어별": {
            lay: dict(Counter(e["해소상태"] for e in all_edges if e.get("src_layer") == lay))
            for lay in sorted({e.get("src_layer") for e in all_edges} - {None})
        },
        "관계": dict(rel),
        "주의": ("dangling 은 '코퍼스에 없다'는 뜻이고, 그 자체가 판정 불가 신호다. "
                 "업로드 시점 A1 화면에 노출해 사용자가 해당 문서를 올리게 유도한다."),
        "edges": all_edges,
        "건너뜀": skipped,
        "문자중복_해소": deduped,
        "조분해_전략": dict(strat_count),
        "src_조번호_채움": sum(1 for e in all_edges if e.get("src_조번호") != "문서전체"),
        "약칭": {
            "정의를_가진_문서": 약칭_문서수,
            "약칭_종류": dict(약칭_누적.most_common(20)),
            "약칭으로_해소": sum(1 for e in all_edges
                              if (e.get("보정근거") or "").startswith("문서 자체 정의")),
            "약칭은_풀었으나_규범없음": sum(1 for e in all_edges
                                     if (e.get("보정근거") or "").endswith("코퍼스에 없는 규범")),
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n문서 {len(per_doc)}건 → 엣지 {len(all_edges)}개  → {OUT.relative_to(ROOT)}")
    print("  해소상태:", dict(st))
    print("  관계    :", dict(rel))
    print(f"  약칭    : 정의 보유 문서 {약칭_문서수}건 · "
          f"약칭으로 해소 {doc['약칭']['약칭으로_해소']}건 · "
          f"규범없음 {doc['약칭']['약칭은_풀었으나_규범없음']}건")
    print(f"            {list(약칭_누적.most_common(10))}")
    top = sorted(per_doc.items(), key=lambda x: -x[1])[:8]
    print("\n  엣지 많은 문서:")
    for k, v in top:
        print(f"    {v:5d}  {k[:62]}")

    if args.show:
        print(f"\n=== {args.show} 샘플 ===")
        for e in all_edges:
            if args.show in e["src_doc_id"]:
                print(f"  [{e['해소상태']:8s}] {e['관계']:6s} {e['참조문자열'][:58]}"
                      + (f"  → {e['dst_조번호']}" if e["dst_조번호"] else "")
                      + (f"  ({e['보정근거']})" if e["보정근거"] else ""))


if __name__ == "__main__":
    main()
