# -*- coding: utf-8 -*-
"""`공유받은 파일/증빙서류 종합.csv` -> `corpus.evidence_sources`.

전처리가 없다. CSV 124행이 그대로 한 행씩 들어간다.
판정에 쓰이지 않고 화면 안내(발급처 링크·툴팁)에만 쓰이므로 검수 통과 조건도 없다.

🔴 **비목 문자열을 여기서 정규화하지 않는다.**
   CSV 의 `해당 비목` 은 "재료비, 외주용역비, TIPS-연구재료비(시험제품·설비 등 제작비)" 처럼
   쉼표로 이어져 있는데, 괄호 안에도 쉼표가 들어간다. 단순 split 은 항목을 쪼갠다.
   그래서 **괄호 깊이를 보며 쪼개되, 값 자체는 원문 그대로 둔다** —
   `rules.비목`(용어 사전 enum 10종)과의 대조는 별칭 매핑 작업에서 따로 한다.
   여기서 미리 맞추려 들면 대조가 필요한 사실이 가려진다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/load_evidence.py
    PYTHONIOENCODING=utf-8 python scripts/load_evidence.py --diff   # rules.증빙 과 대조만
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "공유받은 파일" / "증빙서류 종합.csv"
DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")


def 쪼개기(s: str) -> list[str]:
    """쉼표로 나누되 괄호 안의 쉼표는 무시한다."""
    out, buf, depth = [], [], 0
    for ch in s or "":
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if x]


# ════════════════════════════════════════════════════════════════════════════
# 해당비목 → 기준 문서 비목 매핑 (2026-08-31 3차)
# ════════════════════════════════════════════════════════════════════════════
# 기준 문서는 `_비목_어휘집.json` 의 **guided_json_enum 10종**이다 (`비목` 필드가 아니다 —
# 그쪽은 원문 표기라 "기계장치, 공구·기구" 처럼 조인 키로 못 쓴다).
#
# CSV 의 `해당 비목` 은 세 계통이 섞여 있다. 억지로 한 축에 밀어 넣지 않는다.
#   (1) 창업패키지 비목        → 기준 문서 10종으로 매핑
#   (2) 지급수수료 **세목**    → 기준 문서 `지급수수료` 로 접는다 (세목은 별도 축이다. §세목 참조)
#   (3) TIPS·국가연구개발 비목 → 🔴 매핑하지 않는다 (아래 사유)
#
# 🔴 TIPS 를 창업패키지 10종에 매핑하지 않는 이유
#   · 위임 계통이 다르다. TIPS 비목(연구재료비·연구활동비·연구수당·간접비)은
#     「국가연구개발혁신법」 계통이고, 10종은 「중소기업창업 지원법」 통합관리지침
#     제36조 표-10 계통이다. 이름이 겹쳐 보여도(재료비 vs 연구재료비) 정의·증빙·한도가 다르다.
#   · corpus.rules 에 TIPS 룰이 없다 (비목 체계 상이 + 우선순위 조항 부재로 제외).
#     evidence 쪽만 매핑하면 rules 에 없는 비목으로 조인해 **무음 0행**이 된다.
#   · 처리: 정본값 None + 분류 태그. **원본 `해당비목` 은 그대로 둔다.**
#     별도 값(예: "TIPS:재료비")으로 만들지 않는다 — enum 10종에 없는 값이 조인 축에
#     섞이면 guided_json 생성 때 다시 문제가 된다.
#
# ⚠️ 이 함수는 DB 를 쓰지 않는다. 정규화 결과를 적재하려면 컬럼이 둘 필요하다
#    (`해당비목_정본 TEXT[]` · `해당비목_분류 TEXT[]`). 스키마는 이 세션 소유가 아니라
#    ALTER 하지 않았다 — 오너 결정 대기. `--map` 이 검수용 산출물만 만든다.

_ROOT = ROOT
_어휘집 = _ROOT / "2026_Finance_DATA_FOR_RAG" / "_비목_어휘집.json"

# (2) 지급수수료 세목 — 접두사형("지급수수료-멘토링비")과 민낯형("멘토링비")이 섞여 있다.
#     민낯형이 창업패키지 세목인지 TIPS 비목인지는 CSV `패키지` 열로 갈렸다:
#     아래 9종은 전부 "7개 패키지 공통" 또는 창업패키지 열거였고,
#     회의비·출장비·연구재료비·간접비(총괄)·국제공동연구개발비는 "TIPS" 단독이었다.
_지급수수료_세목 = {
    "기술이전비", "학회·세미나 참가비", "전시회·박람회 참가비", "시험·인증비",
    "멘토링비", "기자재임차비", "장비 수리비", "사무실임차료", "운반비", "보험료",
    "보관료", "회계감사비", "세무·회계비", "법인설립비", "기술보호비", "법률컨설팅비",
}

# (1) 표기 차이 — 원문 표기를 enum 으로. 새로 만들지 않고 용어 사전 `비목` 필드와 대조해 얻었다.
_비목_표기별칭 = {
    "특허권 등 무형자산 취득비": "특허권등무형자산취득비",
    "특허권등 무형자산 취득비": "특허권등무형자산취득비",
    "기계장치, 공구·기구": "기계장치",
    "기계장치(공구·기구, 비품, SW 등)": "기계장치",
    "창업 활동비": "창업활동비",
}

# (3-b) 창업패키지 문서에 나오지만 **창업기업 비목이 아닌** 값.
#       용어 사전에서 적용대상=주관기관이거나, 애초에 비목 축이 아닌 메타 행이다.
_비목아님 = {
    "사업비 구성": "비목이 아니라 사업비 구성비(정부지원/자기부담) 규정",
    "자기부담 현물": "비목이 아니라 자기부담사업비의 현물 계상 규정",
    "기타-비교견적서(전 비목 공통 규정)": "전 비목 공통 규정 — 특정 비목에 속하지 않는다",
}
_주관기관_비목 = {
    # 용어 사전 층=사업별·적용대상=주관기관. 창업기업 10종에 없다.
    "회의비", "일반수용비", "일반 수용비", "창업프로그램운영비", "창업 프로그램 운영비",
    "프로그램운영비", "운영비", "사업운영비", "업무추진비", "홍보비", "시설유지비",
    "자산취득비", "자산취득 및 시설유지비",
}

# (3-a) TIPS·국가연구개발 계통. "TIPS" 로 시작하는 값 + 접두사 없는 R&D 비목 이름.
_RND_비목 = {
    "연구재료비", "연구시설·장비비", "연구활동비", "연구수당", "연구실운영비",
    "연구인력지원비", "연구근접지원인력 인건비", "소프트웨어활용비", "외부전문기술활용비",
    "종합사업관리비", "해외연구자유치지원비", "지식재산창출활동비", "국제공동연구개발비",
    "학생인건비", "내부인건비", "외부인건비", "출장비", "간접비",
}


def 정본_비목_enum() -> set[str]:
    """`_비목_어휘집.json` 의 guided_json_enum 10종. 비목 문자열의 유일한 기준 문서."""
    import json
    with _어휘집.open(encoding="utf-8") as f:
        v = json.load(f)
    if v.get("enum_검수대기"):
        print(f"⚠️ 어휘집 enum_검수대기 {len(v['enum_검수대기'])}종 — 정본 확정 전이다")
    return set(v["guided_json_enum"])


def _앞머리(s: str) -> str:
    """괄호 주석과 접두사를 떼고 핵심 이름만 남긴다. 매핑 판단에만 쓴다."""
    s = s.strip()
    for sep in ("-", "—"):
        if sep in s and not s.startswith(sep):
            s = s.split(sep, 1)[1].strip()
            break
    return s.split("(")[0].strip()


def 정본비목(원문: str, 정본: set[str] | None = None) -> tuple[str | None, str]:
    """CSV `해당 비목` 값 하나 → (기준 문서 enum 또는 None, 분류 태그).

    None 을 돌려주는 것은 실패가 아니라 **판정 축이 다르다는 사실**이다.
    호출부는 None 을 버리지 말고 원문과 분류를 함께 보존해야 한다.

    분류: 기준 문서 | 표기차이 | 지급수수료_세목 | R&D계통 | 주관기관비목 | 비목아님 | 미분류
    """
    정본 = 정본 if 정본 is not None else 정본_비목_enum()
    s = (원문 or "").strip()
    if not s:
        return None, "비목아님"

    # TIPS 는 접두사만으로 확정한다 ("TIPS-연구재료비", "TIPS 인건비(...)" 둘 다).
    if s.upper().startswith("TIPS"):
        return None, "R&D계통"
    if s in 정본:
        return s, "정본"
    if s in _비목_표기별칭:
        return _비목_표기별칭[s], "표기차이"
    if s in _비목아님:
        return None, "비목아님"

    핵 = _앞머리(s)
    if 핵 in _지급수수료_세목 or s in _지급수수료_세목:
        return "지급수수료", "지급수수료_세목"
    if 핵 in _RND_비목 or s in _RND_비목:
        return None, "R&D계통"
    if 핵 in _주관기관_비목 or s in _주관기관_비목:
        return None, "주관기관비목"
    if 핵 in 정본:
        return 핵, "표기차이"
    if 핵 in _비목_표기별칭:
        return _비목_표기별칭[핵], "표기차이"
    return None, "미분류"


def 읽기() -> list[dict]:
    if not CSV_PATH.exists():
        sys.exit(f"CSV 가 없다: {CSV_PATH}")
    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ── 패키지 정규화 ──────────────────────────────────────────────────────
# 🔴 CSV `패키지` 열은 사람이 쓴 산문이라 `programs.사업명` 과 조인되지 않는다.
#    실측(2026-09-01): 그대로 넣으면 124행 중 50행(40.3%)만 붙는다.
#      · '초격차 스타트업'        42행 — 기준 문서는 '초격차 스타트업 프로젝트' (표기차)
#      · '7개 패키지 공통'        23행 — 사업 열거가 아니라 문장
#      · '7개 패키지 공통 + TIPS'  7행
#      · '초격차 스타트업 + TIPS'  2행 — 배열 마지막 원소에 " + TIPS" 가 붙어 오염
#    해소 사전은 **문자열로 적지 않고 `corpus.programs` 에서 만든다.** 여기에 이름을
#    다시 쓰면 표기가 갈려 같은 사고가 재발한다 — 별칭도 programs.별칭 이 기준 문서이다.
def _패키지_해소기(cur):
    """corpus.programs 로 만든 «CSV 표기 -> 사업명 리스트» 해소 함수를 돌려준다."""
    cur.execute('SELECT "사업명", "별칭", "비목계통", "활성" FROM corpus.programs')
    정본: dict[str, str] = {}
    창업, RND = [], []
    for 사업명, 별칭, 계통, 활성 in cur.fetchall():
        정본[사업명] = 사업명
        for a in (별칭 or []):
            정본[a] = 사업명
        if 활성:
            (창업 if 계통 == "창업" else RND).append(사업명)
    # "7개 패키지" 는 창업 계통 전부를 가리킨다. 7이 아니면 전제가 깨진 것이니 멈춘다.
    if len(창업) != 7:
        sys.exit(f"🔴 창업 계통 사업이 {len(창업)}개다 — CSV 의 '7개 패키지 공통' 을 펼 수 없다")

    def 해소(tok: str) -> list[str] | None:
        t = tok.strip()
        if t.endswith("+ TIPS"):                       # 배열 원소에 붙어 온 꼬리
            앞 = 해소(t[: -len("+ TIPS")].strip())
            return None if 앞 is None else 앞 + RND
        if t in ("7개 패키지 공통", "7개 패키지"):
            return list(창업)
        return [정본[t]] if t in 정본 else None

    def 정규화(toks: list[str]) -> list[str]:
        out: list[str] = []
        for t in toks:
            r = 해소(t)
            if r is None:
                # 조용히 넘기면 조인이 다시 40% 로 떨어진다. 새 표기가 들어오면 멈춘다.
                sys.exit(f"🔴 패키지 표기를 programs 로 못 푼다: {t!r} — programs.별칭 에 등록하고 다시 돌려라")
            for x in r:
                if x not in out:                       # 순서 보존 dedupe
                    out.append(x)
        return out

    return 정규화


def 적재(rows: list[dict]) -> None:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # 한 트랜잭션. 중간 상태가 화면에 노출되지 않는다.
            정규화 = _패키지_해소기(cur)          # TRUNCATE 전에 programs 를 읽는다
            cur.execute("TRUNCATE corpus.evidence_sources;")
            with cur.copy("""COPY corpus.evidence_sources
                             (증빙명, 해당비목, 패키지, 세부정보, 발급처) FROM STDIN""") as cp:
                for r in rows:
                    cp.write_row((
                        r["증빙서류이름"].strip(),
                        쪼개기(r.get("해당 비목", "")),   # 원문 보존 — 되돌릴 원본이다
                        정규화(쪼개기(r.get("패키지", ""))),
                        (r.get("세부 정보") or "").strip() or None,
                        (r.get("관련 서식·시스템") or "").strip() or None,
                    ))
        conn.commit()
        # 🔴 TRUNCATE 가 파생 2컬럼을 비웠다. 같은 트랜잭션 밖에서 곧바로 되채운다 —
        #    이걸 빼면 재적재할 때마다 해당비목_정본 이 조용히 사라진다.
        _n, _빔 = 파생컬럼_채우기()
        print(f"파생 컬럼 재계산 {_n}행 · 해당비목_정본 빈 배열 {_빔}행 (R&D·주관기관·비목아님)")
        n, 비목없음, 발급처없음 = conn.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE 해당비목 = '{}'),
                   count(*) FILTER (WHERE 발급처 IS NULL)
              FROM corpus.evidence_sources""").fetchone()
    print(f"적재 {n}행 · 비목 비어있음 {비목없음} · 발급처 비어있음 {발급처없음}")


