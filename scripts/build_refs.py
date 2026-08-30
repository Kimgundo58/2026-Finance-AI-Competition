# -*- coding: utf-8 -*-
"""참조 그래프 추출 → `_refs.json`.

RAG 의 존재 이유가 이것이다 (구현.md 원칙 5). 사용자 문서가
*"통합관리지침 제33조부터 제42조까지에 따른다"* 로 끝나면 그 문서만으로는 답이 없다.
가리키는 곳을 따라가 실제 조항에 닿아야 한다.

한 행 = 엣지 하나. 그래프 DB 는 쓰지 않는다 — 깊이 3, 재귀 CTE 로 밀리초다.
스키마: db/init/01_schema.sql 의 `refs` 테이블. 상세: RAG.md §4-3

해소 상태 3종
    resolved  대상이 코퍼스에 있고 조번호도 맞는다
    shifted   조번호가 구판이라 조제목으로 재매칭했다  (실측 다수)
    dangling  대상이 코퍼스에 없다 → 판정 시점이 아니라 **업로드 시점에 알린다**

실행:
    python scripts/build_refs.py                  전체
    python scripts/build_refs.py --src 창진원      일부만
    python scripts/build_refs.py --show 예비창업    결과 확인
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdftext  # noqa: E402  문자중복 레이어 + 2단 조판 자동 처리
from stage0_articles import split_articles  # noqa: E402  섹션분리 + 3단 fallback

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


def squash(t: str) -> tuple[str, list[int]]:
    buf, idx = [], []
    for i, ch in enumerate(t):
        if not ch.isspace():
            buf.append(ch)
            idx.append(i)
    return "".join(buf), idx


# 규범명 정규화. 세 가지를 걷어내야 코퍼스와 대조가 된다 (2026-08-30).
#   약칭 괄호  「중소기업창업 지원사업 운영요령(이하 "운영요령"이라 한다)」
#   파일명 구분자  L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223
#   판본 꼬리표    …_제14차개정_20251223 · (제2024-101호)(20241206)
# 이걸 안 하면 **코퍼스에 있는 규범도 dangling 으로 샌다.** 실측: 현행 9문서가 인용한
# dangling 34종 중 4종이 실제로는 보유분이었다.
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
    # 「대·중소기업 상생협력 촉진에 관한 법률」이 계속 dangling 이었다.
    s = re.sub(r"[\s_·ㆍ‧․∙⋅•\-—]", "", s)
    return s.strip("「」『』‘’\"'.,")


def load_corpus_names() -> dict[str, str]:
    """코퍼스가 보유한 규범명 → 파일 경로. 해소 가능 여부의 판정 기준."""
    names = {}
    src = ROOT / "법령 PDF" / "_law_sources.json"
    if src.exists():
        for k in json.loads(src.read_text(encoding="utf-8")):
            names[norm_규범(k)] = f"법령 PDF/L1_법령/{k}"
    # 중기부·창진원 배포본
    for f in (ROOT / "2026_Finance_DATA_FOR_RAG").rglob("*.pdf"):
        names.setdefault(norm_규범(f.stem), str(f.relative_to(ROOT)))
    return names


# 인용 표기 -> 코퍼스의 실제 제명 (2026-08-30 실측).
# 🔴 이게 없으면 **가진 법도 참조가 안 이어진다.** 세부관리기준이 「근로자직업능력개발법」
#    이라고 쓸 때 우리는 「국민 평생 직업능력 개발법」으로 갖고 있어서, 참조 폐포가
#    거기서 끊긴다 — RAG 가 그 법을 못 가져온다는 뜻이다.
#    수집 단계(`fetch_missing_norms.py`)와 같은 실측표다.
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


def resolve_규범(name: str, corpus: dict[str, str]) -> tuple[str, str | None]:
    """규범명이 코퍼스에 있나. (해소상태, 경로)"""
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
        if key in k or k in key:
            gap = abs(len(k) - len(key))
            if gap <= 12 and (best is None or gap < best[0]):
                best = (gap, v)
    return ("resolved", best[1]) if best else ("dangling", None)


def scan(text: str, doc_id: str, corpus: dict[str, str],
         src_조번호: str = "문서전체") -> list[dict]:
    t = re.sub(r"\s+", " ", text)
    sq, imap = squash(t)
    edges: list[dict] = []
    seen: set[tuple] = set()

    def 원문(m) -> str:
        a, b = imap[m.start()], imap[m.end() - 1] + 1
        return re.sub(r"\s+", " ", t[a:b])

    for kind, pat in PATTERNS:
        for m in pat.finditer(sq):
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
                e.update(관계="인용", dst_doc_id=path, 해소상태=st)
            elif kind == "별표":
                e.update(관계="별표참조", dst_doc_id=doc_id,
                         dst_조번호=f"{m.group(1)}{m.group(2)}", 해소상태="resolved")
            elif kind == "미규정위임":
                e.update(관계="미규정위임", 해소상태="resolved",
                         보정근거="상위 규범으로 위임. 게이팅(파이프라인 §6.2)의 근거")
            else:  # 조 / 범위
                target = m.group(1)
                조 = m.group(2)
                e.update(관계="준용" if target in ("지침", "요령") else "인용",
                         dst_조번호=f"제{조}조")
                # 지침 참조는 판본 어긋남을 검사한다 (파이프라인 §2.5)
                if target == "지침":
                    for 판, tbl in 지침_조제목.items():
                        if 판 != "제14차" and 조 in tbl:
                            현행 = [k for k, v in 지침_조제목["제14차"].items() if v == tbl[조]]
                            if 현행:
                                e.update(해소상태="shifted",
                                         dst_doc_id="L1_통합관리지침_제14차",
                                         dst_조번호=f"제{현행[0]}조",
                                         보정근거=f"{판} 조번호로 표기됨 → 조제목 '{tbl[조]}' 로 재매칭")
                                break
                    else:
                        e.update(해소상태="resolved", dst_doc_id="L1_통합관리지침_제14차")
                elif target == "요령":
                    # 「중소기업창업 지원사업 운영요령」(중기부고시 2024-101). 코퍼스에 있다.
                    e.update(해소상태="resolved",
                             dst_doc_id="L1_중소기업창업지원사업운영요령_20241206")
                elif target in ("관리기준", "기준"):
                    # 자기 문서 내부 참조 ("본 관리기준 제N조")
                    e.update(관계="인용", 해소상태="resolved", dst_doc_id=doc_id,
                             보정근거="자기 문서 내부 참조")
                elif target in ("법", "시행령", "시행규칙"):
                    # 앞말이 잘린 참조("…법 제5조"). 어느 법인지 확정 불가.
                    e.update(해소상태="dangling",
                             보정근거="규범명이 앞에서 잘림 — 문맥으로 확정 필요")
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
    #         법령 간 상호참조가 없으면 참조 폐포가 L2 안에서 끝난다 — RAG 의 존재 이유가 반토막
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
    items = [(k, v) for k, v in stage0.items() if not args.src or args.src in k]
    total = len(items)
    for n, (stem, d) in enumerate(items, 1):
        arts = d.get("articles") or []
        strategy = d.get("strategy") or "?"
        if not arts:
            skipped.append("%s (조 0개)" % stem[:50])
            continue
        # 문서 전체가 아니라 **조 단위**로 스캔한다. src_조번호 가 비면
        # RAG.md §4-3 참조 폐포 SQL 의 `(src_doc_id, src_조번호)` 조인이 성립하지 않는다.
        # 폐지 조문은 참조 원천이 아니다 — 효력이 없으므로 따라가면 안 된다.
        edges = []
        for a in arts:
            if a.get("삭제"):
                continue
            edges += scan(a["본문"], stem, corpus, src_조번호=a["조번호"])
        # 레이어를 엣지에 붙인다. dangling 비율을 레이어별로 봐야 신호가 산다 —
        # L1 법령끼리의 인용은 코퍼스 경계(219 규범) 밖으로 나가면 당연히 dangling 이고
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
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n문서 {len(per_doc)}건 → 엣지 {len(all_edges)}개  → {OUT.relative_to(ROOT)}")
    print("  해소상태:", dict(st))
    print("  관계    :", dict(rel))
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
