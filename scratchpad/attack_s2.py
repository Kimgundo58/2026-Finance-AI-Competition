# -*- coding: utf-8 -*-
"""S2 자가검토 — 함수를 실제로 태운다. 정지 조건을 «발동시킨다».

가짜 Supabase 를 진짜로 세운다: RSA 키 생성 → JWKS 를 로컬 HTTP 로 서빙 →
auth 가 httpx 로 가져가게 한다. 서명 검증 경로를 실제로 통과시킨다.
"""
import json, os, sys, threading, time, uuid, re
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["SUDDOE_DEMO_SECRET"] = "test-demo-secret"
os.environ["SUDDOE_SLUG_SECRET"] = "test-slug-secret"
os.environ["SUDDOE_MOCK"] = "0"
os.environ["SUDDOE_DEMO_보존초"] = "86400"

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt, psycopg

DSN = "postgresql://postgres:devpw@localhost:5432/suddoe"

# ── 가짜 Supabase JWKS 서버 ─────────────────────────────────────────
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIV = KEY.private_bytes(
    __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM,
    __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PrivateFormat.PKCS8,
    __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).NoEncryption(),
)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update({"kid": "kid-1", "use": "sig", "alg": "RS256"})
JWKS = {"keys": [JWK]}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps(JWKS).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["SUDDOE_JWKS_URL"] = f"http://127.0.0.1:{srv.server_port}/jwks"

sys.path.insert(0, os.path.abspath("."))
from server import auth, routes_orgs
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient

def 발급(**클레임):
    본문 = {"iss": "https://fake.supabase.co/auth/v1", "aud": "authenticated",
            "sub": str(uuid.uuid4()), "exp": int(time.time()) + 600}
    본문.update(클레임)
    return jwt.encode(본문, PRIV, algorithm="RS256", headers={"kid": "kid-1"})

# ── 픽스처 ──────────────────────────────────────────────────────────
피해자 = "97dd45a1-5613-5287-b1fa-abeff000c567"   # 건국대학교 창업지원본부 (실재)
공격자메일 = "s2-attacker@example.test"
피해자메일 = "s2-victim@example.test"
공격자org = "86cbda02-64d1-542f-8c95-9164563e2ce6"  # 건국대학교

def sql(q, a=None):
    with psycopg.connect(DSN) as c:
        cur = c.execute(q, a) if a is not None else c.execute(q)
        try: return cur.fetchall()
        except psycopg.ProgrammingError: return []

sql("DELETE FROM tenant.accounts WHERE email LIKE %s", ("s2-%",))
sql("INSERT INTO tenant.accounts (org_id, email) VALUES (%s,%s)", (공격자org, 공격자메일))
sql("INSERT INTO tenant.accounts (org_id, email) VALUES (%s,%s)", (피해자, 피해자메일))
print("픽스처: accounts 2행 (pw_hash NULL 로 들어갔다 = NOT NULL 해제 확인)")

결과 = []
def 시나리오(이름, 기대, fn):
    try:
        r = fn(); 실제 = f"통과 org={r.org_id} 출처={r.출처}" if hasattr(r, "org_id") else f"통과 {r}"
    except HTTPException as e:
        실제 = f"{e.status_code} {e.detail}"
    except Exception as e:
        실제 = f"EXC {type(e).__name__}: {e}"
    ok = 기대 in 실제
    결과.append((이름, 기대, 실제, ok))
    print(("  OK  " if ok else "  !!  "), 이름, "→", 실제)

