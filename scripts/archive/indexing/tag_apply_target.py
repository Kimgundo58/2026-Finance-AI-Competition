# -*- coding: utf-8 -*-
"""Stage 0.5 : 적용대상 태깅 -> `_apply_target.json`.

값은 `주관기관 | 창업기업 | 공통` 셋이다 (`chunks.적용대상`).
우리 사용자는 창업기업이므로 검색이 `적용대상 IN ('창업기업','공통')` 으로 걸린다.
이 필터가 없으면 "노트북 사도 되나요?" 에 주관기관 전담인력 규정이 섞여 나온다.

🔴 **NULL 두 종류를 섞으면 안 된다.** 태깅 대상 **밖**(법령 264문서 23,324조)은 값이
없는 게 아니라 기본값 `공통` 이다 — NULL 로 두면 `IN` 을 통과하지 못해 조용히 검색에서
빠진다. 태깅 대상 **안**의 NULL 만 2단 LLM 대기이고 그것만 인덱스에서 제외한다.
Stage 2 는 반드시 `적용대상_of()` 를 거친다.

🔴 **`RAG.md` §3 의 "절(節) 헤딩이 이미 적용대상을 선언한다" 는 전제는 틀렸다** (2026-08-30 실측).
7개 사업의 장 제목을 전수로 뽑아 보면 총칙 / 추진체계 / 선정 / 협약 / 사업운영 …
**절차 구분이지 주체 구분이 아니다.** 상속시킬 헤딩이 없다.
실제로 주체를 선언하는 것은 **조 제목**이다 — `제15조(주관기관 사업비 비목)` /
`제22조(창업기업등 사업비 비목)`.

그래서 2단으로 간다.

    1단 결정적   조제목 주어 -> 본문 첫머리 주어      실측 커버리지 87%
    2단 LLM      1단이 못 가른 것만                  306조 중 131조

**결정적 단계를 먼저 두는 이유**는 비용이 아니라 재현성이다. 규칙으로 갈리는 것을
LLM 에 넘기면 같은 조가 실행마다 다르게 태깅될 수 있고, 그러면 검색 결과가 흔들린다.

실행:
    python scripts/archive/indexing/tag_apply_target.py            결정적 단계까지
    python scripts/archive/indexing/tag_apply_target.py --stats    분포만 본다
"""
from __future__ import annotations

# 🔴 2026-09-05 scripts/archive/ 이관 — 원래 scripts/ 바로 밑에 있던 파일이라
#    아래(또는 이 파일의 기존 sys.path 계산)는 scripts/ 바로 밑 기준으로 짜여 있다.
#    이관으로 깊이가 늘어나 깨지므로, `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가
#    scripts/ 와 프로젝트 루트를 sys.path 맨 앞에 다시 건다.
import os as _os_이관, sys as _sys_이관
_p_이관 = _os_이관.path.dirname(_os_이관.path.abspath(__file__))
while not _os_이관.path.isdir(_os_이관.path.join(_p_이관, "_lib")):
    _parent_이관 = _os_이관.path.dirname(_p_이관)
    if _parent_이관 == _p_이관:
        break
    _p_이관 = _parent_이관
if _p_이관 not in _sys_이관.path:
    _sys_이관.path.insert(0, _p_이관)
if _os_이관.path.dirname(_p_이관) not in _sys_이관.path:
    _sys_이관.path.insert(0, _os_이관.path.dirname(_p_이관))
# 🔴 archive 내부에서 카테고리를 넘나드는 import(예: index_guard, stage0_run)가
#    있어 scripts/archive/ 의 모든 하위 폴더도 같이 건다.
_archive_이관 = _os_이관.path.join(_p_이관, "archive")
if _os_이관.path.isdir(_archive_이관):
    for _d_이관 in _os_이관.listdir(_archive_이관):
        _full_이관 = _os_이관.path.join(_archive_이관, _d_이관)
        if _os_이관.path.isdir(_full_이관) and _full_이관 not in _sys_이관.path:
            _sys_이관.path.insert(0, _full_이관)


import argparse
import io
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "scripts" / "_lib").is_dir())  # 🔴 2026-09-05 archive 이관 — 깊이 무관 계산으로 교체
sys.path.insert(0, str(ROOT / "scripts"))
import index_guard
from scope import 범위밖_조                                          # noqa: E402

