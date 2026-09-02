# -*- coding: utf-8 -*-
"""추출된 기관 명부를 `tenant.orgs` · `tenant.org_programs` 에 적재한다.

🔴 --dry 가 기본이다. `--실행` 을 줘야 쓴다.

■ 기관 동일성 — **접지 않는다**
  `org_id = uuid5(NS, 공백만 접은 기관명)`. 그 이상은 접지 않는다.
  「국립순천대학교」와 「순천대학교」는 전화가 같아 같은 기관이 맞지만(2024 국립 접두
  일괄 개칭), **접으면 개칭 이력이 사라진다.** 연도가 이미 그걸 기록하고 있다 —
  2023·24 는 순천대학교, 25·26 은 국립순천대학교로 남는 게 사실에 가깝다.
  그리고 기관 선택 화면은 `v_기관명부_최신`(사업별 최신 연도)을 읽으므로
  **개칭 전 이름은 애초에 목록에 안 나온다.** 접을 이유가 없다.

  🔴 접는 것은 되돌릴 수 없고 접힌 것은 표에 안 남는다. 2026-09-02 에 두 번 당했다
  (초기창업 20→14 · 초격차 16→13). 남기는 쪽으로 틀린다.

■ 협력기관은 **넣지 않는다** (2,420건 제외)
  근거 셋 (2026-09-02 원문 실측):
    ① 정의가 「보육공간 제공 또는 운영사와 함께 보육·투자·기술개발 지원」이다
    ② 2,420건 전부 연락처·주소가 **없다**. 이름뿐이다
    ③ 사업비 「사전승인」의 주어는 원문 전체에서 **전문기관 또는 업무지원기관**이다.
       협력기관이 승인·집행 주체로 나오는 문장은 0건
  우리 표는 «지출을 승인해줄 기관» 의 명부다. 협력기관은 그 자리에 안 선다 —
  운영사의 **속성**이지 기관 명부가 아니다. 필요해지면 별도 표로 만든다.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# uuid5 라 어디서 돌려도 같은 값이 나온다. 재적재해도 org_id 가 안 바뀐다.
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "suddoe.org")

# 폴더명·추출기 표기 → corpus.programs 정본.
# 🔴 여기 없는 이름이 오면 **죽는다.** 조용히 건너뛰지 않는다 — 사업 하나가
#    통째로 빠지는데 아무도 모르는 게 제일 나쁘다.
정본 = {
    "예비창업패키지": "예비창업패키지",
    "초기창업패키지": "초기창업패키지",
    "재도전성공패키지": "재도전성공패키지",
    "창업도약패키지": "창업도약패키지",
    "창업중심대학": "창업중심대학",
    "초격차 스타트업 프로젝트": "초격차 스타트업 프로젝트",
    "초격차": "초격차 스타트업 프로젝트",
    "모두의 창업 프로젝트": "모두의 창업 프로젝트",
    "TIPS": "TIPS",
}

제외역할 = {"협력기관"}          # 위 docstring ■ 참조

# 🔴 추출이 틀린 게 확인된 건. 두 세션이 독립으로 원문을 읽어 같은 결론을 냈다.
#    (기관명이 표 셀 안에서 hp:p 두 개로 쪼개져 앞부분이 유실됐다)
정정 = {
    ("초격차 스타트업 프로젝트", 2025, "02-880-8741"):
        "서울대학교 시스템반도체산업진흥센터",
}


def 키(이름: str) -> str:
    """org_id 의 열쇠. 🔴 공백만 접는다. 법인 접두어·꼬리는 건드리지 않는다."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", 이름))


def org_id(이름: str) -> str:
    return str(uuid.uuid5(_NS, 키(이름)))


def 연도출처(v) -> str:
    """추출기마다 다르게 적었다. 표의 CHECK 세 값 중 하나로 좁힌다."""
    s = str(v or "")
    if "대상년도" in s or "본문" in s:
        return "대상년도"
    if "수집" in s:
        return "수집기록"
    return "파일명"


def 읽기(경로들: list[str]) -> list[dict]:
    out = []
    for p in 경로들:
        d = json.loads(io.open(p, encoding="utf-8").read())
        print(f"   {os.path.basename(p)}  {len(d)}건")
        out += d
    return out