주 = auth.현재주체
print("\n=== A. 토큰 정지조건 ===")
시나리오("토큰 없음 · org_id 없음", "출처=none", lambda: 주(None, None))
시나리오("Bearer 아님", "401", lambda: 주("Basic abc", None))
시나리오("Bearer 빈값", "401", lambda: 주("Bearer   ", None))
시나리오("쓰레기 토큰", "401", lambda: 주("Bearer not.a.jwt", None))
시나리오("위조 서명(다른 RSA키)", "401", lambda: 주(
    "Bearer " + jwt.encode({"iss":"https://fake.supabase.co/auth/v1","aud":"authenticated",
        "sub":"x","email":공격자메일,"exp":int(time.time())+600},
        rsa.generate_private_key(public_exponent=65537,key_size=2048).private_bytes(
            __import__("cryptography.hazmat.primitives.serialization",fromlist=["x"]).Encoding.PEM,
            __import__("cryptography.hazmat.primitives.serialization",fromlist=["x"]).PrivateFormat.PKCS8,
            __import__("cryptography.hazmat.primitives.serialization",fromlist=["x"]).NoEncryption()),
        algorithm="RS256", headers={"kid":"kid-1"}), None))
시나리오("만료 토큰", "401", lambda: 주("Bearer " + 발급(email=공격자메일, exp=int(time.time())-10), None))
시나리오("alg=none", "401", lambda: 주(
    "Bearer " + jwt.encode({"iss":"https://fake.supabase.co/auth/v1","email":공격자메일,
        "sub":"x","exp":int(time.time())+600}, key="", algorithm="none"), None))
# 🔴 pyjwt 는 «공개키를 HMAC 비밀로» 인코딩하는 것을 거부한다 — 그래서 손으로 만든다.
#    안 그러면 공격 토큰이 만들어지지도 않고 우리 디코더를 «태워보지 못한 채» 통과로 읽힌다.
import base64, hashlib as _h, hmac as _hm
def 손수_HS256(본문, 비밀바이트, kid="kid-1"):
    b64 = lambda d: base64.urlsafe_b64encode(d).rstrip(b"=")
    h = b64(json.dumps({"alg":"HS256","typ":"JWT","kid":kid}).encode())
    p_ = b64(json.dumps(본문).encode())
    sig = b64(_hm.new(비밀바이트, h + b"." + p_, _h.sha256).digest())
    return (h + b"." + p_ + b"." + sig).decode()

_PEM공개 = KEY.public_key().public_bytes(
    __import__("cryptography.hazmat.primitives.serialization",fromlist=["x"]).Encoding.PEM,
    __import__("cryptography.hazmat.primitives.serialization",fromlist=["x"]).PublicFormat.SubjectPublicKeyInfo)
시나리오("alg confusion (JWKS 공개키를 HMAC 비밀로 · 손수 서명)", "401", lambda: 주(
    "Bearer " + 손수_HS256({"iss":"https://fake.supabase.co/auth/v1","aud":"authenticated",
        "sub":"x","email":공격자메일,"exp":int(time.time())+600}, _PEM공개), None))
시나리오("alg confusion · JWK n 값을 비밀로", "401", lambda: 주(
    "Bearer " + 손수_HS256({"iss":"https://fake.supabase.co/auth/v1","aud":"authenticated",
        "sub":"x","email":공격자메일,"exp":int(time.time())+600}, JWK["n"].encode()), None))
시나리오("모르는 kid", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"https://fake.supabase.co/auth/v1","aud":"authenticated","sub":"x",
     "email":공격자메일,"exp":int(time.time())+600}, PRIV, algorithm="RS256",
    headers={"kid":"kid-없음"}), None))
시나리오("aud 틀림", "401", lambda: 주("Bearer " + 발급(email=공격자메일, aud="anon"), None))
시나리오("email 클레임 없음", "403", lambda: 주("Bearer " + 발급(), None))
시나리오("미등록 email", "403", lambda: 주("Bearer " + 발급(email="nobody@example.test"), None))
시나리오("데모비밀로 서명 + supabase iss", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"https://fake.supabase.co/auth/v1","aud":"authenticated","sub":"x",
     "email":공격자메일,"exp":int(time.time())+600}, "test-demo-secret",
    algorithm="HS256", headers={"kid":"kid-1"}), None))
시나리오("정상 토큰", f"org={공격자org}", lambda: 주("Bearer " + 발급(email=공격자메일), None))

