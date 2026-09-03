# -*- coding: utf-8 -*-
"""시연용 기관 1건 + 계정 1건을 심는다. **멱등** — 두 번 돌려도 중복이 안 생긴다.

    PYTHONIOENCODING=utf-8 python scripts/seed_demo.py --email 심사용@example.com
    PYTHONIOENCODING=utf-8 python scripts/seed_demo.py --email ... --dsn <운영DSN> --운영승인

🔴 **이메일에 기본값을 두지 않는다.** 오너 미결이고, 기본값을 넣으면 그 값이 그대로
   운영에 박힌다. 없으면 exit 2 로 죽는다.

🔴 **비밀번호는 안 심는다.** 로그인은 Supabase 가 든다 — 우리 `tenant.accounts` 는
   (email → org_id) 매핑표일 뿐이고 `pw_hash` 는 비어 있는 게 정상이다
   (`server/auth.py` 머리말). 그래서 여기서 심는 이메일은 **Supabase 에 이미 있는
   계정의 이메일** 이어야 한다. 아니면 토큰이 안 나와서 아무것도 안 열린다.

■ RLS 아래서도 돈다
  `tenant.orgs`·`tenant.accounts` 정책이 `org_id = current_org()` 라, 슈퍼유저가
  아니면 org 를 «만들» 수 없다(닭-달걀). 그래서 org 를 새로 만들 때는 uuid 를
  **앱이 먼저 뽑아** GUC 에 세우고 같은 값으로 INSERT 한다 — 정책이 그대로여도 통과한다
  (실측). 로컬 `postgres` 는 슈퍼유저라 이 경로가 없어도 되지만, 그 편의가 바로
  「로컬에선 되는데 운영에서 죽는」 사고의 원인이라 양쪽을 같은 길로 태운다.

■ 🔴 이 스크립트가 **못 고치는 것** — 심어도 로그인이 안 열릴 수 있다
  `auth._계정조회()` 는 «org 를 알아내려고» accounts 를 읽는다. 즉 읽는 시점에
  GUC 가 아직 없다. 비특권 롤로 재면:

      앱롤 · GUC 없음        → 0행     ← 로그인은 여기서 403 이 된다
      앱롤 · GUC 세움(=org)  → 1행

  로컬 `postgres`(superuser)로는 1행이 나와서 **안 보인다.** 명부를 채우는 것과
  별개인 정책 문제이고 DDL 이 필요해 여기서 안 고친다 (보고서 참조).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg


def _가림(dsn: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)


def _로컬인가(dsn: str) -> bool:
    return "@localhost" in dsn or "@127.0.0.1" in dsn


def _인자():
    p = argparse.ArgumentParser(description="시연용 기관·계정 심기 (멱등)")
    p.add_argument("--email", default=os.environ.get("SUDDOE_SEED_EMAIL", "").strip(),
                   help="시연 계정 이메일. 🔴 기본값 없음 (SUDDOE_SEED_EMAIL 도 됨)")
    p.add_argument("--기관명", default=os.environ.get("SUDDOE_SEED_ORG", "건국대학교"))
    p.add_argument("--dsn", default=os.environ.get("SUDDOE_DSN", "").strip(),
                   help="없으면 server/_common.py 의 DSN 을 쓴다")
    p.add_argument("--운영승인", action="store_true",
                   help="🔴 localhost 가 아닌 DB 에 쓰려면 반드시 붙인다")
    p.add_argument("--dry-run", action="store_true", help="롤백하고 끝낸다")
    a = p.parse_args()
    if not a.email:
        p.error("--email 이 필요하다 (또는 SUDDOE_SEED_EMAIL). "
                "🔴 기본값은 두지 않는다 — 오너 미결이다")
    if not a.dsn:
        from server._common import DSN
        a.dsn = DSN
    return a


def _org잡기(cur, 기관명: str) -> tuple[str, str]:
    """돌려주는 값: (org_id, '있음'|'만듦')."""
    행 = cur.execute("SELECT org_id FROM tenant.orgs WHERE 기관명 = %s",
                     (기관명,)).fetchall()
    if len(행) > 1:
        raise SystemExit(f"🔴 «{기관명}» 이름의 기관이 {len(행)}건이다 — 어느 것인지 "
                         f"고를 수 없다. --기관명 을 정확히 줘라: "
                         f"{[str(r[0]) for r in 행]}")
    if 행:
        return str(행[0][0]), "있음"
    # 🔴 닭-달걀 우회. uuid 를 «먼저» 뽑아 GUC 에 세우고 같은 값으로 INSERT 한다.
    #    DEFAULT gen_random_uuid() 에 맡기면 GUC 와 값이 달라져 RLS 가 막는다(실측).
    새 = str(uuid.uuid4())
    cur.execute("SELECT set_config('app.org_id', %s, true)", (새,))
    cur.execute("INSERT INTO tenant.orgs (org_id, 기관명) VALUES (%s, %s)", (새, 기관명))
    return 새, "만듦"


def _계정잡기(cur, org: str, email: str) -> str:
    cur.execute("SELECT set_config('app.org_id', %s, true)", (org,))
    행 = cur.execute("SELECT account_id FROM tenant.accounts WHERE email = %s",
                     (email,)).fetchall()
    if 행:
        return "있음"
    try:
        cur.execute("INSERT INTO tenant.accounts (org_id, email) VALUES (%s, %s)",
                    (org, email))
    except psycopg.errors.UniqueViolation:
        # 🔴 위 SELECT 는 «이 org 안에서만» 본다 (RLS). 다른 기관에 같은 이메일이
        #    있으면 안 보이다가 여기서 터진다 — 조용히 넘기면 「심었다」고 착각한다.
        raise SystemExit(f"🔴 이메일 «{email}» 이 이미 «다른 기관» 에 있다. "
                         f"옮기려면 오너 확인이 필요하다 — 이 스크립트는 안 옮긴다")
    return "심음"


def main() -> int:
    a = _인자()
    if not _로컬인가(a.dsn) and not a.운영승인:
        print(f"🔴 로컬이 아닌 DB 다 ({_가림(a.dsn)}). 쓰려면 --운영승인 을 붙여라.",
              file=sys.stderr)
        return 2

    print(f"대상  : {_가림(a.dsn)}")
    print(f"기관명: {a.기관명}\n이메일: {a.email}")

    conn = psycopg.connect(a.dsn, connect_timeout=10)
    conn.autocommit = False           # 🔴 GUC 가 트랜잭션 한정이라 켜면 안 된다
    try:
        cur = conn.cursor()
        org, org상태 = _org잡기(cur, a.기관명)
        계정상태 = _계정잡기(cur, org, a.email)
        if a.dry_run:
            conn.rollback()
            print(f"\n[dry-run] 기관 {org상태} · 계정 {계정상태} — 롤백했다")
            return 0
        conn.commit()
    finally:
        conn.close()

    # ── 검산 — 커밋한 «뒤» 새 커넥션으로 되읽는다 (같은 트랜잭션 안에서 재면
    #    안 써진 것도 써진 것처럼 보인다)
    with psycopg.connect(a.dsn, connect_timeout=10) as c:
        c.execute("SELECT set_config('app.org_id', %s, true)", (org,))
        n = c.execute("SELECT count(*) FROM tenant.accounts WHERE email = %s",
                      (a.email,)).fetchone()[0]
    print(f"\n기관 {org상태} : {org}\n계정 {계정상태} : {a.email}\n검산  : accounts {n}행 (1이어야 한다)")
    return 0 if n == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
