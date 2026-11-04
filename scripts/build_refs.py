# -*- coding: utf-8 -*-
"""코퍼스 조문 사이의 참조(인용·준용·별표·위임)를 뽑아 `_refs.json` 을 만든다(오프라인 적재용).

입력은 2026_Finance_DATA_FOR_RAG/_articles.json(조문 분할 결과), 출력은 corpus.refs
(db/init/01_schema.sql)에 적재할 엣지 목록이다. 해결 상태: resolved(대상 조가 코퍼스에 있음),
remapped(구판 조번호를 조제목으로 재매칭), dangling(대상이 코퍼스에 없음). 지침_조제목 은
지침 개정판별 조번호 기준표다. `dst_doc_id` 는 DB 를 못 읽으면 파일 경로로 남으므로 적재 전에
corpus.documents.doc_id 로 맞춰야 한다.

실행: python scripts/build_refs.py [--doc-filter 창진원] [--show 예비창업]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

from _lib import db
from scope import 범위밖_조                   # 모두의창업 제3편 로컬트랙은 범위 밖

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "2026_Finance_DATA_FOR_RAG" / "_refs.json"

# ── 참조 패턴 ────────────────────────────────────────────────────────────
# 다단 레이아웃 대비: 공백을 전부 없앤 사본에서 매칭한다.
PATTERNS = [
    # "지침 제33조부터 제42조까지"  → 범위 참조
    ("범위", re.compile(r"(지침|요령|관리기준|기준|법|시행령|시행규칙)제(\d+)조(?:의(\d+))?부터제(\d+)조(?:의(\d+))?까지")),
    # "지침 제27조제3항"           → 단일 조 참조
    ("조",   re.compile(r"(지침|요령|관리기준|기준)제(\d+)조(?:의(\d+))?(?:제(\d+)항)?")),
    # "「국가연구개발혁신법」"        → 외부 규정 참조
    ("규정", re.compile(r"[「『]([^」』]{4,45})[」』]")),
    # "별표 2" / "붙임 3" / "별지 7"
    ("별표", re.compile(r"(별표|붙임|별지|참고)제?(\d+)")),
    # "이 지침에서 정하지 아니한 사항은 ~ 에 따름"  → 미규정 위임
    ("미규정위임", re.compile(r"정하지아니한사항")),
]

# 지침 개정판별 조번호 → 조제목 (제14차가 현행). remapped 판정의 기준표.
지침_조제목 = {
    "제12차": {"33": "창업기업등 사업비 비목", "34": "재료비", "35": "외주용역비",
               "36": "기계장치", "42": "광고선전비"},
    "제13차": {"39": "창업기업등 사업비 비목", "40": "여비", "41": "외주용역비",
               "42": "기계장치"},
    "제14차": {"36": "창업기업등 사업비 비목", "37": "재료비", "38": "외주용역비",
               "39": "기계장치, 공구·기구", "43": "여비", "45": "광고선전비"},
}
# 14차 조번호로 쓴 문서. 「지침 제40조」가 14차 제40조(특허권 등 무형자산 취득비)라 옮기지 않는다.
지침_제40조_14차표기_문서 = {"초격차 스타트업 프로젝트 세부관리기준(제10차)"}


# ── 문서별 약칭 사전 ────────────────────────────────────────────────────
# 법령 문서는 자기 안에서 약칭을 정의한다. 법제처 편집 관례라 예외가 거의 없다:
#     제1조(목적) 이 요령은 「중소기업창업 지원법」(이하 "법"이라 한다) 제12조에 따른…
#     제2조 … 「중소기업창업 지원법 시행령」(이하 "영"이라 한다) 제38조제3항…
# 이 정의를 모아 약칭을 정식 규정명으로 해결한다.
#
# `RE_약칭`(아래)은 이 괄호를 지우는 정규식이다 — 약칭은 지우기 전에 모아야 한다.
RE_약칭정의 = re.compile(
    r"[「『]([^」』\n]{4,60})[」』]\s*[」』]?\s*[(（]\s*이하\s*"
    r"['\"‘’“”]?([가-힣]{1,12})['\"‘’“”]?\s*(?:이라|라)\s*(?:한다|함)")

# 약칭으로 인정하지 않는 것 — 규정이 아니라 개념을 가리킨다.
# ("이하 '지원사업'이라 한다" 처럼 「」 없이 붙는 것은 정규식이 이미 거른다)
약칭_제외 = {"이하", "약칭", "이법", "동법"}


def 수집_약칭(전체본문: str) -> dict[str, str]:
    """문서가 스스로 정의한 약칭 → 정식 규정명."""
    표: dict[str, str] = {}
    for m in RE_약칭정의.finditer(re.sub(r"\s+", " ", 전체본문 or "")):
        정식, 약 = m.group(1).strip(), m.group(2).strip()
        if 약 in 약칭_제외 or len(약) < 1:
            continue
        # 먼저 나온 정의가 우선한다.
        표.setdefault(약, 정식)
    return 표


def _단어경계_확인(t: str, imap: list[int], m) -> bool:
    """매치 앞이 한글이면 긴 규정명의 끝부분만 매칭한 것이므로 버린다.

    공백을 지운 사본에서는 앞뒤가 붙어 판별이 안 되므로 원문 위치(imap)로 되돌려 본다.
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