DATA = ROOT / "2026_Finance_DATA_FOR_RAG"
S0 = DATA / "_stage0_articles.json"
OUT = DATA / "_apply_target.json"
TODO = DATA / "_apply_target_todo.json"

# 태깅 대상. 법령 229건에는 걸지 않는다 —
# 「산업안전보건법 시행규칙」 390조에 주관기관/창업기업 구분은 존재하지 않는다.
대상_포함 = ("세부관리기준", "관리기준", "통합관리지침", "운영지침")
대상_제외 = ("별지서식", "모집공고", "공고문", "현황", "신청서")

# 🔴 **태깅 대상 밖 문서의 기본값** (2026-08-30 결정).
#   태깅하지 않는다는 것과 값이 없다는 것은 다르다. 검색 필터가
#   `적용대상 IN ('창업기업','공통')` 이고 SQL 의 NULL 은 IN 을 통과하지 못하므로,
#   NULL 로 두면 **법령 229건 약 19,200조가 조용히 검색에서 빠진다.**
#   조용히 빠지는 것은 근거 누락 = 오답이라 가장 나쁜 실패다.
#   일반 법령에는 주체 구분이 애초에 존재하지 않으므로 `공통` 이 맞다.
#   태깅 **대상 안**에서의 NULL 은 의미가 다르다 — 2단 LLM 대기이고, 그대로 두면
#   주관기관 규정이 창업기업 판정에 섞이므로 Stage 2 가 인덱스에 올리지 않는다.
대상밖_기본값 = "공통"

# 🔴 **현행/구판 구분이 파이프라인 어디에도 없다.** `documents.status` 가 스키마에는
# 있는데 Stage 0 이 채우지 않는다. 구판까지 태깅하면 판정 인덱스에 안 들어갈 것을
# 태깅하는 낭비이고, 결정률 통계도 흐려진다 (구판 포함 51% vs 현행만 87%).
# 임시로 현행 목록을 여기 상수로 둔다. **`status` 를 Stage 0 이 채우면 이 상수는 지운다.**
현행 = {
    "예비창업패키지 세부관리기준(2025년)",
    "초기창업패키지 세부관리기준(2025년)",
    "2026년 재도전성공패키지 세부관리기준(11차 개정)",   # 2026-09-01 판본 역전 교정
    "창업도약패키지 세부관리기준(2025년)",
    "창업중심대학 세부관리기준2025년 개정",
    "초격차 스타트업 프로젝트 세부관리기준(제10차)",
    "모두의 창업 프로젝트 세부관리기준(개정본)",
    "붙임1. 2026년 팁스TIPS 총괄 운영지침 3차 개정안 본문",
    "L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223",
}

RE_주관 = re.compile(r"주관기관|전담조직|전담인력|운영기관|지역허브|전문기관|총괄기관|운영사")
RE_창업 = re.compile(r"창업기업|창업자|진출자|도전자|입주기업|창업팀")

# 본문 주어 판별 구간. 조 첫머리에 주어가 온다.
HEAD = 200

# ── 절(節) 상속 (2026-08-31 구현) ─────────────────────────────────────────────
# `RAG.md` §3 에 "절 헤딩이 선언하면 상속, LLM 은 절 밖 조문만" 이라고 명세돼 있었는데
# 구현이 빠져 있었다. 조문 재조립이 조 단위라 절 헤딩은 **앞 조 본문 꼬리**에 붙어 온다:
#   "...심의결과를 전문기관의 장에게 보고하여야 한다. 제 4 장 협 약 < 제 1 절 주관기관 >"
#
# 🔴 우선순위는 조제목 > 절 > 본문주어 다. 실측 근거:
#   조제목 경로 96건 중 절이 값을 주는 49건 -> 충돌 0
#   본문주어 경로 151건 중 절이 값을 주는 70건 -> 충돌 9 (13%)
#   충돌 9건 전수 확인 결과 **9건 중 8건은 절이 맞고 본문주어가 틀렸다** (1건 판단보류).
#   대표: 통합관리지침 제37조(재료비)가 '주관기관' 으로 태깅돼 있었다. 본문에
#   "전문기관의 장 또는 주관기관의 장의 사전승인" 이 나와서다. 재료비는 창업기업등
#   비목(제36~45조)이고, 이대로 인덱싱하면 "재료 사도 되나요?" 에 재료비 조항이
#   검색 필터에서 빠진다. 창업도약 제31조·모두의창업 제34조도 같은 재료비 조항이었다.
RE_절장 = re.compile("[<〈]?\s*제\s*(\d+)\s*(절|장)\s*[>〉]?\s*([^<>\n]{0,22})")
RE_절_공통 = re.compile(r"공통")
# 절 이름 전용 보강. 절 헤딩은 주체를 **선언**하므로 본문주어보다 어휘를 넓게 잡아도 안전하다.
# (RE_주관 을 넓히면 본문주어 판정까지 흔들리므로 절 전용으로 분리한다)
#   "< 제1절 창업중심대학 >" — 창업중심대학이 그 사업의 주관기관이다
RE_절_주관 = re.compile(RE_주관.pattern + r"|창업중심대학|전담기관|주관대학")