print("\n=== B. 남의 org_id 사칭 ===")
시나리오("🔴 토큰 없이 남의 org_id (폴백 켜짐 = R4 기본)", f"org={피해자}",
         lambda: 주(None, 피해자))
시나리오("토큰 + 남의 org_id 동시 → 토큰이 이겨야", f"org={공격자org}",
         lambda: 주("Bearer " + 발급(email=공격자메일), 피해자))
시나리오("미등록 토큰 + 남의 org_id → 파라미터로 «흐르면» 안 된다", "403",
         lambda: 주("Bearer " + 발급(email="nobody@example.test"), 피해자))
시나리오("만료 토큰 + 남의 org_id → 흐르면 안 된다", "401",
         lambda: 주("Bearer " + 발급(email=공격자메일, exp=int(time.time())-10), 피해자))
시나리오("org_id 가 UUID 가 아님", "422", lambda: 주(None, "'; DROP TABLE--"))
auth.ORG_PARAM_허용 = False
시나리오("폴백 끔 (SUDDOE_ORG_PARAM=0) + 남의 org_id", "401", lambda: 주(None, 피해자))
시나리오("폴백 끔 + 정상 토큰", f"org={공격자org}", lambda: 주("Bearer " + 발급(email=공격자메일), None))
auth.ORG_PARAM_허용 = True

print("\n=== C. 데모 토큰 ===")
데모org = str(uuid.uuid4())
t, ttl = auth.데모토큰_발급(데모org)
시나리오("정상 데모 토큰", f"org={데모org}", lambda: 주("Bearer " + t, None))
시나리오("데모 토큰 + 남의 org_id → 토큰이 이겨야", f"org={데모org}", lambda: 주("Bearer " + t, 피해자))
시나리오("데모 토큰 위조(다른 비밀)", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"suddoe-demo","sub":"d","org":피해자,"exp":int(time.time())+600},
    "wrong", algorithm="HS256"), None))
시나리오("데모 토큰 만료", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"suddoe-demo","sub":"d","org":데모org,"exp":int(time.time())-10},
    "test-demo-secret", algorithm="HS256"), None))
시나리오("데모 토큰 org 를 남의 것으로 바꿔치기(비밀 모름)", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"suddoe-demo","sub":"d","org":피해자,"exp":int(time.time())+600},
    "test-slug-secret", algorithm="HS256"), None))
시나리오("데모 iss + RS256 (비대칭으로 우회 시도)", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"suddoe-demo","sub":"d","org":피해자,"exp":int(time.time())+600},
    PRIV, algorithm="RS256", headers={"kid":"kid-1"}), None))
시나리오("데모 토큰에 org 없음", "401", lambda: 주("Bearer " + jwt.encode(
    {"iss":"suddoe-demo","sub":"d","exp":int(time.time())+600},
    "test-demo-secret", algorithm="HS256"), None))

# ── D. 라우터 ───────────────────────────────────────────────────────
print("\n=== D. GET /api/orgs · POST /api/demo/session ===")
app = FastAPI(); app.include_router(routes_orgs.router)
@app.get("/probe")
def probe(주체=Depends(auth.현재주체)):
    return {"org_id_없음확인": 주체.org_id is None, "출처": 주체.출처}
c = TestClient(app)

r = c.get("/api/orgs", params={"q": "건국대", "크기": 50})
본문 = r.text
print("  건국대 검색:", r.status_code, "총건수", r.json()["총건수"])
for it in r.json()["항목"]:
    print("     ", it["slug"], it["기관명"])
UUID정규 = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
샘 = UUID정규.findall(본문)
결과.append(("GET /api/orgs 응답에 UUID 문자열", "0건", f"{len(샘)}건 {샘[:2]}", len(샘) == 0))
print(("  OK  " if not 샘 else "  !!  "), f"응답 grep UUID: {len(샘)}건")

r2 = c.get("/api/orgs", params={"q": "건국대학교창업지원본부"})
print("  공백 뗀 질의 '건국대학교창업지원본부' →", r2.json()["총건수"], "건",
      [i["기관명"] for i in r2.json()["항목"]])