# 규정명 정규화. 대조를 위해 약칭 괄호·파일명 접두어·개정차수 접미사를 제거한다.
#   약칭 괄호  「중소기업창업 지원사업 운영요령(이하 "운영요령"이라 한다)」
#   파일명 구분자  L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223
#   개정차수 접미사 …_제14차개정_20251223 · (제2024-101호)(20241206)
RE_약칭 = re.compile(r"\s*[(（]\s*이하[^)）]*[)）]\s*")
RE_개정차수_접미 = re.compile(r"(_제\d+차[^_]*)?(_?\d{8})?$")


def norm_규정명(s: str) -> str:
    s = RE_약칭.sub("", s or "")
    s = re.sub(r"^L\d_", "", s)                    # 파일명 계층 접두어
    s = RE_개정차수_접미.sub("", s)
    s = re.sub(r"[(（][^)）]*[)）]", "", s)          # 남은 괄호 주석(호수·시행일)
    s = re.sub(r"[(（][^)）]*$", "", s)             # 닫히지 않은 괄호 (조판으로 잘린 경우)
    # 공백·언더스코어·가운뎃점. 가운뎃점은 코드포인트가 여러 가지다 —
    # 인용은 `·`(U+00B7), 법령 제명은 `ㆍ`(U+318D) 다. 눈으로는 같아 보인다.
    s = re.sub(r"[\s_·ㆍ‧․∙⋅•\-—]", "", s)
    return s.strip("「」『』‘’\"'.,")


def load_corpus_names() -> dict[str, str]:
    """코퍼스가 보유한 규정명 → doc_id. 해결 가능 여부의 판정 기준.

    값은 파일 경로가 아니라 doc_id 여야 한다 — 파일 경로를 넣으면 `documents.doc_id`
    와 조인이 안 돼 참조 확장이 이어지지 않는다. DB 를 읽어 실제 `doc_id` 를 쓴다. DB 가 없으면
    파일 경로로 폴백하고 경고한다 — 그 산출물은 적재 전에 doc_id 로 맞춰야 한다.
    """
    names: dict[str, str] = {}
    try:
        with db.connect(connect_timeout=5) as conn:
            for (doc_id,) in conn.execute("SELECT doc_id FROM corpus.documents").fetchall():
                # doc_id 는 `L1_<제명>_<날짜>` 또는 제명 그대로다. 둘 다 제명으로 변환해 등록한다.
                제명 = doc_id[3:] if doc_id.startswith("L1_") else doc_id
                if len(제명) > 9 and 제명[-8:].isdigit() and 제명[-9] == "_":
                    제명 = 제명[:-9]
                names.setdefault(norm_규정명(제명), doc_id)
                names.setdefault(norm_규정명(doc_id), doc_id)
        return names
    except Exception as e:
        print(f"경고: documents 를 못 읽어 파일 경로로 폴백한다 ({type(e).__name__}). "
              f"적재 전에 dst_doc_id 를 corpus.documents.doc_id 로 맞춰야 한다.")

    src = ROOT / "법령 PDF" / "_law_sources.json"
    if src.exists():
        for k in json.loads(src.read_text(encoding="utf-8")):
            names[norm_규정명(k)] = f"법령 PDF/L1_법령/{k}"
    for f in (ROOT / "2026_Finance_DATA_FOR_RAG").rglob("*.pdf"):
        names.setdefault(norm_규정명(f.stem), str(f.relative_to(ROOT)))
    return names