def 대조() -> None:
    """CSV 증빙명 vs rules.증빙. 별칭 매핑이 얼마나 필요한지 실측한다."""
    with psycopg.connect(DSN) as conn:
        csv_names = {r[0] for r in conn.execute(
            "SELECT 증빙명 FROM corpus.evidence_sources").fetchall()}
        rule_names = {r[0] for r in conn.execute(
            "SELECT DISTINCT e FROM corpus.rules, unnest(증빙) e").fetchall()}

    일치 = csv_names & rule_names
    미일치 = sorted(rule_names - 일치)
    복합 = [x for x in 미일치 if ":" in x or "·" in x]
    단순 = [x for x in 미일치 if x not in 복합]

    print(f"CSV {len(csv_names)}종 · rules.증빙 {len(rule_names)}종")
    print(f"  완전일치     {len(일치)}종 ({len(일치)/max(len(rule_names),1)*100:.0f}%)")
    print(f"  미일치       {len(미일치)}종")
    print(f"    (b) 복합 문자열 — 낱개로 쪼개 재적재 필요  {len(복합)}종")
    for x in 복합:
        print(f"        {x[:78]}")
    print(f"    (a) 표기 차이 — 별칭 매핑으로 해결        {len(단순)}종")
    for x in 단순:
        print(f"        {x[:78]}")


