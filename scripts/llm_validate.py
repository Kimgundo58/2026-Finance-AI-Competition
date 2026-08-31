# -*- coding: utf-8 -*-
"""(5) 검증·강등기. LLM 출력 [1겹] → 최종 응답 [2겹].

정본: `LLM.md` §3-4(스키마·강등) · §3-7(S번호 사양) · `rule_base.md`(verified 규칙)

원칙 하나로 요약하면 — **LLM 이 자칭한 것은 아무것도 믿지 않는다.**
인용은 s맵으로, 신뢰등급은 `documents.extraction` 으로, 전제는 F 스키마 컬럼으로 검증한다.
검증에 실패한 것은 **고치지 않고 폐기하거나 강등한다.** 기본값은 판단불가다.

═══ 조립기(중앙세션)와의 계약 ═══
    s맵: dict[str, tuple[str, int|None, str|None]]
         {"S01": ("chunk", 12345, "①"), "S07": ("article", 6789, None), ...}
    def 검증(llm출력: dict, s맵: dict) -> tuple[dict, list[str]]

⚠️ 위치인자 2개는 계약 그대로다. 아래 `*` 뒤 키워드는 **전부 기본값이 있어**
   `검증(출력, s맵)` 2인자 호출이 그대로 동작한다. 조립기 코드는 바꿀 필요가 없다.
   키워드를 둔 이유: 강등 규칙 중 셋(vlm·verified·F스키마)이 DB 사실을 필요로 하는데,
   그걸 함수 안에서 직접 조회하면 테스트가 DB 없이는 못 돈다.
   기본값은 라이브 DB 조회이고, 테스트는 스텁을 넣는다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/llm_validate.py --self-test
    PYTHONIOENCODING=utf-8 python scripts/llm_validate.py --f-paths   # F 경로 실측 출력
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from typing import Any, Callable, Iterable, Optional

# 훅이 PYTHONIOENCODING=utf-8 을 강제하므로 보통 이미 utf-8 이다.
# 조건 없이 다시 감싸면 **import 될 때 앞의 래퍼가 GC 되며 버퍼가 닫힌다** —
# llm_validate 가 llm_schema 를 import 하는 순간 터졌다.
if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # PYTHONPATH 없이 동작
from llm_schema import 판정_ENUM, 미충족시_ENUM, 인용, 전제, 최종응답  # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# ════════════════════════════════════════════════════════════════════════════
# F 필드 경로 — `tenant` 실제 컬럼에서 만든다 (하드코딩 금지)
# ════════════════════════════════════════════════════════════════════════════
# `서비스 아키텍쳐.md` §2-3 의 축 번호 ↔ 테이블. 경로 표기는 §222 의
# `["F1.정부지원.현금", ...]` 형식이다 — 컬럼 `정부지원_현금` 의 `_` 가 `.` 이 된다.
F축_테이블 = {"F1": "f_profile", "F3": "f_exec", "F4": "f_personnel"}
# f_profile 안에 살지만 축 번호가 따로 붙은 것
F축_특례 = {"F2": {"과업범위요약"}}
# 저장하지 않는 축 (판정 후 폐기) — 테이블이 없으므로 문서의 항목명을 그대로 연다
F5_항목 = {"친족", "전직임직원", "구매명의"}
_메타컬럼 = {"profile_id", "org_id", "exec_id", "person_id", "created_at", "updated_at"}


def f_경로집합(dsn: str | None = None) -> set[str]:
    """허용되는 F 필드 경로 전체. DB 컬럼이 정본이다."""
    import psycopg
    out: set[str] = {f"F5.{x}" for x in F5_항목}
    for 축, 컬럼 in F축_특례.items():
        out |= {f"{축}.{c}" for c in 컬럼}
    with psycopg.connect(dsn or DSN) as conn:
        for 축, t in F축_테이블.items():
            for (c,) in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='tenant' AND table_name=%s", (t,)).fetchall():
                if c in _메타컬럼:
                    continue
                out.add(f"{축}." + c.replace("_", "."))
    return out


def _항_추출(본문: str, 항호: str | None) -> tuple[str, str]:
    """조 본문에서 해당 항만 잘라낸다. (원문, 범위)

    🔴 **자르기지 요약이 아니다.** 반환값은 반드시 본문의 **정확한 부분 문자열**이다
    (CLAUDE.md 원칙 4 — 인용은 생성이 아니라 추출).
    마커를 못 찾으면 조 전체를 돌려주고 그 사실을 범위로 알린다. 지어내지 않는다.
    """
    if not 본문:
        return "", "없음"
    if not 항호:
        return 본문, "조전체"
    k = 본문.find(항호)
    if k < 0:
        return 본문, "조전체(항호 미발견)"
    뒤 = [본문.find(m, k + 1) for m in _항마커 if 본문.find(m, k + 1) > 0]
    end = min(뒤) if 뒤 else len(본문)
    잘린 = 본문[k:end].strip()
    return (잘린, "항") if 잘린 else (본문, "조전체")


_항마커 = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def s번호_메타(s맵: dict, dsn: str | None = None) -> dict[str, dict]:
    """s맵의 각 S번호 → 치환·등급산정에 필요한 DB 사실.

    §3-4 [2겹] `인용[].조번호·원문 | 코드 | S번호 → DB 원문 치환` 을 여기서 한다.
    반환 키: doc_id · 조번호 · 조제목 · 원문 · 원문범위 · version · extraction · 항호_DB
    """
    import psycopg
    청크 = {sid: i for sid, (k, i, _) in s맵.items() if k == "chunk" and i is not None}
    조문 = {sid: i for sid, (k, i, _) in s맵.items() if k == "article" and i is not None}
    l3 = {sid: i for sid, (k, i, _) in s맵.items() if k == "l3" and i is not None}
    항호 = {sid: h for sid, (_, _, h) in s맵.items()}
    out: dict[str, dict] = {}

    with psycopg.connect(dsn or DSN) as conn:
        # chunk — 청크 자체가 이미 항 단위(§3-7)라 text 를 그대로 쓴다. 자르지 않는다.
        if 청크:
            m = {r[0]: r for r in conn.execute("""
                SELECT c.chunk_id, c.doc_id, c.조번호, c.조제목, c.항호,
                       COALESCE(c.version, d.version), d.extraction, c.text
                  FROM corpus.chunks c JOIN corpus.documents d ON d.doc_id = c.doc_id
                 WHERE c.chunk_id = ANY(%s)""", (list(청크.values()),)).fetchall()}
            for sid, cid in 청크.items():
                if cid in m:
                    _, doc, 조, 제목, h, ver, ex, txt = m[cid]
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=txt or "",
                                    원문범위="청크", version=ver, extraction=ex, 항호_DB=h)
        # article — 조 전체가 오므로 s맵의 항호로 잘라낸다
        if 조문:
            m = {r[0]: r for r in conn.execute("""
                SELECT a.article_id, a.doc_id, a.조번호, a.조제목, a.본문,
                       d.version, d.extraction
                  FROM corpus.doc_articles a JOIN corpus.documents d ON d.doc_id = a.doc_id
                 WHERE a.article_id = ANY(%s)""", (list(조문.values()),)).fetchall()}
            for sid, aid in 조문.items():
                if aid in m:
                    _, doc, 조, 제목, 본문, ver, ex = m[aid]
                    원문, 범위 = _항_추출(본문, 항호.get(sid))
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=원문,
                                    원문범위=범위, version=ver, extraction=ex, 항호_DB=None)
        # l3 — tenant. extraction·version 축이 없다. 기관 업로드분이라 native 로 본다
        if l3:
            m = {r[0]: r for r in conn.execute("""
                SELECT article_id, doc_id::text, 조번호, 조제목, 본문
                  FROM tenant.l3_articles WHERE article_id = ANY(%s)""",
                (list(l3.values()),)).fetchall()}
            for sid, aid in l3.items():
                if aid in m:
                    _, doc, 조, 제목, 본문 = m[aid]
                    원문, 범위 = _항_추출(본문, 항호.get(sid))
                    out[sid] = dict(doc_id=doc, 조번호=조, 조제목=제목, 원문=원문,
                                    원문범위=범위, version=None, extraction="native",
                                    항호_DB=None)
    return out


# ════════════════════════════════════════════════════════════════════════════
# 검증·강등
# ════════════════════════════════════════════════════════════════════════════
def 검증(llm출력: dict, s맵: dict, *,
         메타: Optional[dict[str, dict]] = None,
         f경로: Optional[Iterable[str]] = None,
         룰들: Optional[list[dict]] = None,
         체크코드: Optional[Iterable[str]] = None,
         dsn: Optional[str] = None) -> tuple[dict, list[str]]:
    """(검증·강등된 출력, 강등사유 목록).

    메타/f경로 를 주지 않으면 DB 에서 읽는다. 테스트는 스텁을 넣어 DB 없이 돈다.
    룰들 은 B4 에 들어간 `effective_rule` 결과다 — `[{"verified": bool, ...}]`.
    """
    사유: list[str] = []
    메타 = 메타 if 메타 is not None else s번호_메타(s맵, dsn)
    허용경로 = set(f경로) if f경로 is not None else f_경로집합(dsn)

    # ── 1. 판정 enum ─────────────────────────────────────────────────────
    # 폐쇄 목록 밖이면 고쳐 맞추지 않는다. 모든 실패의 기본값은 판단불가다.
    판정 = llm출력.get("판정")
    if 판정 not in 판정_ENUM:
        사유.append(f"판정 '{판정}' 이 4-way enum 밖 → 판단불가")
        판정 = "판단불가"

    # ── 2. 인용 S번호 ────────────────────────────────────────────────────
    # §3-7: "매핑표에 없는 S번호 인용은 폐기 (환각 인용 차단)"
    인용목록: list[인용] = []
    for s in llm출력.get("인용") or []:
        if s not in s맵:
            사유.append(f"인용 {s} 가 s맵 밖 → 폐기(환각 인용)")
            continue
        종류, _id, 항호 = s맵[s]
        m = 메타.get(s)
        if m is None:
            # s맵엔 있는데 DB 에 없다 = 조립기가 넘긴 id 가 죽었다. 원문을 못 붙이므로 폐기한다.
            사유.append(f"인용 {s} 의 원본을 DB 에서 못 찾음({종류} id={_id}) → 폐기")
            continue
        # 항호는 s맵(조립기)이 정본이다. 청크가 다른 항을 가리키면 조립 사고이므로 남긴다.
        if m.get("항호_DB") and 항호 and m["항호_DB"] != 항호:
            사유.append(f"인용 {s} 항호 불일치(s맵 {항호} vs DB {m['항호_DB']}) → s맵 값 채택")
        인용목록.append(인용(s번호=s, doc_id=m.get("doc_id"), 조번호=m.get("조번호"),
                          조제목=m.get("조제목"), 항호=항호,
                          원문=m.get("원문"), 원문범위=m.get("원문범위"),
                          version=m.get("version"), extraction=m.get("extraction")))

    # ── 3. 전제 ──────────────────────────────────────────────────────────
    전제목록: list[전제] = []
    미매핑: list[str] = []
    for p in llm출력.get("전제") or []:
        사실 = (p.get("사실") or "").strip()
        근거 = p.get("근거조항")
        if not 근거:
            사유.append(f"전제 '{사실[:24]}' 근거조항 없음 → 폐기")
            continue
        if 근거 not in s맵:
            사유.append(f"전제 '{사실[:24]}' 근거조항 {근거} 가 s맵 밖 → 폐기(환각 인용)")
            continue
        미충족 = p.get("미충족시")
        if 미충족 not in 미충족시_ENUM:
            사유.append(f"전제 '{사실[:24]}' 미충족시 '{미충족}' enum 밖 → 불가로 고정")
            미충족 = "불가"
        경로 = list(p.get("매핑") or [])
        밖 = [x for x in 경로 if x not in 허용경로]
        if 밖:
            # 폐기하지 않는다 — 전제 자체는 유효할 수 있다. F 스키마 결손일 수도 있어
            # `tenant.unmapped_premise` 로 쌓아 사람이 본다. (DB 쓰기는 여기서 안 한다)
            사유.append(f"전제 '{사실[:24]}' 매핑 {밖} 가 F 스키마 밖 → unmapped_premise 대상")
            미매핑.extend(밖)
        전제목록.append(전제(사실=사실, 근거조항=근거, 매핑=경로,
                          미충족시=미충족, 미매핑=bool(밖)))

    # ── 4. 신뢰등급 — 코드가 산정한다. LLM 자칭 금지 ──────────────────────
    # `documents.extraction='vlm'` = 스캔 판독본. CLAUDE.md "A등급 인용 금지"
    vlm = [c.s번호 for c in 인용목록 if c.extraction == "vlm"]
    if not 인용목록:
        신뢰등급 = None
        if 판정 != "판단불가":
            사유.append("인용 0건인데 판정이 판단불가가 아니다 → 판단불가")
            판정 = "판단불가"
    elif vlm:
        신뢰등급 = "B"
        사유.append(f"인용 {vlm} 가 extraction='vlm'(스캔 판독) → A등급 금지, B 로 강등")
    else:
        신뢰등급 = "A"

    # ── 5. verified=false 룰만으로 '가능' 금지 ────────────────────────────
    # `corpus.rules.verified` 주석: 'false 인 룰만으로 "가능" 판정 금지'
    if 룰들 is not None and 판정 == "가능" and 룰들 and not any(r.get("verified") for r in 룰들):
        사유.append("미검수(verified=false) 룰만으로 '가능' → 조건부로 강등")
        판정 = "조건부"

    # ── 5-b. 해야할일 code — check_items 폐쇄 목록 밖이면 폐기 ─────────────
    # code 는 재판정 간 진행상황을 잇는 키다. LLM 이 지어낸 code 를 그대로 두면
    # `tenant.plan_tasks.코드` FK 가 깨지고, 사용자 체크가 다음 판정에 안 붙는다.
    허용코드 = set(체크코드) if 체크코드 is not None else None
    해야할일 = []
    for h in (llm출력.get("해야할일") or []):
        if not isinstance(h, dict) or not h.get("항목"):
            continue
        c = h.get("code")
        if 허용코드 is not None and c is not None and c not in 허용코드:
            사유.append(f"해야할일 code '{c}' 가 check_items 밖 → 폐기")
            continue
        해야할일.append(h)

    # ── 6. 버전스탬프 — 인용 문서의 version. 코드가 채운다 ────────────────
    # 인용된 것만 모은다. s맵 전체를 쓰면 인용하지도 않은 문서의 판본이 화면에 붙는다.
    버전 = sorted({c.version for c in 인용목록 if c.version})
    응답 = 최종응답(
        판정=판정,
        요약=(llm출력.get("요약") or "").strip(),
        해야할일=해야할일,
        인용목록=인용목록, 전제목록=전제목록,
        신뢰등급=신뢰등급,
        버전스탬프=", ".join(버전) or None,
        참조사슬=[],                      # 조립기가 refs 레코드를 넣는다
        강등사유=사유,
        미매핑전제=sorted(set(미매핑)),
    )
    return 응답.to_dict(), 사유


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--f-paths", action="store_true", help="허용 F 경로를 DB 에서 뽑아 출력")
    a = ap.parse_args()
    if a.f_paths:
        for p in sorted(f_경로집합()):
            print(" ", p)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