# 인용 표기 -> 코퍼스의 실제 제명.
# 세부관리기준이 구 제명으로 인용하고 코퍼스는 정식 제명으로 갖고 있으면, 이 표가 없을 때
# 코퍼스에 있는 법령인데도 참조가 해결되지 않아 참조 확장이 거기서 멈춘다.
# 통합관리지침 제14차의 실제 doc_id. 약칭("L1_통합관리지침_제14차")을 넣으면
# documents 와 조인이 안 된다 — 위 load_corpus_names 주석 참조.
지침_DOCID = "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223"

별칭 = {
    # 제명 개정 — 세부관리기준이 구 제명으로 인용한다
    "근로자직업능력개발법": "국민평생직업능력개발법",
    # 약칭 인용 — 정식 제명이 훨씬 길다
    "외부감사법시행령": "주식회사등의외부감사에관한법률시행령",
    "외부감사법": "주식회사등의외부감사에관한법률",
    # 원문 오기 — 그런 이름의 규정은 없다
    "중소기업기술개발지원사업관리지침": "중소기업기술개발지원사업운영요령",
    "중소기업지원사업운영요령": "중소기업창업지원사업운영요령",
    # 조판 오류 — 다른 컬럼 글자가 끼어들었다
    "공공기관의운영에관한비율법률": "공공기관의운영에관한법률",
}


def _법령종류(s: str) -> str:
    """법 / 시행령 / 시행규칙 중 무엇인가. 부분일치가 법령 종류를 넘나들지 않게 비교 기준으로 쓴다.

    본법이 수집되지 않았다고 시행령·시행규칙으로 경고 없이 대체하지 않는다 —
    그러면 엉뚱한 조문이 근거로 인용되고 수집 누락이 드러나지 않는다.
    """
    for 접미 in ("시행규칙", "시행령", "시행규정"):
        if s.endswith(접미):
            return 접미
    return "법"


def resolve_규정명(name: str, corpus: dict[str, str]) -> tuple[str, str | None]:
    """규정명이 코퍼스에 있나. (해결상태, 경로)"""
    key = norm_규정명(name)
    if not key or len(key) < 4:
        return "dangling", None
    key = 별칭.get(key, key)
    if key in corpus:
        return "resolved", corpus[key]
    # 부분 일치 (「…법」 vs 「…법률」, 파일명에 판본이 더 붙은 경우 등).
    # 짧은 쪽이 긴 쪽에 전부 포함되어야 한다 — 길이 차 상한은 두되,
    # 파일명 쪽이 길어지는 경우가 많아 비대칭으로 잡는다.
    best = None
    for k, v in corpus.items():
        if _법령종류(k) != _법령종류(key):
            continue                      # 「법인세법」이 「법인세법시행령」에 붙는 걸 막는다
        if key in k or k in key:
            gap = abs(len(k) - len(key))
            if gap <= 12 and (best is None or gap < best[0]):
                best = (gap, v)
    return ("resolved", best[1]) if best else ("dangling", None)


# 기본 참조패턴이 이미 잡는 토큰. 여기 있는 것은 동적 패턴에서 빼야 엣지가 두 번 안 생긴다.
기본토큰 = {"지침", "요령", "관리기준", "기준", "법", "시행령", "시행규칙"}
# 앞이 한글이면 긴 규정명의 끝부분으로 보고 버릴 토큰 (`_단어경계_확인`). 「부가가치세법」을 「법」으로 오매칭하지 않게 한다.
경계검사_토큰 = {"법", "시행령", "시행규칙", "영"}