def 절값(꼬리: str) -> str | None:
    """절 헤딩의 이름에서 적용대상을 읽는다. 못 읽으면 None (상속을 끊는다)."""
    if RE_절_공통.search(꼬리):
        return "공통"
    주, 창 = bool(RE_절_주관.search(꼬리)), bool(RE_창업.search(꼬리))
    if 주 and not 창:
        return "주관기관"
    if 창 and not 주:
        return "창업기업"
    return None


def 절_상속(articles: list[dict]) -> dict[str, str | None]:
    """조번호 -> 그 조가 속한 절의 적용대상. 장이 바뀌면 상속을 끊는다."""
    out: dict[str, str | None] = {}
    state: str | None = None
    for a in articles:
        # 🔴 조가 **장/절을 명시로 들고 오면** 그걸 쓴다 (2026-09-01).
        #    본문 꼬리에서 절 헤딩을 긁는 방식은 «헤딩이 앞 조 본문에 붙어 온다» 는
        #    조문 재조립의 부산물에 기댄 것이다. VLM 판독본은 조 단위로 정제돼 오므로
        #    꼬리에 헤딩이 없다 — 재도전 2026판 33조 중 11조가 그래서 미결로 떨어졌고,
        #    그 11조에 사업비 비목 6조(회의비·여비·지급수수료·외주용역비·기계장치·
        #    광고선전비)가 전부 들어 있었다. 룰 근거가 바로 그 조들이라 인덱스에
        #    못 올라가면 재도전 판정이 통째로 죽는다.
        #    명시 필드가 있으면 «제5장 사업비의 구성 / 제1절 주관기관» 처럼 오므로
        #    같은 `절값()` 으로 읽는다 — 판정 규칙은 하나만 둔다.
        장문자열 = (a.get("장") or "").strip()
        if 장문자열:
            state = 절값(장문자열.split("/")[-1]) if "절" in 장문자열 else None
        out[a["조번호"]] = state              # 조 **시작 시점**의 절 상태
        if not 장문자열:
            for m in RE_절장.finditer(a.get("본문") or ""):
                state = None if m.group(2) == "장" else 절값(m.group(3))
    return out


# 범위 밖 구간(모두의창업 제3편 로컬트랙) 컷은 `scripts/scope.py` 가 기준 문서이다.
# build_refs 와 같은 컷을 써야 해서 공용 모듈로 뺐다 (2026-08-31).


# ── 2단 결과 병합 (2026-08-31) ────────────────────────────────────────────────
# 1단(결정적: 조제목 > 절 상속 > 본문주어)이 못 가른 조를 사람/LLM 이 판단한 결과.
# 파일이 없으면 그냥 건너뛴다 — 1단만으로도 파이프라인은 돈다.
# `verified=false` 다. 룰과 같은 취급 — 이 태그만으로 "가능" 을 만들지 않는다.
STAGE2 = ROOT / "2026_Finance_DATA_FOR_RAG" / "_apply_target_2단.json"


def 이단맵() -> dict[tuple[str, str], dict]:
    if not STAGE2.exists():
        return {}
    d = json.loads(STAGE2.read_text(encoding="utf-8"))
    return {(r["doc_id"], r["조번호"]): r for r in d.get("items", [])}


def is_target(doc_id: str, 현행만: bool = True) -> bool:
    if any(k in doc_id for k in 대상_제외):
        return False
    if not any(k in doc_id for k in 대상_포함):
        return False
    return doc_id in 현행 if 현행만 else True