def 다듬기(레코드: list[dict]) -> tuple[list[dict], Counter, list[dict]]:
    """정본 매핑 · 역할 채우기 · 정정 적용 · 제외. 버린 것과 결손을 센다."""
    버림 = Counter()
    결손: list[dict] = []
    out = []
    for r in 레코드:
        역할 = r.get("역할") or "주관기관"
        if 역할 in 제외역할:
            버림[f"제외역할:{역할}"] += 1
            continue
        사업 = r.get("사업")
        if 사업 not in 정본:
            raise SystemExit(f"🔴 정본에 없는 사업명: {사업!r} — 매핑을 추가하거나 원인을 밝혀라")
        # 🔴 기관명이나 연도가 비면 **버리지 않고 멈춘다.** 이건 「없는 기관」이 아니라
        #    「추출이 실패한 기관」이다. 조용히 빠지면 명부에서 한 곳이 사라지는데
        #    개수를 세는 사람은 그걸 못 본다. 알고도 진행하려면 --결손허용 을 줘야 한다.
        if not r.get("기관명") or not r.get("연도"):
            결손.append(r)
            continue
        사업정본 = 정본[사업]
        이름 = r["기관명"].strip()
        고침 = 정정.get((사업정본, r["연도"], r.get("연락처")))
        if 고침:
            버림[f"정정:{이름}→{고침}"] += 1
            이름 = 고침
        out.append({
            "org_id": org_id(이름),
            "사업명": 사업정본,
            "기준연도": int(r["연도"]),
            "역할": 역할,
            "구분": (r.get("구분") or "")[:200],
            "연락처": r.get("연락처"),
            "주소": r.get("주소"),
            "연도출처": 연도출처(r.get("연도출처")),
            "출처표기": r["기관명"],
            "출처파일": r.get("출처") or r.get("파일") or "",
            "_이름": 이름,
        })
    return out, 버림, 결손


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="+", help="추출 JSON 경로")
    ap.add_argument("--실행", action="store_true", help="실제로 쓴다 (기본은 dry)")
    ap.add_argument("--결손허용", action="store_true",
                    help="🔴 기관명·연도가 빈 레코드를 알고도 건너뛴다. 기본은 정지")
    a = ap.parse_args()

    print("■ 읽기")
    행 = 읽기(a.json)
    행, 버림, 결손 = 다듬기(행)
    print(f"■ 다듬기 후 {len(행)}건")
    for k, v in 버림.most_common():
        print(f"   버림/정정  {k}  {v}건")
    if 결손:
        print(f"\n🔴 추출 결손 {len(결손)}건 — 「없는 기관」이 아니라 「뽑기가 실패한 기관」이다")
        for r in 결손:
            print(f"   사업={r.get('사업')} 연도={r.get('연도')} 기관명={r.get('기관명')!r}")
            print(f"      연락처={r.get('연락처')}")
            print(f"      출처={r.get('출처')}")
        if not a.결손허용:
            raise SystemExit(
                "적재 중단 — 원본에서 고쳐 오거나, 알고도 진행하려면 --결손허용 을 줘라")
        print("   ⚠️ --결손허용 — 알고도 건너뛴다. 고쳐지면 재적재하면 들어간다\n")

    # ── PK 충돌을 **적재 전에** 센다. DB 가 죽고 나서 알면 늦다 ──
    pk = Counter((r["org_id"], r["사업명"], r["기준연도"], r["역할"], r["구분"]) for r in 행)
    충돌 = {k: n for k, n in pk.items() if n > 1}
    if 충돌:
        print(f"🔴 PK 충돌 {len(충돌)}건 — 적재하면 조용히 덮인다")
        이름표 = {r["org_id"]: r["_이름"] for r in 행}
        for k, n in list(충돌.items())[:15]:
            print(f"   {n}회  {이름표[k[0]]} · {k[1]} · {k[2]} · {k[3]} · 구분={k[4]!r}")
        raise SystemExit("적재 중단")
    print(f"■ PK 충돌 0 · 고유 {len(pk)}건")

    기관 = {}
    사업별 = defaultdict(set)
    for r in 행:
        기관[r["org_id"]] = r["_이름"]
        사업별[r["org_id"]].add(r["사업명"])
    print(f"■ 기관 {len(기관)}곳")
    print("■ 사업×역할×연도")
    for k, n in sorted(Counter((r["사업명"], r["역할"], r["기준연도"]) for r in 행).items()):
        print(f"   {k[0]:<24} {k[1]:<6} {k[2]}  {n:>4}건")

    if not a.실행:
        print("\n(dry) --실행 을 줘야 쓴다")
        return

    import psycopg
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        # orgs 를 먼저 채운다 (FK). 이미 있으면 건드리지 않는다 —
        # 🔴 기존 픽스처 2행과 다른 세션이 만든 행을 덮지 않는다.
        cur.executemany(
            'INSERT INTO tenant.orgs (org_id, "기관명", "사업명") VALUES (%s,%s,%s) '
            "ON CONFLICT (org_id) DO NOTHING",
            [(k, v, sorted(사업별[k])) for k, v in 기관.items()])
        새기관 = cur.rowcount
        cur.executemany(
            'INSERT INTO tenant.org_programs '
            '(org_id,"사업명","기준연도","역할","구분","연락처","주소","연도출처","출처표기","출처파일") '
            "VALUES (%(org_id)s,%(사업명)s,%(기준연도)s,%(역할)s,%(구분)s,"
            "%(연락처)s,%(주소)s,%(연도출처)s,%(출처표기)s,%(출처파일)s) "
            "ON CONFLICT DO NOTHING",
            행)
        conn.commit()
        cur.execute("SELECT count(*) FROM tenant.org_programs")
        적재 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tenant.orgs")
        orgs = cur.fetchone()[0]
    print(f"\n■ 적재 완료 — org_programs {적재}행 · orgs {orgs}행 (신규 {새기관})")
    if 적재 != len(행):
        print(f"🔴 넣으려던 {len(행)} 과 표의 {적재} 이 다르다 — 원인을 밝혀라")


if __name__ == "__main__":
    main()
