# -*- coding: utf-8 -*-
"""미들웨어를 «진짜 routes_plans 라우터» 에 걸고 태운다.

이게 핵심이다 — auth.py 만으로는 아무것도 안 막힌다. routes_plans 는 쿼리파라미터를
직접 읽으니까. 미들웨어가 라우터 «앞» 에서 갈아끼우는지 실제 HTTP 로 확인한다.
"""
import json, os, sys, threading, time, uuid

from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["SUDDOE_DEMO_SECRET"] = "test-demo-secret"
os.environ["SUDDOE_SLUG_SECRET"] = "test-slug-secret"
os.environ["SUDDOE_MOCK"] = "0"

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt, psycopg

DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = KEY.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "kid-1", "use": "sig", "alg": "RS256"})


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"keys": [JWK]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a): pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["SUDDOE_JWKS_URL"] = f"http://127.0.0.1:{srv.server_port}/jwks"

sys.path.insert(0, os.path.abspath("."))
from server import auth, routes_orgs, routes_plans
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.add_middleware(auth.OrgId주입)              # ← main.py 패치가 넣을 «그» 한 줄
app.include_router(routes_orgs.router)
app.include_router(routes_plans.router)         # 손 안 댄 진짜 라우터

# SSE 가 미들웨어 뒤에서 조각조각 오는지 — 버퍼링되면 판정 진행표시가 죽는다
from server._common import _sse, _sse응답
@app.get("/api/_sse테스트")
def _sse테스트(org_id: str | None = None):
    def gen():
        for i in range(3):
            yield _sse("진행", {"i": i, "본_org": org_id})
            time.sleep(0.15)
    return _sse응답(gen())

c = TestClient(app)


def sql(q, a=None):
    with psycopg.connect(DSN) as cn:
        cur = cn.execute(q, a) if a is not None else cn.execute(q)
        try:
            return cur.fetchall()
        except psycopg.ProgrammingError:
            return []


공격자org = "86cbda02-64d1-542f-8c95-9164563e2ce6"   # 건국대학교
피해자org = "97dd45a1-5613-5287-b1fa-abeff000c567"   # 건국대학교 창업지원본부
공격자메일 = "s2-attacker@example.test"
sql("DELETE FROM tenant.accounts WHERE email LIKE %s", ("s2-%",))
sql("INSERT INTO tenant.accounts (org_id, email) VALUES (%s,%s)", (공격자org, 공격자메일))
sql("DELETE FROM tenant.expense_plans WHERE 제목 LIKE %s", ("S2테스트%",))
sql("INSERT INTO tenant.expense_plans (org_id,제목,질문원문) VALUES (%s,%s,%s)",
    (피해자org, "S2테스트 · 피해자의 비밀 지출", "천만원짜리 장비"))
sql("INSERT INTO tenant.expense_plans (org_id,제목,질문원문) VALUES (%s,%s,%s)",
    (공격자org, "S2테스트 · 공격자 본인 지출", "노트북"))


def 발급(**kw):
    본 = {"iss": "https://fake.supabase.co/auth/v1", "aud": "authenticated",
          "sub": str(uuid.uuid4()), "exp": int(time.time()) + 600}
    본.update(kw)
    return jwt.encode(본, PRIV, algorithm="RS256", headers={"kid": "kid-1"})


결과 = []


def 케이스(이름, 기대, 경로, params=None, headers=None):
    r = c.get(경로, params=params or {}, headers=headers or {})
    if r.status_code == 200 and "항목" in r.text:
        실제 = f"200 {[i['제목'] for i in r.json()['항목']]}"
    else:
        실제 = f"{r.status_code} {r.text[:70]}"
    ok = 기대 in 실제
    결과.append((이름, 기대, 실제, ok))
    print(("  OK  " if ok else "  !!  "), 이름, "→", 실제)


정상 = 발급(email=공격자메일)
print("=== 미들웨어를 통과한 /api/plans (진짜 라우터) ===")
케이스("토큰 없이 피해자 org_id — R4 폴백이라 «뚫린다»", "피해자의 비밀 지출",
       "/api/plans", {"org_id": 피해자org})
케이스("🔴 토큰(공격자) + 피해자 org_id → 토큰이 이겨야", "공격자 본인 지출",
       "/api/plans", {"org_id": 피해자org}, {"Authorization": f"Bearer {정상}"})
케이스("토큰만 → 자기 것만", "공격자 본인 지출", "/api/plans", {},
       {"Authorization": f"Bearer {정상}"})
케이스("org_id 를 «두 번» 실어 우회 시도", "공격자 본인 지출",
       f"/api/plans?org_id={피해자org}&org_id={피해자org}", None,
       {"Authorization": f"Bearer {정상}"})
케이스("만료 토큰 + 피해자 org_id → 401 (파라미터로 흐르면 안 된다)", "401",
       "/api/plans", {"org_id": 피해자org},
       {"Authorization": f"Bearer {발급(email=공격자메일, exp=int(time.time()) - 10)}"})
케이스("위조 토큰 + 피해자 org_id → 401", "401", "/api/plans", {"org_id": 피해자org},
       {"Authorization": "Bearer aaa.bbb.ccc"})
케이스("데모 토큰 + 피해자 org_id → 데모 org (0건)", "200 []", "/api/plans",
       {"org_id": 피해자org},
       {"Authorization": f"Bearer {auth.데모토큰_발급(str(uuid.uuid4()))[0]}"})