def 태그맵(tags: list[dict]) -> dict[tuple[str, str], str | None]:
    """`_apply_target.json` 의 `tags` 를 `(doc_id, 조번호) -> 적용대상` 으로 뒤집는다."""
    return {(t["doc_id"], t["조번호"]): t["적용대상"] for t in tags}


def 적용대상_of(doc_id: str, 조번호: str,
                맵: dict[tuple[str, str], str | None]) -> str | None:
    """Stage 2 가 청크 1건의 `적용대상` 을 정하는 단일 진입점.

    반환이 None 이면 **인덱스에 올리지 않는다** (2단 LLM 대기).

        태깅 대상 밖  -> '공통'            주체 구분이 존재하지 않는 규범
        태깅 대상 안  -> 태그값 또는 None  None = LLM 대기
        부칙·붙임      -> '공통'            본칙이 아니라 태깅에서 빠졌다.
                                          시행일·경과조치는 누구에게나 적용된다
    """
    if not is_target(doc_id):
        return 대상밖_기본값
    if (doc_id, 조번호) not in 맵:
        return 대상밖_기본값                # 부칙·붙임 (조번호_int 가 None 이라 제외됨)
    return 맵[(doc_id, 조번호)]


def 결정(조제목: str | None, 본문: str) -> tuple[str | None, str, float]:
    """(적용대상, 판정경로, 확신도). 못 가르면 (None, '미결정', 0.0)."""
    title = 조제목 or ""
    t주, t창 = bool(RE_주관.search(title)), bool(RE_창업.search(title))
    if t주 and not t창:
        return "주관기관", "조제목", 0.95
    if t창 and not t주:
        return "창업기업", "조제목", 0.95
    if t주 and t창:
        return None, "조제목_혼재", 0.0        # LLM 이 본다

    head = 본문[:HEAD]
    h주, h창 = bool(RE_주관.search(head)), bool(RE_창업.search(head))
    if h주 and not h창:
        return "주관기관", "본문주어", 0.75
    if h창 and not h주:
        return "창업기업", "본문주어", 0.75
    if h주 and h창:
        return None, "본문_혼재", 0.0          # LLM 이 본다
    return None, "미결정", 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--구판포함", action="store_true",
                    help="구판까지 태깅한다 (판정 인덱스 대상이 아니므로 기본은 제외)")
    args = ap.parse_args()

    if not S0.exists():
        sys.exit(f"Stage 0 산출물이 없다: {S0}")
    stage0 = json.loads(S0.read_text(encoding="utf-8"))
    docs = {k: v for k, v in stage0.items() if is_target(k, not args.구판포함)}
    print(f"태깅 대상 문서 {len(docs)}건 / 전체 {len(stage0)}\n")

    tagged: list[dict] = []
    todo: list[dict] = []
    경로 = Counter()
    값 = Counter()

    범위밖 = 이단적용 = 0
    뒤집힘: list[dict] = []
    이단 = 이단맵()
    for doc_id, d in docs.items():
        arts = d.get("articles") or []
        절 = 절_상속(arts)
        밖 = 범위밖_조(doc_id, arts)
        for a in arts:
            if a.get("삭제"):
                continue
            if a.get("조번호_int") is None:
                continue                      # 부칙·붙임은 본칙이 아니다
            if a["조번호"] in 밖:
                범위밖 += 1
                continue                      # 로컬트랙 — 태깅도 인덱싱도 하지 않는다
            val, path, conf = 결정(a.get("조제목"), a.get("본문") or "")
            # 조제목(0.95)은 그대로 두고, 그 아래는 절 상속이 이긴다 (근거는 위 주석)
            j = 절[a["조번호"]]
            if path != "조제목" and j is not None:
                if val is not None and val != j:
                    뒤집힘.append({"doc_id": doc_id, "조번호": a["조번호"],
                                   "조제목": a.get("조제목"), "전": val,
                                   "후": j, "전_경로": path})
                val, path, conf = j, "절상속", 0.90
            검수 = False
            if val is None:
                r = 이단.get((doc_id, a["조번호"]))
                if r:
                    val, path, conf = r["적용대상"], "2단", 0.70
                    검수 = bool(r.get("검수필요"))
                    이단적용 += 1
            경로[path] += 1
            rec = {"doc_id": doc_id, "조번호": a["조번호"], "조제목": a.get("조제목"),
                   "적용대상": val, "판정경로": path, "확신도": conf}
            if 검수:
                rec["검수필요"] = True
            if val is None:
                todo.append({**rec, "본문": (a.get("본문") or "")[:1200]})
            else:
                값[val] += 1
            tagged.append(rec)

    print(f"범위밖(로컬트랙) 제외: {범위밖}조")
    print(f"절 상속이 본문주어를 뒤집은 건: {len(뒤집힘)}")
    for r in 뒤집힘:
        print(f"   {r['doc_id'][:30]}|{r['조번호']} '{r['조제목']}'  {r['전']} -> {r['후']}")
    print(f"2단 병합: {이단적용}조 (파일 {'있음' if 이단 else '없음'})")
    print("판정경로:", dict(경로))
    print("결정값  :", dict(값))
    n = len(tagged)
    print(f"\n조 {n}  ·  결정 {n - len(todo)} ({(n-len(todo))/max(n,1):.0%})  ·  LLM 대기 {len(todo)}")
    print("\n문서별 미결:")
    for k, c in Counter(t["doc_id"] for t in todo).most_common():
        print(f"   {c:>3}  {k[:56]}")

    # 태깅 대상 밖이 얼마나 되는가. 기본값을 안 주면 이 만큼이 검색에서 사라진다.
    밖_문서 = 밖_조 = 0
    for doc_id, d in stage0.items():
        if doc_id in docs or index_guard.reject_reason(d.get("path", ""), d.get("layer")):
            continue
        if d.get("layer") not in ("L1", "L2"):
            continue                          # 사례는 case_chunks 라 이 필터를 안 탄다
        밖_문서 += 1
        밖_조 += sum(1 for a in (d.get("articles") or []) if not a.get("삭제"))
    print(f"\n태깅 대상 밖 (L1·L2): 문서 {밖_문서}  조 {밖_조}"
          f"  -> 기본값 '{대상밖_기본값}'")

    if args.stats:
        return
    OUT.write_text(json.dumps({
        "생성": "scripts/archive/indexing/tag_apply_target.py",
        "값": ["주관기관", "창업기업", "공통"],
        "범위밖": {"제외조": 범위밖,
                   "근거": "모두의 창업 제3편 로컬트랙 — 위임 계통이 다르다 (CLAUDE.md 사업 스코프)"},
        "절상속_뒤집음": 뒤집힘,
        "기본값": {
            "값": 대상밖_기본값,
            "적용": "태깅 대상 밖 문서 전부 + 태깅 대상 안의 부칙·붙임",
            "근거": ("일반 법령에는 주체 구분이 애초에 존재하지 않는다. NULL 로 두면 "
                     "검색 필터 `적용대상 IN ('창업기업','공통')` 을 통과하지 못해 "
                     "조용히 빠진다 — 근거 누락은 오답이다."),
            "규모": {"문서": 밖_문서, "조": 밖_조},
            "해석기": "scripts/archive/indexing/tag_apply_target.py::적용대상_of",
        },
        "주의": ("1단(결정적)만 채운 상태다. `적용대상=null` 인 행은 2단(LLM) 대기이고 "
                 "**태깅 대상 안에서만 발생한다** — Stage 2 는 이 null 을 인덱스에 "
                 "올리지 않는다. 필터가 안 걸리면 주관기관 규정이 창업기업 판정에 섞인다. "
                 "태깅 대상 **밖**은 null 이 아니라 위 `기본값` 이다. 둘을 섞지 말 것 — "
                 "`적용대상_of()` 를 거치면 구분된다."),
        "요약": {"문서": len(docs), "조": n, "결정": n - len(todo), "미결": len(todo),
                 "판정경로": dict(경로), "값분포": dict(값),
                 "대상밖_문서": 밖_문서, "대상밖_조": 밖_조},
        "tags": tagged,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    TODO.write_text(json.dumps({
        "생성": "scripts/archive/indexing/tag_apply_target.py",
        "용도": "2단 LLM 태깅 입력. 조 본문 1,200자까지 잘라 담았다.",
        "건수": len(todo),
        "items": todo,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n-> {OUT.relative_to(ROOT)}\n-> {TODO.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
