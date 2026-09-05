# -*- coding: utf-8 -*-
"""합성 L3 픽스처 — 게이팅 4갈래 · 멀티테넌시 3차 방어를 **실제로** 태운다.

`0831_최종구현.md` §8-E1·E3 · `Agent.md` §3-2 · `RAG.md` §4-1.

━━ 🔴 왜 진짜 기관 규정이 아니라 합성인가 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
정답셋 77문항의 정답근거 82건이 **전부 L1·L2 이고 L3 는 0건**이다. HWPX/HWP 파서를
오늘 지어도 지표에 안 잡히고, 기관마다 문서 구조가 달라 롱테일이다.
그런데 게이팅 4갈래·RLS·`index_guard` 는 **L3 데이터가 없으면 한 줄도 못 태운다** —
지금까지 그 경로는 실행된 적이 없는 코드였다. 그래서 데이터 대신 **경로**를 검증한다.

  ✔ 검증하는 것 : L3 게이팅 4갈래 · 장 필터 · 교차조회 0건 · index_guard 의 L3 거부 ·
                 상위참조 해소와 끊긴 참조 · 조문 → 룰 추출기
  ✘ 검증 못 하는 것 : 실제 기관 문서의 파싱 정확도 (파서가 없다)

━━ 🔴 `출처='테스트픽스처'` 라벨이 필수인 이유 ━━━━━━━━━━━━━━━━━━━━━━━━━━
합성 조문이 실기관 규정과 섞이는 순간, 이 서비스가 사용자에게 **존재하지 않는 규정을
인용**하게 된다. 판정 제품에서 그보다 나쁜 실패는 없다. 라벨은 그걸 막는 유일한 표식이고
`l3룰()` 반환 dict 에도 그대로 실려 A 의 검증층까지 따라간다.
기관명도 실재하지 않는 이름으로 골랐다 — 실제 대학 이름을 쓰면 라벨을 지우는 순간
분간이 안 된다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/archive/seed/seed_l3_fixture.py            # 적재
    PYTHONIOENCODING=utf-8 python scripts/archive/seed/seed_l3_fixture.py --verify   # 검증만
    PYTHONIOENCODING=utf-8 python scripts/archive/seed/seed_l3_fixture.py --drop     # 픽스처 제거
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
import os
import sys
import uuid

if (sys.stdout.encoding or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg                                                  # noqa: E402

import l3_load                                                  # noqa: E402
from index_guard import IndexGuardError, reject_reason          # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")
출처 = "테스트픽스처"

# 🔴 org_id 를 고정한다. 재적재마다 UUID 가 바뀌면 A 의 `judge_cli --org <id>`,
#    회귀 스크립트, `decisions` 로그가 전부 어제와 끊긴다. uuid5 라 어디서 돌려도 같다.
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "suddoe.fixture.l3")
ORG_A = str(uuid.uuid5(_NS, "org-a"))
ORG_B = str(uuid.uuid5(_NS, "org-b"))


# ════════════════════════════════════════════════════════════════════════════
# 픽스처 본체
# ════════════════════════════════════════════════════════════════════════════
# 두 기관을 **precedence 가 서로 다른 사업**으로 골랐다. 하나만 만들면 L3 오버레이가
# 언제 이기고 언제 지는지를 한쪽만 태우게 된다.
#   기관A = 예비·초기창업패키지 → precedence_rules 범위='all', L2 > L3
#           🔴 L3 가 더 엄격해도 L2 가 이긴다. 제13조(멘토링 20만원 < L2 30만원)가 그 재료다
#   기관B = 초격차 스타트업 프로젝트 → L2>L3 조항이 **없다** (L1>L2 unspecified_only 만).
#           대체 경로(상위 우선 + 동일층 엄격)로 가서 L3 오버레이가 실제로 작동한다
#
# 조문 배치는 게이팅 4갈래가 **두 기관에서 서로 다른 비목**에 걸리도록 짰다.
# 교차조회가 새면 기관B 의 재료비가 '불가'(기관A 것)로, 기관A 의 인건비가
# '가능'(기관B 것)으로 나온다 — 누수가 **판정값의 차이로 드러난다.**

기관들: list[dict] = [
    {
        "org_id": ORG_A,
        "기관명": "한국창업대학교 산학협력단",       # 실재하지 않는 이름
        "사업명": ["예비창업패키지", "초기창업패키지"],
        "주소": "(픽스처) 시험용 주소",
        "부서": "창업지원단",
        "문서": {
            "원본파일명": "_테스트픽스처_한국창업대학교_창업사업비_집행지침.hwpx",
            "version": "픽스처 v1.0",
            "시행일": "2026-01-01",
            "status": "active",
            "extraction": "hwpx",
            "파싱품질": "pass",
        },
        "조문": [
            ("제1조", "목적", "제1장 총칙", 1,
             "이 지침은 한국창업대학교 산학협력단이 수행하는 창업지원사업의 "
             "사업비 집행에 관하여 필요한 사항을 정함을 목적으로 한다."),

            ("제2조", "적용범위", "제1장 총칙", 1,
             "이 지침에 정하지 아니한 사항은 「중소기업창업 지원사업 통합관리지침」 "
             "제36조 및 사업별 세부관리기준에 따른다."),

            # ── 제외되어야 할 장 ── 장 필터가 실제로 자르는지 본다
            ("제5조", "복무시간", "제2장 인사 및 복무", 2,
             "직원의 근무시간은 1일 8시간, 주 40시간으로 한다."),
            ("제6조", "연차휴가", "제2장 인사 및 복무", 2,
             "직원의 연차휴가는 「근로기준법」 제60조에 따른다."),

            # ── 게이팅 (3) L3 가 막는다 → need_upper=False ──
            ("제10조", "재료비", "제3장 사업비 집행", 3,
             "① 재료비는 시제품 제작에 직접 소요되는 원부자재의 구입비로 한다. "
             "② 귀금속·보석 및 원석은 구매할 수 없다."),

            # ── 게이팅 (2) 참조만 → need_upper=True + seed_refs ──
            ("제11조", "외주용역비", "제3장 사업비 집행", 3,
             "외주용역비의 집행 기준과 사전심의 절차는 "
             "「중소기업창업 지원사업 통합관리지침」 제38조에 따른다."),

            # ── 게이팅 (4) 🔴 L3 가 "가능" → need_upper=True **강제** ──
            #    L3 단독으로는 절대 확정하면 안 되는 갈래. 이 프로젝트의 핵심 안전장치다
            ("제12조", "기계장치", "제3장 사업비 집행", 3,
             "연구 및 시제품 제작에 사용하는 기계장치는 사업비로 구매할 수 있다."),

            # ── 게이팅 (3) 조건부 + 한도.  🔴 L2(30만원)보다 엄격한 20만원인데
            #    precedence 범위='all' 이라 **L2 가 이긴다** → A 의 PRECEDENCE_FLIP 재료
            ("제13조", "지급수수료", "제3장 사업비 집행", 3,
             "① 멘토링비는 1인 1일 20만원 이내로 한다. "
             "② 제1항의 멘토링비를 집행하려는 경우 단장의 사전승인을 받아야 한다."),

            # 여비 조문 없음 → 게이팅 (1) 미규정

            ("제20조", "정산서류의 제출", "제4장 정산 및 사후관리", 4,
             "사업 종료 후 30일 이내에 정산보고서와 증빙서류를 제출하여야 한다."),
            # 🔴 본문에 '기계장치' 가 나오지만 자산관리 조문이다.
            #    `비목추정` 의 제목 우선 패스가 이걸 기계장치 룰로 오인하지 않아야 한다
            ("제21조", "자산의 등록 및 관리", "제4장 정산 및 사후관리", 4,
             "취득가액 500만원을 초과하는 기계장치는 취득일부터 1개월 이내에 "
             "자산관리대장에 등록하여야 한다."),

            ("제30조", "실험실 안전관리", "제5장 시설 및 안전관리", 5,
             "실험실 책임자는 매 반기 1회 안전점검을 실시하여야 한다."),
        ],
    },
    {
        "org_id": ORG_B,
        "기관명": "대전과학기술원 창업지원단",       # 실재하지 않는 이름
        "사업명": ["초격차 스타트업 프로젝트"],
        "주소": "(픽스처) 시험용 주소",
        "부서": "창업지원팀",
        "문서": {
            "원본파일명": "_테스트픽스처_대전과학기술원_사업비_운영세칙.hwpx",
            "version": "픽스처 v1.0",
            "시행일": "2026-01-01",
            "status": "active",
            "extraction": "hwpx",
            "파싱품질": "pass",
        },
        "조문": [
            ("제1조", "목적", "제1장 총칙", 1,
             "이 세칙은 대전과학기술원 창업지원단의 창업지원사업 사업비 운영에 관한 "
             "사항을 정함을 목적으로 한다."),
            # 🔴 해소되지 않는 참조 — 끊긴 참조를 **업로드 시점에** 알리는 경로를 태운다
            ("제3조", "다른 규정과의 관계", "제1장 총칙", 1,
             "이 세칙에 정하지 아니한 사항은 「대전과학기술원 연구비 관리규정」 "
             "제12조에 따른다."),

            # ── 게이팅 (4) 가능 → need_upper=True 강제 (기관A 에는 인건비 조문이 없다) ──
            ("제7조", "인건비", "제2장 사업비 관리", 2,
             "창업지원사업에 참여하는 학생연구원에게 사업비에서 인건비를 지급할 수 있다."),

            # ── 게이팅 (3) 불가 ──
            ("제8조", "광고선전비", "제2장 사업비 관리", 2,
             "일회성 배포를 목적으로 하는 기념품 및 판촉물의 제작비는 집행할 수 없다."),

            # ── 게이팅 (2) 참조만 · 해소 1건 + 끊긴 참조 1건이 한 조문에 섞인 경우 ──
            ("제9조", "교육훈련비", "제2장 사업비 관리", 2,
             "교육훈련비의 집행 범위는 「중소기업창업 지원사업 통합관리지침」 제44조 및 "
             "「대전과학기술원 연구비 관리규정」 제20조에 따른다."),

            # 재료비 조문 없음 → 게이팅 (1) 미규정 (기관A 에는 '불가' 로 있다 = 누수 탐지기)

            ("제15조", "출퇴근 관리", "제4장 복무", 4,
             "직원은 출퇴근 시각을 전자적 방법으로 기록하여야 한다."),
        ],
    },
]

# 기대값 — 검증이 실제 값을 보고 통과 여부를 판정한다. 코드가 바뀌면 여기가 먼저 깨진다.
기대_게이팅: list[tuple[str, str, str, bool]] = [
    # (org, 비목, 갈래, need_upper)
    (ORG_A, "여비",       "(1) 미규정",  True),
    (ORG_A, "외주용역비", "(2) 참조만",  True),
    (ORG_A, "재료비",     "(3) 불가",    False),
    (ORG_A, "지급수수료", "(3) 조건부",  False),
    (ORG_A, "기계장치",   "(4) 가능",    True),    # 🔴 강제
    (ORG_B, "재료비",     "(1) 미규정",  True),
    (ORG_B, "교육훈련비", "(2) 참조만",  True),
    (ORG_B, "광고선전비", "(3) 불가",    False),
    (ORG_B, "인건비",     "(4) 가능",    True),    # 🔴 강제
]

# 교차조회 탐지기 — 한쪽에만 있는 비목. 새면 값이 달라져서 드러난다.
누수탐지기: list[tuple[str, str]] = [
    (ORG_B, "지급수수료"), (ORG_B, "외주용역비"), (ORG_B, "기계장치"),
    (ORG_A, "인건비"), (ORG_A, "광고선전비"), (ORG_A, "교육훈련비"),
]


def _게이팅(룰: dict | None) -> tuple[str, bool]:
    """`Agent.md` §3-2 의 판정을 그대로 옮긴 참조 구현.

    🔴 이건 B 의 `rule_lookup.l3_게이팅()` 을 대체하지 않는다 — **기대값 대조용**이다.
       B 모듈이 붙으면 아래 `--verify` 가 두 구현을 나란히 돌려 불일치를 잡는다.
    """
    if 룰 is None:
        return "(1) 미규정", True
    if 룰.get("참조만"):
        return "(2) 참조만", True
    if 룰.get("허용") in ("불가", "조건부"):
        return f"(3) {룰['허용']}", False
    if 룰.get("허용") == "가능":
        return "(4) 가능", True            # 🔴 강제
    return "(1) 미규정", True              # 분류 실패는 안전한 쪽으로


# ════════════════════════════════════════════════════════════════════════════
# 적재
# ════════════════════════════════════════════════════════════════════════════
def 적재(conn) -> None:
    with conn.cursor() as cur:
        for org in 기관들:
            cur.execute(
                "INSERT INTO tenant.orgs (org_id, 기관명, 사업명, 주소, 부서) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (org_id) DO UPDATE SET "
                "  기관명=EXCLUDED.기관명, 사업명=EXCLUDED.사업명, "
                "  주소=EXCLUDED.주소, 부서=EXCLUDED.부서",
                (org["org_id"], org["기관명"], org["사업명"], org["주소"], org["부서"]))

            # 문서는 지우고 다시 넣는다 (조문 CASCADE). 🔴 삭제 범위를 반드시
            # `출처='테스트픽스처'` 로 좁힌다 — 같은 기관에 실제 규정이 올라와 있으면
            # 픽스처 재적재가 사용자 문서를 지우게 된다.
            cur.execute("DELETE FROM tenant.l3_documents "
                        " WHERE org_id=%s AND 출처=%s", (org["org_id"], 출처))
            d = org["문서"]
            doc_id = cur.execute(
                "INSERT INTO tenant.l3_documents "
                " (org_id, 원본파일명, version, 시행일, status, extraction, 파싱품질, "
                "  dangling수, 출처) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s) RETURNING doc_id",
                (org["org_id"], d["원본파일명"], d["version"], d["시행일"], d["status"],
                 d["extraction"], d["파싱품질"], 출처)).fetchone()[0]

            for 조번호, 조제목, 장, _장번호, 본문 in org["조문"]:
                cur.execute(
                    "INSERT INTO tenant.l3_articles "
                    " (doc_id, org_id, 조번호, 조제목, 조번호_int, 장, 본문, 페이지) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc_id, org["org_id"], 조번호, 조제목,
                     int(조번호.strip("제조")), 장, 본문, None))

            # 🔴 끊긴 참조는 판정 시점이 아니라 **업로드 시점**에 센다 (CLAUDE.md).
            #    판정 때 세면 사용자는 이미 답을 기다리는 중이고, 고칠 방법이 없다.
            dang = 0
            for _조, _제, 장, _n, 본문 in org["조문"]:
                if not l3_load.사업비관련장(장):
                    continue
                dang += sum(1 for r in l3_load.상위참조(cur, 본문) if not r["해소"])
            cur.execute("UPDATE tenant.l3_documents SET dangling수=%s WHERE doc_id=%s",
                        (dang, doc_id))
            print(f"   {org['기관명']:<22} 조문 {len(org['조문'])} · dangling {dang}")
    conn.commit()


def 제거(conn) -> None:
    with conn.cursor() as cur:
        n = cur.execute("DELETE FROM tenant.l3_documents WHERE 출처=%s", (출처,)).rowcount
        m = cur.execute("DELETE FROM tenant.orgs WHERE org_id = ANY(%s)",
                        ([ORG_A, ORG_B],)).rowcount
    conn.commit()
    print(f"픽스처 제거 — 문서 {n} · 기관 {m}")


# ════════════════════════════════════════════════════════════════════════════
# 검증
# ════════════════════════════════════════════════════════════════════════════
def 검증(conn) -> int:
    """실패 개수를 돌려준다. 0 이 아니면 비0 종료."""
    실패: list[str] = []
    with conn.cursor() as cur:

        # ── 1. 라벨 ──────────────────────────────────────────────────────
        print("\n[1] 픽스처 라벨")
        for org_id, 기관명 in ((ORG_A, "기관A"), (ORG_B, "기관B")):
            for d in l3_load.문서요약(cur, org_id):
                ok = d["출처"] == 출처
                print(f"   {'✔' if ok else '🔴'} {기관명} {d['원본파일명']}  "
                      f"출처={d['출처']} · dangling {d['dangling수']}")
                if not ok:
                    실패.append(f"라벨 누락: {d['원본파일명']}")
        섞임 = cur.execute(
            "SELECT count(*) FROM tenant.l3_documents "
            " WHERE 출처=%s AND org_id <> ALL(%s)", (출처, [ORG_A, ORG_B])).fetchone()[0]
        print(f"   {'✔' if 섞임 == 0 else '🔴'} 픽스처 라벨이 붙은 타 기관 문서 {섞임}건")
        if 섞임:
            실패.append("픽스처 라벨이 픽스처 기관 밖에 있다")

        # ── 2. 장 필터 ───────────────────────────────────────────────────
        print("\n[2] 장 필터 — 사업비 관련 장만 로드")
        for org_id, 기관명 in ((ORG_A, "기관A"), (ORG_B, "기관B")):
            전체 = cur.execute("SELECT count(*) FROM tenant.l3_articles WHERE org_id=%s",
                             (org_id,)).fetchone()[0]
            로드됨 = l3_load.로드(cur, org_id, None)
            제외장 = cur.execute(
                "SELECT DISTINCT 장 FROM tenant.l3_articles WHERE org_id=%s "
                " AND article_id <> ALL(%s)",
                (org_id, [c["article_id"] for c in 로드됨])).fetchall()
            나쁨 = [x[0] for x in 제외장 if l3_load.사업비관련장(x[0])]
            print(f"   {'✔' if not 나쁨 else '🔴'} {기관명} {len(로드됨)}/{전체}조 로드 · "
                  f"제외된 장 {[x[0] for x in 제외장]}")
            if 나쁨:
                실패.append(f"{기관명}: 사업비 관련 장이 잘렸다 {나쁨}")

        # ── 3. 게이팅 4갈래 ──────────────────────────────────────────────
        print("\n[3] 게이팅 4갈래 (Agent.md §3-2)")
        for org_id, 비목, 기대갈래, 기대상위 in 기대_게이팅:
            룰 = l3_load.l3룰(cur, org_id, 비목)
            갈래, 상위 = _게이팅(룰)
            ok = 갈래 == 기대갈래 and 상위 == 기대상위
            기관명 = "A" if org_id == ORG_A else "B"
            부가 = ""
            if 룰:
                if 룰["seed_refs"]:
                    부가 += f" seed_refs={[r['조번호'] for r in 룰['seed_refs']]}"
                if 룰["dangling"]:
                    부가 += f" dangling={룰['dangling']}"
                if 룰["한도_값"]:
                    부가 += f" 한도={룰['한도_값']:,.0f}{룰['한도_단위']}"
            print(f"   {'✔' if ok else '🔴'} 기관{기관명} {비목:<12} {갈래:<10} "
                  f"need_upper={상위}{부가}")
            if not ok:
                실패.append(f"게이팅 불일치 기관{기관명}/{비목}: "
                            f"{갈래}(need_upper={상위}) ≠ {기대갈래}({기대상위})")

        # ── 4. 🔴 교차조회 — 멀티테넌시 1차(WHERE) 방어 ──────────────────
        print("\n[4] 교차조회 — 한쪽에만 있는 비목이 다른 쪽에서 보이면 누수")
        for org_id, 비목 in 누수탐지기:
            룰 = l3_load.l3룰(cur, org_id, 비목)
            기관명 = "A" if org_id == ORG_A else "B"
            ok = 룰 is None
            print(f"   {'✔' if ok else '🔴 TENANT_LEAK'} 기관{기관명} {비목:<12} "
                  f"→ {'None (기대대로)' if ok else 룰}")
            if not ok:
                실패.append(f"TENANT_LEAK 기관{기관명}/{비목}")
        for org_id, 남 in ((ORG_A, ORG_B), (ORG_B, ORG_A)):
            샌것 = [c for c in l3_load.로드(cur, org_id, None)
                   if cur.execute("SELECT 1 FROM tenant.l3_articles "
                                  "WHERE article_id=%s AND org_id=%s",
                                  (c["article_id"], 남)).fetchone()]
            print(f"   {'✔' if not 샌것 else '🔴'} 로드() 결과에 섞인 타 기관 조문 "
                  f"{len(샌것)}건")
            if 샌것:
                실패.append("로드() 가 타 기관 조문을 실었다")

        # ── 5. 🔴 RLS 실효성 — 정책이 있는 것과 먹는 것은 다르다 ─────────
        print("\n[5] RLS 실효성 (3차 방어)")
        실패 += _rls검증(conn)

        # ── 6. index_guard 가 L3 를 판정 인덱스에서 막는가 ───────────────
        print("\n[6] index_guard — L3 는 판정 인덱스(corpus.chunks) 밖이다")
        실패 += _index_guard검증(cur)

        # ── 7. 상위참조 해소 ─────────────────────────────────────────────
        print("\n[7] 상위참조 해소 — seed_refs 가 실재 조문을 가리키는가")
        for org_id, 비목 in ((ORG_A, "외주용역비"), (ORG_B, "교육훈련비")):
            룰 = l3_load.l3룰(cur, org_id, 비목)
            해소 = 룰["seed_refs"] if 룰 else []
            ok = len(해소) >= 1
            print(f"   {'✔' if ok else '🔴'} {비목}: 해소 {len(해소)}건 "
                  f"{[(r['doc_id'][:28], r['조번호']) for r in 해소]}"
                  + (f" · dangling {룰['dangling']}" if 룰 and 룰["dangling"] else ""))
            if not ok:
                실패.append(f"seed_refs 미해소: {비목} — 게이팅(2)가 상위를 못 짚는다")

        # ── 8. 미분류 조문 (무음 결손) ───────────────────────────────────
        print("\n[8] 미분류 조문 — 비목은 잡혔는데 추출기가 포기한 것")
        for org_id, 기관명 in ((ORG_A, "기관A"), (ORG_B, "기관B")):
            미 = l3_load.미분류(cur, org_id)
            print(f"   {'✔' if not 미 else '⚠️'} {기관명}: {len(미)}건 "
                  + (str(미) if 미 else ""))

        # ── 9. 🔴 게스트·잘못된 org_id — 연쇄 실패를 만들면 안 된다 ──────
        print("\n[9] 게스트/비정상 org_id — L3 없음으로 닫히고 커서가 살아 있는가")
        실패 += _org입력검증(cur)

    print("\n" + "═" * 74)
    if 실패:
        print(f"🔴 실패 {len(실패)}건")
        for f in 실패:
            print("   ·", f)
    else:
        print("✔ 전 항목 통과")
    return len(실패)


def _org입력검증(cur) -> list[str]:
    """🔴 정답셋 77문항에는 `org_id` 가 없다. 게스트 경로가 판정을 죽이면 안 된다.

    `org_id` 는 `WHERE org_id = %s` 로 UUID 컬럼에 바로 들어간다. 'guest' 같은 값이
    오면 Postgres 가 `invalid input syntax for type uuid` 를 던지고 **그 트랜잭션이
    통째로 abort 된다.** 그러면 A 의 오케스트레이터가 같은 커서로 하는 이후 조회가
    전부 `current transaction is aborted` 로 죽는다 — L3 하나 없는 것이 검색·룰·
    인용 검증까지 끌고 내려가는 연쇄 실패가 된다.
    계약 §2-3 은 "모든 실패의 기본값은 판단불가" 다. 잘못된 org_id 는 "L3 없음"
    (= 상위를 본다) 으로 닫혀야지 판정을 죽이는 사건이 아니다.

    그래서 반환값만 보지 않고 **예외 뒤에 커서가 살아 있는지**까지 본다.
    """
    실패: list[str] = []
    for 이름, org in (("None(게스트)", None), ("빈 문자열", ""), ("공백", " "),
                     ("비UUID 문자열", "guest"),
                     ("없는 UUID", "00000000-0000-0000-0000-000000000000")):
        try:
            a = l3_load.로드(cur, org, None)
            b = l3_load.l3룰(cur, org, "재료비")
            d = l3_load.문서요약(cur, org)
            m = l3_load.미분류(cur, org)
            살아있나 = cur.execute("SELECT 1").fetchone()[0] == 1   # 트랜잭션 abort 검사
            ok = a == [] and b is None and d == [] and m == [] and 살아있나
            print(f"   {'✔' if ok else '🔴'} {이름:<16} 로드 {len(a)}조 · "
                  f"l3룰 {b} · 문서 {len(d)} · 커서생존 {살아있나}")
            if not ok:
                실패.append(f"게스트 경로 {이름}: L3 없음으로 안 닫힌다")
        except Exception as e:
            print(f"   🔴 {이름:<16} 예외 {type(e).__name__}: "
                  f"{str(e).splitlines()[0][:60]}")
            실패.append(f"게스트 경로 {이름} 가 예외를 던진다 — 판정 연쇄 실패")
    # 정상 경로가 이 가드로 망가지지 않았는지
    n = len(l3_load.로드(cur, ORG_A, None))
    print(f"   {'✔' if n == 8 else '🔴'} 정상 org_id 는 그대로 {n}조 (기대 8)")
    if n != 8:
        실패.append(f"가드가 정상 경로를 깨뜨렸다: {n}조")
    return 실패


def _rls검증(conn) -> list[str]:
    """정책이 걸려 있어도 **소유자 접속이면 우회된다.** 그걸 실제로 잰다.

    `postgres` 는 `l3_articles` 의 소유자이고 `FORCE ROW LEVEL SECURITY` 가 없다 →
    정책은 존재하지만 한 줄도 적용되지 않는다. "RLS 켜 있음" 을 방어로 세면
    실제로는 `WHERE org_id` 하나에 전부 걸려 있는 상태다.
    저권한 롤을 잠깐 만들어 **정책 자체는 옳다**는 것과 **현재 롤에서는 안 먹는다**는 것을
    분리해서 보고한다. 어느 쪽인지 모르면 못 고친다.
    """
    실패: list[str] = []
    with conn.cursor() as cur:
        forced, enabled = cur.execute(
            "SELECT relforcerowsecurity, relrowsecurity FROM pg_class "
            " WHERE oid = 'tenant.l3_articles'::regclass").fetchone()
        소유자 = cur.execute(
            "SELECT pg_get_userbyid(relowner) = current_user "
            "  FROM pg_class WHERE oid='tenant.l3_articles'::regclass").fetchone()[0]
        우회 = bool(소유자 and not forced)
        print(f"   정책 ENABLE={enabled} · FORCE={forced} · 접속롤이 소유자={소유자}")
        print(f"   {'🔴' if 우회 else '✔'} 현재 접속에서 RLS "
              f"{'우회됨 — 3차 방어가 꺼져 있다' if 우회 else '적용됨'}")

        # 우회 여부와 무관하게 **정책 자체가 옳은지**는 따로 잰다
        롤 = "suddoe_rls_probe"
        try:
            cur.execute(f'DROP OWNED BY {롤}')
        except Exception:
            conn.rollback()
        try:
            cur.execute(f'DROP ROLE IF EXISTS {롤}')
            cur.execute(f'CREATE ROLE {롤} NOLOGIN')
            cur.execute(f'GRANT USAGE ON SCHEMA tenant TO {롤}')
            cur.execute(f'GRANT SELECT ON tenant.l3_articles, tenant.l3_documents, '
                        f'tenant.orgs TO {롤}')
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"   ⚠️ 검증롤 생성 실패 — 정책 실효성 미측정: {e}")
            return 실패 + ["RLS 정책 실효성 미측정"]

        try:
            with conn.cursor() as c2:
                c2.execute(f'SET LOCAL ROLE {롤}')
                c2.execute("SELECT set_config('app.org_id', %s, true)", (ORG_A,))
                내것 = c2.execute("SELECT count(*) FROM tenant.l3_articles "
                                "WHERE org_id=%s", (ORG_A,)).fetchone()[0]
                남의것 = c2.execute("SELECT count(*) FROM tenant.l3_articles "
                                  "WHERE org_id=%s", (ORG_B,)).fetchone()[0]
                필터없이 = c2.execute("SELECT count(*) FROM tenant.l3_articles").fetchone()[0]
            conn.rollback()
        finally:
            with conn.cursor() as c3:
                c3.execute(f'REVOKE ALL ON tenant.l3_articles, tenant.l3_documents, '
                           f'tenant.orgs FROM {롤}')
                c3.execute(f'REVOKE USAGE ON SCHEMA tenant FROM {롤}')
                c3.execute(f'DROP ROLE IF EXISTS {롤}')
            conn.commit()

        ok = 남의것 == 0 and 내것 > 0 and 필터없이 == 내것
        print(f"   {'✔' if ok else '🔴'} 저권한 롤 + app.org_id=기관A → "
              f"내 조문 {내것} · 타 기관 조문 {남의것} · WHERE 없이 {필터없이}")
        if 남의것 != 0:
            실패.append(f"RLS 정책이 교차조회를 못 막는다 — 타 기관 {남의것}행 보임")
        if 필터없이 != 내것:
            실패.append("RLS 정책이 WHERE 없는 전체조회를 못 막는다")
        if 우회:
            print("   → 🔴 정책은 옳으나 앱 접속 롤(소유자)에서 우회된다. "
                  "오늘의 실질 방어는 (a) 별도 테이블 (b) l3_load 의 WHERE org_id 다")
    return 실패


def _index_guard검증(cur) -> list[str]:
    """L3 는 `corpus.chunks` 에 들어가면 안 된다.

    `CLAUDE.md`: *판정 검색은 항상 `layer IN ('L1','L2')`, L3 는 `tenant.l3_articles`
    별도 경로 — 다른 테이블이라 누수가 구조적으로 불가능하다.*
    그 "구조적으로 불가능" 은 **L3 가 chunks 에 절대 안 들어갈 때만** 참이다.
    """
    실패: list[str] = []
    사유 = reject_reason("2026_Finance_DATA_FOR_RAG/기관규정/OO대_사업비지침.hwpx", "L3")
    막힘 = 사유 is not None
    print(f"   {'✔' if 막힘 else '🔴'} reject_reason(layer='L3') → {사유!r}")
    if not 막힘:
        실패.append("index_guard 가 layer='L3' 를 막지 않는다 "
                    "(BLOCKED 로 A 에게 보고 · 아래 실측 참조)")
    for layer in ("L4", "L5"):
        if reject_reason("x.pdf", layer) is None:
            실패.append(f"index_guard 가 layer={layer} 를 막지 않는다")
    print(f"   ✔ L4/L5 차단 유지 · 경로 블랙리스트 "
          f"{'✔' if reject_reason('archive/x.pdf') else '🔴'}")

    # 실제로 뚫려 있는지 — chunks 에 L3 가 이미 있는가
    n = cur.execute("SELECT count(*) FROM corpus.chunks WHERE layer NOT IN ('L1','L2')"
                    ).fetchone()[0]
    m = cur.execute("SELECT count(*) FROM corpus.documents "
                    " WHERE layer='L3' AND index_target").fetchone()[0]
    print(f"   {'✔' if n == 0 else '🔴'} corpus.chunks 의 L1·L2 아닌 청크 {n}건 · "
          f"index_target 인 L3 문서 {m}건")
    if n or m:
        실패.append("판정 인덱스에 L1·L2 아닌 것이 들어 있다")
    return 실패


def main() -> None:
    ap = argparse.ArgumentParser(description="합성 L3 픽스처")
    ap.add_argument("--verify", action="store_true", help="적재 없이 검증만")
    ap.add_argument("--drop", action="store_true", help="픽스처 제거")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        if a.drop:
            제거(conn)
            return
        if not a.verify:
            print(f"픽스처 적재 (출처='{출처}')")
            적재(conn)
        n = 검증(conn)
    sys.exit(1 if n else 0)


if __name__ == "__main__":
    main()