케이스("다른 필터가 살아남는가 (탭·정렬 보존)", "공격자 본인 지출", "/api/plans",
       {"org_id": 피해자org, "탭": "전체", "정렬": "금액많은순", "크기": 5},
       {"Authorization": f"Bearer {정상}"})

pid = sql("SELECT plan_id FROM tenant.expense_plans WHERE 제목 LIKE %s",
          ("S2테스트 · 피해자%",))[0][0]
r = c.get(f"/api/plans/{pid}", params={"org_id": 피해자org},
          headers={"Authorization": f"Bearer {정상}"})
결과.append(("🔴 상세: 남의 plan_id 직접 조회 + 토큰", "404", str(r.status_code), r.status_code == 404))
print(("  OK  " if r.status_code == 404 else "  !!  "),
      f"상세 /api/plans/{pid} 남의 계획 + 토큰 → {r.status_code}")

r = c.get(f"/api/plans/{pid}", params={"org_id": 피해자org})
결과.append(("상세: 토큰 없이 남의 plan_id (R4 폴백 = 뚫림)", "200", str(r.status_code),
             r.status_code == 200))
print(f"  --   폴백 켜짐일 때 상세 {r.status_code} (= R4 가 남긴 구멍, 예상된 값)")

auth.ORG_PARAM_허용 = False
r = c.get("/api/plans", params={"org_id": 피해자org})
결과.append(("🔴 폴백 끔 + 토큰 없이 남의 org_id", "401", str(r.status_code), r.status_code == 401))
print(("  OK  " if r.status_code == 401 else "  !!  "), f"폴백 끔 → 목록 = {r.status_code}")
r = c.get(f"/api/plans/{pid}", params={"org_id": 피해자org})
결과.append(("🔴 폴백 끔 + 상세 직접 조회", "401", str(r.status_code), r.status_code == 401))
print(("  OK  " if r.status_code == 401 else "  !!  "), f"폴백 끔 → 상세 = {r.status_code}")
r1 = c.get("/api/orgs", params={"q": "건국"})
결과.append(("폴백 끔이어도 /api/orgs 는 열려야 (가입 화면)", "200", str(r1.status_code),
             r1.status_code == 200))
r2 = c.post("/api/demo/session")
결과.append(("폴백 끔이어도 데모 진입은 열려야", "200", str(r2.status_code), r2.status_code == 200))
print(f"  제외경로: /api/orgs {r1.status_code} · /api/demo/session {r2.status_code}")
auth.ORG_PARAM_허용 = True

sql("DELETE FROM tenant.expense_plans WHERE 제목 LIKE %s", ("S2테스트%",))
sql("DELETE FROM tenant.accounts WHERE email LIKE %s", ("s2-%",))
sql("DELETE FROM tenant.orgs WHERE 기관명 LIKE %s", ("[데모] %",))
print(f"  정리 후 orgs={sql('SELECT count(*) FROM tenant.orgs')[0][0]} (원래 413)")


sql("INSERT INTO tenant.accounts (org_id, email) VALUES (%s,%s)", (공격자org, 공격자메일))
print("=== SSE 스트리밍 (미들웨어 통과) ===")
t0 = time.time(); 도착 = []
with c.stream("GET", "/api/_sse테스트", params={"org_id": 피해자org},
              headers={"Authorization": f"Bearer {정상}"}) as r:
    for line in r.iter_lines():
        if line.startswith("data:"):
            도착.append((round(time.time() - t0, 2), line))
# 🔴 «절대 시각» 으로 재면 안 된다 — TestClient 가 스스로 버퍼링한다(대조군으로 확인).
#    미들웨어가 «추가로» 버퍼링하는지만 물어야 하므로 미들웨어 없는 앱과 «비교» 한다.
대조 = FastAPI()
@대조.get("/api/s")
def _대조():
    def g():
        for i in range(3):
            yield _sse("진행", {"i": i}); time.sleep(0.15)
    return _sse응답(g())
c2 = TestClient(대조); t1 = time.time(); 대조도착 = []
with c2.stream("GET", "/api/s") as r2:
    for line in r2.iter_lines():
        if line.startswith("data:"): 대조도착.append(round(time.time() - t1, 2))
동일 = len(도착) == 3 == len(대조도착) and abs(도착[-1][0] - 대조도착[-1]) < 0.25
결과.append(("SSE: 미들웨어가 «대조군보다» 더 버퍼링하는가", "동일", 
             f"미들 {[d[0] for d in 도착]} vs 대조 {대조도착}", 동일))
print(("  OK  " if 동일 else "  !!  "),
      f"SSE 조각 {len(도착)}개 · 미들 {[d[0] for d in 도착]} vs 대조군 {대조도착}")
본 = [d[1] for d in 도착][0]
결과.append(("🔴 SSE 안에서도 토큰 org 가 이기는가", 공격자org, 본, 공격자org in 본))
print(("  OK  " if 공격자org in 본 else "  !!  "), "SSE 라우터가 본 org:", 본[:80])

sql("DELETE FROM tenant.accounts WHERE email LIKE %s", ("s2-%",))
실패 = [x for x in 결과 if not x[3]]
print("=" * 60)
print(f"총 {len(결과)} · 통과 {len(결과) - len(실패)} · 실패 {len(실패)}")
for n, e, a, _ in 실패:
    print("  실패:", n, "| 기대", e, "| 실제", a)
sys.exit(1 if 실패 else 0)