def _동적패턴(약칭표: dict[str, str]):
    """문서가 스스로 정의한 약칭만으로 참조패턴을 만든다. 정의가 없으면 아무것도 안 잡는다.

    범위와 조의 토큰 집합이 일부러 다르다.
      · 범위(`…부터 …까지`) 는 기본 참조패턴이 이미 `법|시행령|시행규칙` 을 잡는다 → 빼야 중복이 없다
      · 조(`법 제51조제6항`) 는 기본 참조패턴이 `지침|요령|관리기준|기준` 만 잡는다 →
        `법|시행령|시행규칙` 을 여기서 처음 잡는다.
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


def _약칭_해결(e: dict, 토큰: str, 약칭표: dict[str, str], corpus: dict[str, str]) -> None:
    """약칭 → 정식명 → 코퍼스. 해결하지 못하면 그 이유를 남기고 미해결 참조로 둔다."""
    정식 = (약칭표 or {}).get(토큰)
    if not 정식:
        e.update(해결상태="dangling", 해결근거="규정명이 앞에서 잘림 — 문맥으로 확정 필요")
        return
    st, path = resolve_규정명(정식, corpus)
    if st == "resolved":
        e.update(해결상태="resolved", dst_doc_id=path,
                 해결근거=f'문서 자체 정의 「{정식}」(이하 "{토큰}") 로 해결')
    else:
        # 정식명은 알아냈는데 코퍼스에 그 규정이 없다. 실제 누락이고,
        # 위의 '문맥으로 확정 필요' 와 섞으면 수집 대상 목록이 오염된다.
        e.update(해결상태="dangling",
                 해결근거=f'약칭 "{토큰}" → 「{정식}」 — 코퍼스에 없는 규정')


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

    # 범위 패턴을 먼저 매칭한다. "법 제28조부터 제31조까지" 는 조 패턴에도
    # "법 제28조" 로 걸려서, 순서를 안 잡으면 같은 참조가 엣지 두 개가 된다.
    참조패턴 = PATTERNS + (동적 or [])
    참조패턴 = [x for x in 참조패턴 if x[0].endswith("범위")] + [x for x in 참조패턴 if not x[0].endswith("범위")]
    범위_구간: list[tuple[int, int]] = []

    for kind, pat in 참조패턴:
        for m in pat.finditer(sq):
            # 약칭 토큰은 앞이 한글이면 긴 규정명의 끝부분이다 — 버린다 (`_단어경계_확인` 주석 참조)
            if kind.startswith("약칭") or (kind in ("범위", "조") and m.group(1) in 경계검사_토큰):
                if not _단어경계_확인(t, imap, m):
                    continue
            if kind.endswith("범위"):
                범위_구간.append((m.start(), m.end()))
            elif kind.endswith("조") and any(a <= m.start() < b for a, b in 범위_구간):
                continue                      # 범위 참조가 이미 차지한 자리
            raw = 원문(m)
            key = (kind, raw)
            if key in seen:
                continue
            seen.add(key)

            e = {"src_doc_id": doc_id, "src_조번호": src_조번호, "참조문자열": raw, "관계": None,
                 "dst_doc_id": None, "dst_조번호": None,
                 "해결상태": "dangling", "해결근거": None}

            if kind == "규정":
                st, path = resolve_규정명(m.group(1), corpus)
                # 약칭을 「」 안에 넣어 쓰는 문서가 있다. 정식 제명으로는 코퍼스에 없지만
                # 그 문서가 스스로 정의해 뒀다 — 약칭표로 한 번 더 해결한다.
                if st == "dangling" and (약칭표 or {}).get(m.group(1).strip()):
                    정식 = 약칭표[m.group(1).strip()]
                    st2, path2 = resolve_규정명(정식, corpus)
                    if st2 == "resolved":
                        st, path = st2, path2
                        e["해결근거"] = f'문서 자체 정의 「{정식}」(이하 "{m.group(1).strip()}") 로 해결'
                e.update(관계="인용", dst_doc_id=path, 해결상태=st)
            elif kind == "별표":
                e.update(관계="별표참조", dst_doc_id=doc_id,
                         dst_조번호=f"{m.group(1)}{m.group(2)}", 해결상태="resolved")
            elif kind == "미규정위임":
                # `resolved` 가 아니다 — "정하지 아니한 사항은 상위 규정에 따른다" 는
                # 어느 조인지 지목하지 않아 dst 가 비어 있다. 누락이 아니라
                # 법령의 정상 문장 형식이다 — 사용하는 쪽은 `관계='미규정위임'` 으로 걸러 쓴다.
                e.update(관계="미규정위임", 해결상태="dangling",
                         해결근거="상위 규정으로 위임(조 미지정). L3 상위 규정 확인의 근거")
            else:  # 조 / 범위 / 약칭조 / 약칭범위
                target = m.group(1)
                조 = m.group(2)
                e.update(관계="준용" if target in ("지침", "요령") else "인용",
                         dst_조번호=f"제{조}조")
                if kind.startswith("약칭"):
                    _약칭_해결(e, target, 약칭표, corpus)
                    edges.append(e)
                    continue
                # 지침 참조는 판본 어긋남을 검사한다.
                if target == "지침":
                    for 판, tbl in 지침_조제목.items():
                        if 조 == "40" and doc_id in 지침_제40조_14차표기_문서:
                            continue
                        if 판 != "제14차" and 조 in tbl:
                            현행 = [k for k, v in 지침_조제목["제14차"].items() if v == tbl[조]]
                            if 현행:
                                e.update(해결상태="remapped",
                                         # 약칭이 아니라 실제 doc_id 를 쓴다 (위 주석)
                                         dst_doc_id=지침_DOCID,
                                         dst_조번호=f"제{현행[0]}조",
                                         해결근거=f"{판} 조번호로 표기됨 → 조제목 '{tbl[조]}' 로 재매칭")
                                break
                    else:
                        e.update(해결상태="resolved", dst_doc_id=지침_DOCID)
                elif target == "요령":
                    # 「중소기업창업 지원사업 운영요령」(중기부고시 2024-101). 코퍼스에 있다.
                    e.update(해결상태="resolved",
                             dst_doc_id="L1_중소기업창업지원사업운영요령_20241206")
                elif target in ("관리기준", "기준"):
                    # 자기 문서 내부 참조 ("본 관리기준 제N조")
                    e.update(관계="인용", 해결상태="resolved", dst_doc_id=doc_id,
                             해결근거="자기 문서 내부 참조")
                elif target in ("법", "시행령", "시행규칙"):
                    # 문서의 제1·2조가 「…」(이하 "법"이라 한다) 로 직접 정의한 경우
                    # 그 정의로 해결한다. 정의가 없는 문서는 미해결 참조로 남는다.
                    _약칭_해결(e, target, 약칭표, corpus)
            edges.append(e)
    return edges


def main() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-filter", default="",
                    help="문서 stem 에 이 문자열이 들어간 문서만 처리한다")
    ap.add_argument("--show", default="")
    args = ap.parse_args()

    corpus = load_corpus_names()
    print(f"코퍼스 규정명 {len(corpus)}건 로드")
    for must in ("중소기업창업지원사업운영요령", "중소기업창업지원법"):
        if must not in corpus:
            print(f"  경고: 코퍼스에 '{must}' 가 없다 — 이 규정 참조는 dangling 이 된다")

    # 입력은 PDF 가 아니라 조문 분할 결과다 — 법령 XML 포함, 이중 파싱 없음.
    조문분할_파일 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_articles.json"
    if not 조문분할_파일.exists():
        sys.exit(f"조문 분할 결과가 없다: {조문분할_파일}")
    조문분할_결과 = json.loads(조문분할_파일.read_text(encoding="utf-8"))

    all_edges, per_doc, skipped = [], {}, []
    strat_count = Counter()
    약칭_문서수, 약칭_누적 = 0, Counter()
    items = [(k, v) for k, v in 조문분할_결과.items() if not args.doc_filter or args.doc_filter in k]
    total = len(items)
    for n, (stem, d) in enumerate(items, 1):
        arts = d.get("articles") or []
        strategy = d.get("strategy") or "?"
        if not arts:
            skipped.append("%s (조 0개)" % stem[:50])
            continue
        # 문서 전체가 아니라 조 단위로 스캔한다 — src_조번호 가 비면 참조 확장 SQL 의
        # `(src_doc_id, src_조번호)` 조인이 성립하지 않는다.
        # 폐지 조문은 참조 원천이 아니다 — 효력이 없으므로 따라가면 안 된다.
        # 범위 밖(모두의창업 제3편 로컬트랙)은 위임 체계가 달라 참조도 수집하지 않는다.
        밖 = 범위밖_조(stem, arts)
        # 약칭표는 문서 전체에서 한 번 모은다 — 정의(제1·2조)와 쓰이는 곳(중간 조)이
        # 떨어져 있어 조 단위로 모으면 못 만난다. 삭제·범위밖 조도 정의 원천으로는 읽는다.
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
        # 계층(layer)을 엣지에 붙인다 — 미해결 참조 비율을 계층별로 봐야 L2 의 실제
        # 해결 실패가 드러난다.
        lay = d.get("layer")
        for e in edges:
            e["src_layer"] = lay
        all_edges += edges
        per_doc[stem] = len(edges)
        strat_count[strategy] += 1
        if n % 25 == 0 or n == total:
            print("  [%3d/%d] 누적 %d엣지" % (n, total, len(all_edges)), flush=True)

    st = Counter(e["해결상태"] for e in all_edges)
    rel = Counter(e["관계"] for e in all_edges)
    doc = {
        "생성": "scripts/build_refs.py",
        "기준일": "2026-08-27",
        "문서수": len(per_doc),
        "엣지수": len(all_edges),
        "해결상태": dict(st),
        "해결상태_계층별": {
            lay: dict(Counter(e["해결상태"] for e in all_edges if e.get("src_layer") == lay))
            for lay in sorted({e.get("src_layer") for e in all_edges} - {None})
        },
        "관계": dict(rel),
        "주의": ("dangling 은 '코퍼스에 없다'는 뜻이고, 그 자체가 판정 불가 신호다. "
                 "업로드 시점에 사용자에게 알려 해당 문서를 올리게 유도한다."),
        "edges": all_edges,
        "제외문서": skipped,
        "조문분할_전략": dict(strat_count),
        "src_조번호_보유건수": sum(1 for e in all_edges if e.get("src_조번호") != "문서전체"),
        "약칭": {
            "약칭정의_문서수": 약칭_문서수,
            "약칭_종류": dict(약칭_누적.most_common(20)),
            "약칭으로_해결": sum(1 for e in all_edges
                              if (e.get("해결근거") or "").startswith("문서 자체 정의")),
            "약칭해결_코퍼스없음": sum(1 for e in all_edges
                                 if (e.get("해결근거") or "").endswith("코퍼스에 없는 규정")),
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n문서 {len(per_doc)}건 → 엣지 {len(all_edges)}개  → {OUT.relative_to(ROOT)}")
    print("  해결상태:", dict(st))
    print("  관계    :", dict(rel))
    print(f"  약칭    : 정의 보유 문서 {약칭_문서수}건 · "
          f"약칭으로 해결 {doc['약칭']['약칭으로_해결']}건 · "
          f"코퍼스에 없음 {doc['약칭']['약칭해결_코퍼스없음']}건")
    print(f"            {list(약칭_누적.most_common(10))}")
    top = sorted(per_doc.items(), key=lambda x: -x[1])[:8]
    print("\n  엣지 많은 문서:")
    for k, v in top:
        print(f"    {v:5d}  {k[:62]}")

    if args.show:
        print(f"\n=== {args.show} 샘플 ===")
        for e in all_edges:
            if args.show in e["src_doc_id"]:
                print(f"  [{e['해결상태']:8s}] {e['관계']:6s} {e['참조문자열'][:58]}"
                      + (f"  → {e['dst_조번호']}" if e["dst_조번호"] else "")
                      + (f"  ({e['해결근거']})" if e["해결근거"] else ""))


if __name__ == "__main__":
    main()