def 파생컬럼_채우기() -> tuple[int, int]:
    """`해당비목` 원문 -> `해당비목_정본`·`해당비목_분류` 를 다시 계산해 적재한다.

    🔴 이 함수가 없으면 `적재()` 의 TRUNCATE 가 파생 2컬럼을 비우고 아무도 안 채운다.
       실제로 2026-09-01 에 그렇게 날아갔다 (52행 -> 124행 전부 빈 배열). 컬럼은
       `02_frontend.sql:64` 에 있는데 쓰기 경로만 코드에 없었던 것이다.

    ⚠️ **이 함수의 산출은 손으로 채워져 있던 52행과 같지 않다 — 125행 중 73행이다.**
       분류기가 사람이 비워둔 것까지 매핑하기 때문이고, 재계산과 DB 저장값의 불일치는
       0 이다(2026-09-01 실측). 즉 여기서 보증되는 것은 «CSV 원문에서 결정적으로
       재생산된다» 이지 «손으로 채운 원본과 일치한다» 가 아니다. 52 를 복구 성공의
       기준선으로 삼지 마라.

    `해당비목` 원문은 건드리지 않는다 — 되돌릴 원본이다.
    빈 `해당비목_정본` 은 결손이 아니다: TIPS·R&D 계통과 주관기관 비목은 창업 10종에
    매핑하지 않기로 한 결정의 결과다 (`audit_db.py:123` 이 같은 근거를 든다).
    """
    정본 = 정본_비목_enum()
    with psycopg.connect(DSN) as conn:
        rows = conn.execute("SELECT 증빙명, 해당비목 FROM corpus.evidence_sources").fetchall()
        갱신 = []
        for 증빙명, 해당비목 in rows:
            매핑, 분류 = [], []
            for 원문 in (해당비목 or []):
                v, tag = 정본비목(원문, 정본)
                if v and v not in 매핑:
                    매핑.append(v)
                if tag not in 분류:
                    분류.append(tag)
            갱신.append((매핑, 분류, 증빙명))
        with conn.cursor() as cur:
            cur.executemany("""UPDATE corpus.evidence_sources
                                  SET 해당비목_정본 = %s, 해당비목_분류 = %s
                                WHERE 증빙명 = %s""", 갱신)
        conn.commit()
        빔 = sum(1 for m, _, _ in 갱신 if not m)
    return len(갱신), 빔