결과.append(("공백 무시 검색이 띄어쓰기 변종을 다 잡는가", ">=2", str(r2.json()["총건수"]),
             r2.json()["총건수"] >= 2))

# 페이징
p1 = c.get("/api/orgs", params={"페이지": 1, "크기": 5}).json()
p2 = c.get("/api/orgs", params={"페이지": 2, "크기": 5}).json()
겹 = {i["slug"] for i in p1["항목"]} & {i["slug"] for i in p2["항목"]}
결과.append(("페이징 1·2쪽 겹침", "0", str(len(겹)), len(겹) == 0))
print(f"  페이징: 총 {p1['총건수']}, 1쪽 {len(p1['항목'])}건, 2쪽 {len(p2['항목'])}건, 겹침 {len(겹)}")

# 데모 세션 2개
d1 = c.post("/api/demo/session").json()
d2 = c.post("/api/demo/session").json()
o1 = jwt.decode(d1["access_token"], "test-demo-secret", algorithms=["HS256"], issuer="suddoe-demo")["org"]
o2 = jwt.decode(d2["access_token"], "test-demo-secret", algorithms=["HS256"], issuer="suddoe-demo")["org"]
결과.append(("데모 세션 2회가 서로 다른 org", "다름", f"{o1[:8]} vs {o2[:8]}", o1 != o2))
샘2 = UUID정규.findall(json.dumps(d1, ensure_ascii=False))
결과.append(("데모 응답 본문(토큰 제외 필드)에 UUID", "0건", str(len(샘2)), len(샘2) == 0))
print(f"  데모 org: {o1[:8]} / {o2[:8]} · 응답필드 UUID {len(샘2)}건 · 만료 {d1['expires_in']}s")

# 데모 org 가 목록에 뜨는가
목록전체 = c.get("/api/orgs", params={"크기": 200, "q": "데모"}).json()
결과.append(("데모 org 가 /api/orgs 에 노출", "0건", str(목록전체["총건수"]), 목록전체["총건수"] == 0))
print(f"  /api/orgs 에서 '데모' 검색: {목록전체['총건수']}건")

# 격리 실증 — 데모1 이 계획을 넣고 데모2 가 본다
sys.path.insert(0, os.path.abspath("."))
from server.routes_plans import _실_목록
sql("INSERT INTO tenant.expense_plans (org_id, 제목, 질문원문) VALUES (%s,%s,%s)",
    (o1, "심사위원1의 비밀 지출", "노트북 사도 되나요"))
본 = _실_목록(o2)
결과.append(("🔴 데모2 가 데모1 의 계획을 보는가", "0건", f"{len(본)}건", len(본) == 0))
게 = _실_목록(None)
결과.append(("🔴 게스트(None)가 데모1 의 계획을 보는가", "0건", f"{len(게)}건", len(게) == 0))
자 = _실_목록(o1)
결과.append(("데모1 이 자기 계획을 보는가", "1건", f"{len(자)}건", len(자) == 1))
print(f"  격리: 데모1자기 {len(자)}건 · 데모2 {len(본)}건 · 게스트 {len(게)}건")

# 정리
sql("DELETE FROM tenant.expense_plans WHERE org_id IN (%s,%s)", (o1, o2))
sql("DELETE FROM tenant.orgs WHERE 기관명 LIKE %s", ("[데모] %",))
sql("DELETE FROM tenant.accounts WHERE email LIKE %s", ("s2-%",))
남 = sql("SELECT count(*) FROM tenant.orgs")[0][0]
print(f"  정리 후 tenant.orgs = {남} (원래 413)")
결과.append(("정리 후 orgs 원복", "413", str(남), 남 == 413))

print("\n" + "=" * 60)
실패 = [r for r in 결과 if not r[3]]
print(f"총 {len(결과)}건 · 통과 {len(결과)-len(실패)} · 실패 {len(실패)}")
for n, e, a, _ in 실패:
    print("  실패:", n, "| 기대", e, "| 실제", a)
sys.exit(1 if 실패 else 0)