def 비목매핑(적용: bool = False) -> None:
    """해당비목 → 기준 문서 10종 매핑 커버리지 리포트. 기본은 **DB 를 쓰지 않는다.**

    `해당비목` 은 CSV 원문 그대로 남는다 (원본 보존).
    `적용=True` 면 파생 2컬럼(`해당비목_정본`·`해당비목_분류`)만 다시 채운다.
    """
    import collections
    if 적용:
        n, 빔 = 파생컬럼_채우기()
        print(f"파생 2컬럼 재적재 {n}행 · 해당비목_정본 빈 배열 {빔}행 (R&D·주관기관·비목아님)")

    정본 = 정본_비목_enum()
    with psycopg.connect(DSN) as conn:
        rows = conn.execute(
            "SELECT 증빙명, 해당비목 FROM corpus.evidence_sources ORDER BY 증빙명").fetchall()

    분류별 = collections.defaultdict(list)   # 태그 -> [(원문, 기준 문서)]
    본 = set()
    행_커버 = 행_부분 = 행_없음 = 0
    for _, items in rows:
        결과 = [정본비목(i, 정본) for i in (items or [])]
        for i, (v, tag) in zip(items or [], 결과):
            if (i, v) not in 분류별[tag]:
                분류별[tag].append((i, v))
            본.add(i)
        매핑수 = sum(1 for v, _ in 결과 if v)
        if not 결과 or 매핑수 == 0:
            행_없음 += 1
        elif 매핑수 == len(결과):
            행_커버 += 1
        else:
            행_부분 += 1

    print(f"evidence_sources {len(rows)}행 · 해당비목 distinct {len(본)}종")
    print(f"  전부 매핑됨 {행_커버}행 · 일부만 {행_부분}행 · 하나도 안 됨 {행_없음}행\n")

    순서 = ["정본", "표기차이", "지급수수료_세목", "R&D계통", "주관기관비목", "비목아님", "미분류"]
    for tag in 순서:
        vals = 분류별.get(tag, [])
        if not vals:
            continue
        표 = "🔴 " if tag == "미분류" else "   "
        print(f"{표}[{tag}] {len(vals)}종")
        for 원문, v in sorted(vals):
            print(f"      {원문:<44} -> {v if v else '(매핑 없음 — 원본 보존)'}")
        print()

    미 = 분류별.get("미분류", [])
    if 미:
        print(f"🔴 미분류 {len(미)}종 — 매핑 규칙을 보강하거나 사람이 판단해야 한다")
    else:
        print("미분류 0종 — 모든 값이 분류됐다 (분류 != 매핑. 위 표를 보라)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true", help="적재하지 않고 rules.증빙 과 대조만")
    ap.add_argument("--map", action="store_true",
                    help="적재하지 않고 해당비목 -> 정본 10종 매핑 커버리지만 (DB 쓰기 없음)")
    ap.add_argument("--map-apply", action="store_true",
                    help="해당비목_정본·해당비목_분류 파생 2컬럼만 다시 채운다")
    a = ap.parse_args()
    if a.map_apply:
        비목매핑(적용=True)
        return
    if a.map:
        비목매핑()
        return
    if not a.diff:
        적재(읽기())
    대조()


if __name__ == "__main__":
    main()
