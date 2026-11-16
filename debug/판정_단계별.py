# -*- coding: utf-8 -*-
"""판정 단계별 디버깅: 운영 프론트(v045~v051)와 같은 API 순서로, 운영 배포 코드 · 운영 DB(읽기 전용) · 운영 서버 IP 로 돈다.

  1 ① 화면 0 한 문장 → POST /api/normalize {질문}           화면 1 「이렇게 확인했어요!」 칸 채우기
       칸을 고쳤으면 → POST /api/normalize (폼)             품목확인.별칭일치 → ② 건너뜀
       지출 항목 입력 검증(v057): 지출 아님 → [고쳐 쓰기](입력검증_시도 2) → 또 지출 아님 → 한 문장 설명 → ② 첫 요청 품목설명
  2 ② 품목 조사 → POST /api/questions {웹조사: true}       화면 2 「…맞나요?」 [맞아요] [직접 입력하기] (후보 거래유형 v055)
  3 ③ 분류 · ④-2 판정용 조사 · ⑤ 질문 → POST /api/questions {조사ID, 조사답변, 품목설명?, 재조사답변?}
       ③ 프롬프트 변형 A · B · C 결과와 이유 · 불일치 · 조정 근거 · 거래 유형 확인 질문(v056)을 디버그로 찍는다
       분류없음 → 다음행동: 재조사확인(③-R 카드 · 거래 유형 확인 카드) · 설명입력(한 문장) · 다시시도 · 판정(분류 없이) 을 화면처럼 이어 간다
       화면 3 확인 질문 [예][아니오][모르겠어요] · 입력 칸
  4 ⑥ ⑦ 판정 → POST /api/judge {멱등키}                  판정 내부 단계마다 멈춤. 판정실패면 [다시 점검](같은 멱등키)
  5 판정만 다시(선택) · 6 전체 요약
  시스템 오류 재시도(v053): 단계 · 원인 · 횟수 · 대기를 [재시도] 줄로 찍고, 다시시도 안내를 받으면 화면처럼 같은 요청을 다시 보낸다

실행 방법 (자세히는 debug/README.md)
  · VS Code 디버그: 실행 및 디버그 → 「판정 단계별 (운영)」. 단계 사이 · 판정 내부 단계마다 멈추고, 서버 코드 어디에나 중단점.
  · Interactive 창: 각 `# %%` 칸의 「Run Cell」(줄 단위는 「Debug Cell」). 값을 고쳐 칸을 다시 돌린다.
  · 터미널: PYTHONIOENCODING=utf-8 python debug/판정_단계별.py  (단계마다 Enter)
  · 서버 방식(설정.실행방식 = "서버", 「판정 디버그 서버 (운영)」): 0번 칸 준비 뒤 디버그 서버 + 프론트를 띄우고 여기서 끝난다.
    브라우저 화면이 보낸 ① ② ③ · 판정 요청마다 기록 폴더가 생긴다(추적.흐름기록). 1~6번 칸은 돌지 않는다.
  · 설정.화면API_주소 = "http://127.0.0.1:8000": 떠 있는 디버그 서버로 1~6번 칸 요청을 보낸다(계획 저장 POST /api/plans 포함).
  · 검색만 다시: python debug/판정_단계별.py 검색다시 <판정 단계 폴더>  (규정조회 · 규정검색만 지금 코드로, LLM 없음, 녹화와 비교)
"""
# %% 0. 설정 · 운영 연결 · 서버 띄우기 ───────────────────────────────
import json
import os
import sys
from pathlib import Path

if "__file__" in globals():
    DEBUG = Path(__file__).resolve().parent
else:                                                    # Interactive 창
    DEBUG = next(p for p in (Path.cwd(), Path.cwd() / "debug") if (p / "설정.py").exists())
작업트리 = DEBUG.parent
for p in (str(DEBUG), str(작업트리 / "scripts"), str(작업트리)):
    if p not in sys.path:
        sys.path.insert(0, p)


class _터미널복제:
    """터미널 출력(stdout · stderr)을 debug/실행기록/터미널_<시각>.log 에도 그대로 쓴다."""

    def __init__(self, 원래, 파일):
        self._원래, self._파일 = 원래, 파일

    def write(self, s):
        n = self._원래.write(s)
        try:
            self._파일.write(s)
            self._파일.flush()
        except Exception:  # noqa: BLE001  로그 파일 문제로 실행이 멈추지 않게
            pass
        return n

    def flush(self):
        self._원래.flush()

    def __getattr__(self, 이름):
        return getattr(self._원래, 이름)


if not isinstance(sys.stdout, _터미널복제):
    import time as _time
    터미널로그 = DEBUG / "실행기록" / f"터미널_{_time.strftime('%Y%m%d_%H%M%S')}.log"
    터미널로그.parent.mkdir(parents=True, exist_ok=True)
    _로그파일 = open(터미널로그, "a", encoding="utf-8", buffering=1)
    sys.stdout, sys.stderr = _터미널복제(sys.stdout, _로그파일), _터미널복제(sys.stderr, _로그파일)
    print(f"터미널 로그: {터미널로그}")

import 설정        # noqa: E402
import 운영연결    # noqa: E402
import 추적        # noqa: E402

# 검색다시 CLI: 디버그 서버와 같이 돌 수 있게 다른 터널 포트를 쓰고, 화면 API 없이 판정 인자만으로 돈다
검색다시_폴더 = sys.argv[2] if len(sys.argv) >= 3 and sys.argv[1] == "검색다시" else None
if 검색다시_폴더:
    설정.실행방식, 설정.화면API_주소, 설정.단계마다_멈춤 = "단계별", None, False
    설정.DB포트, 설정.SOCKS포트 = 설정.검색다시_DB포트, 설정.검색다시_SOCKS포트
서버모드 = 설정.실행방식 == "서버"
원격 = bool(설정.화면API_주소) and not 서버모드      # 떠 있는 디버그 서버로 요청만 보낸다(운영 연결 · 서버 기록은 그 서버 쪽)
MOCK = 설정.연결 == "mock"
if 원격:
    print(f"연결: 디버그 서버 {설정.화면API_주소} 로 요청만 보낸다(기록 폴더 · LLM · SQL 은 서버 쪽에 남는다)")
elif MOCK:
    os.environ["SUDDOE_MOCK"] = "1"                      # 프로젝트 모듈 import 전에 넣는다(server._common 이 import 때 읽는다)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    print("연결: mock (SSH · DB · LLM 없음, server/mock_flow.py 고정 응답)")
else:
    정보 = 운영연결.연결(설정)                     # 프로젝트 모듈 import 전에 env 를 넣는다
    print("운영 배포 SHA:", 정보["운영SHA"])
    print("이 코드 SHA  :", 정보["코드SHA"], "(운영과 같음)" if 정보["코드SHA"] == 정보["운영SHA"] else "⚠ 운영 배포와 다름")
    if 정보["코드고침"]:
        print("이 작업트리에서 고친 파일(운영과 다르게 돈다):\n" + 정보["코드고침"])
    for k, v in 운영연결.보이는_env().items():
        print(f"  {k} = {v}")

기록 = 추적.기록장(설정)
기록.추적기.설치()                               # 서버 스레드가 생기기 전에 건다

import llm_qwen          # noqa: E402,F401
import research_runtime  # noqa: E402
import orchestrate       # noqa: E402
from _lib import db      # noqa: E402
from server import auth, judge_response, research_sessions  # noqa: E402
from server.models import 판정요청  # noqa: E402

if 설정.SQL기록 and not MOCK and not 원격:
    추적.SQL_기록_설치(기록)
추적.LLM_기록_설치(기록)
추적.재시도_기록_설치(기록)                        # v053 시스템 오류 재시도(단계 · 원인 · 횟수 · 대기)

주체 = None
if not MOCK and not 원격:
    with db.connect(connect_timeout=5) as _c:
        print("DB:", _c.execute("select current_user, current_setting('default_transaction_read_only')").fetchone(),
              "(읽기 전용)")
    if 설정.이메일:
        _행 = auth._로그인주체(설정.이메일)
        if not _행:
            raise RuntimeError(f"운영에 등록되지 않은 이메일입니다: {설정.이메일}")
        주체 = auth.주체(org_id=_행[0], 출처="token", email=설정.이메일, account_id=_행[2], 본인_account_id=_행[1])
        print(f"로그인: 기관 {주체.org_id} · 데이터 계정 {주체.account_id}" + (" (팀 대표 계정)" if _행[1] != _행[2] else ""))
if 주체 is None and not 원격:
    print("로그인: 게스트(기관 없음) — L3 · 협약 프로필 · 지출 기록 없이 판정합니다. 설정.이메일 로 바꿀 수 있습니다.")

if not 원격:
    추적.운영보호(기록, 설정, 주체)
리로드 = 추적.핫리로드(설정)                       # 핫 리로드: 서버 방식은 흐름 요청마다, 단계별은 판정_다시 · 검색_다시 에서


def 검색_다시(단계폴더=None):
    """녹화한 판정 단계 폴더(판정_인자.json)로 규정조회 · 규정검색 단계만 지금 코드로 다시 돌려 녹화와 비교한다(LLM 없음).
    retrieve.py · ontology_lookup.py 등을 고쳤으면 먼저 다시 읽는다(핫 리로드). 폴더를 안 주면 이 실행의 마지막 판정 단계."""
    if 단계폴더 is None:
        단계폴더 = next((d for d in sorted(기록.폴더.glob("*"), reverse=True) if (d / "판정_인자.json").exists()), None)
        if 단계폴더 is None:
            raise RuntimeError("판정 단계 폴더를 주세요(debug/실행기록/<시각>/NN_판정 또는 NN_judge)")
    if not MOCK:
        운영연결.터널_확인()
    return 추적.검색_다시_실행(기록, 설정, 리로드, 단계폴더)


if 검색다시_폴더:
    검색_다시(검색다시_폴더)
    sys.exit(0)
if 서버모드:
    추적.서버_실행(기록, 설정, 리로드)                # 끝날 때까지 돌아오지 않는다(Ctrl+C · 디버그 중지)
    sys.exit(0)
api = 추적.화면API(기록, 설정.화면API_주소 if 원격 else None)
print("\n준비 끝. 기록 폴더:", 기록.폴더)


def 보기(값, 최대: int | None = 4000):
    s = 값 if isinstance(값, str) else 추적._json(값)
    print(s if 최대 is None or len(s) <= 최대 else s[:최대] + f"\n… (전체 {len(s):,}자)")


def 묻기(질문: str, 선택지: list[str] | None = None, 기본: str = ""):
    """화면에서 사용자가 고르는 것. 설정.모르면_자동 이면 기본값."""
    if 설정.모르면_자동:
        print(f"  {질문} → {기본!r} (자동)")
        return 기본
    표시 = f" [{'/'.join(선택지)}]" if 선택지 else ""
    while True:
        try:
            답 = input(f"  {질문}{표시} (기본 {기본!r}): ").strip() or 기본
        except EOFError:
            return 기본
        if not 선택지 or 답 in 선택지:
            return 답
        print("  선택지 중에서 골라 주세요.")


def 멈춤(설명: str):
    추적.멈춤(설정, 설명)


class 화면멈춤(Exception):
    """화면이 더 진행하지 않는 응답(오류로 화면 1 로 돌아감 등). 이 칸에서 멈춘다. 설정을 고쳐 1번 칸부터 다시 돈다."""


def 세션():
    """조사 세션(서버 메모리). 분류 · 노드결정 · 판정조사 · 재조사 · 설명정리를 디버그로 본다."""
    return research_sessions.조회(조사ID, 주체.org_id if 주체 else None, 주체.account_id if 주체 else None) if 조사ID else None


def 예산_보기():
    """조사 세션 예산(①~⑦ 가 함께 쓴다). 운영: research_runtime.Budget 기본값(v061 부터 600초 · LLM 200 · 도구 60, 그 전 300초 · LLM 100). 판정(/api/judge)은 시작할 때 600초 · LLM 40회 이상으로 늘린다."""
    s = 세션()
    print("  " + 추적.예산_요약(s.budget if s else None))


def 카드_답(후보: list[dict], 미리: dict | None, 이름: str) -> list[dict]:
    """② · ③-R 카드: 확인 문장만 보이고 발췌는 「출처 보기」 안이다(질문종류=기본이면 발췌를 바로 보인다). 답은 맞아요 | 직접입력(+한 문장)."""
    답변 = []
    for c in 후보:
        print(f"\n  [{이름} · {c.get('유형')}] {c.get('질문') or c.get('내용')}")
        if c.get("질문종류") == "기본":
            print(f"         {c.get('내용')}")
        print(f"    (출처 보기) {[x.get('url') for x in c.get('출처') or []]}")
        if c.get("거래유형"):
            print(f"    (화면에 안 나옴) 거래유형 {c.get('거래유형')}")       # v055: 확인 문장이 이 거래 유형을 말하는지 본다
        답 = (미리 or {}).get(c["id"]) or 묻기("[맞아요] / [직접입력]", ["맞아요", "직접입력"], "맞아요")
        항목 = {"id": c["id"], "답": 답}
        if 답 == "직접입력":
            항목["정정"] = 묻기("어떤 제품·서비스인지 한 문장으로(필수)", None, "") or (서버정규화.get("품목") or "")
        답변.append(항목)
    return 답변


def _출력json(r: dict) -> dict:
    try:
        v = json.loads(r.get("출력") or "")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def 정규화_요청(이름: str, 본문: dict) -> dict:
    """POST /api/normalize. v053: 시스템 오류가 예산까지 이어지면 오류 이벤트 {메시지, 다음행동 다시시도} 로 끝난다.
    화면은 그 자리에 머물고 같은 버튼으로 같은 요청을 다시 보낸다(최대 설정.분류_최대요청 번)."""
    r: dict = {}
    for 회 in range(1, 설정.분류_최대요청 + 1):
        with 기록.단계(이름 if 회 == 1 else f"{이름}_다시{회}"):
            r = api.post("/api/normalize", 본문)
        오류 = next((v for n, v in r.get("이벤트") or [] if n == "오류"), None)
        if r.get("결과") or not (isinstance(오류, dict) and 오류.get("다음행동") == "다시시도"):
            return r
        print(f"\n[① 다시 시도 안내] 화면: 「{오류.get('메시지')}」 — 같은 요청을 다시 보냅니다")
        묻기("[다시 시도] (Enter)", None, "")
    return r


def 입력검증_보기(정규화: dict) -> dict:
    """① 지출 항목 입력 검증(v057) 결과 {판단, 다음행동, 안내}. LLM 이유는 화면에 안 나가는 값이다."""
    v = 정규화.get("입력검증") if isinstance(정규화.get("입력검증"), dict) else {}
    print(f"\n[① 지출 항목 입력 검증] 판단 {v.get('판단')} · 다음행동 {v.get('다음행동')}"
          + (f" · 화면: {v.get('안내')}" if v.get("안내") else ""))
    llm = [r for r in 기록.LLM if r.get("단계") == 기록.현재단계 and "expense_input_check" in (r.get("호출위치") or "")]
    if llm:
        print(f"  [디버그] 입력 검증 LLM {len(llm)}회 · 마지막 이유: {_출력json(llm[-1]).get('이유')}")
    elif v.get("판단") == "건너뜀":
        print("  [디버그] 입력 검증 LLM 0회(별칭 일치 · 지출 항목 빔)")
    return v


분류디버그_보기 = 추적.분류디버그_보기       # ③ 분류 디버그(v056) 출력. 서버 방식도 같은 함수를 쓴다


def 한문장_설명(안내: str) -> str | None:
    print(f"  [화면] {안내}")
    if 설정.품목설명 is not None:
        return 설정.품목설명 or None
    return 묻기("한 문장 설명(비우면 없음)", None, "") or None


if 설정.호출추적:
    print("※ 호출 추적은 파이썬 코드를 2배 가까이 느리게 합니다(온톨로지 조회 23초 → 43초 관찰). 예산 시간이 모자라면 "
          "설정.예산초 를 늘리거나 호출추적=False 로 비교하세요.")


# %% 1. ① 화면 0 한 문장 → 화면 1 「이렇게 확인했어요!」 → POST /api/normalize ─────────────
운영연결.터널_확인()                             # 터널이 끊겼으면 같은 포트로 다시 연다(감시 스레드도 3초마다 본다)
사업명, 부가세구분 = 설정.사업명, 설정.부가세구분
고친칸 = {k: v for k, v in (설정.칸 or {}).items() if v is not None}
서버정규화: dict = {}
정리칸: dict = {}                                # 한 문장 정리 결과로 채운 화면 1 칸
if 설정.한문장:
    r1 = 정규화_요청("한문장정리", {"질문": 설정.한문장, "사업명": 사업명, "부가세구분": 부가세구분})
    서버정규화 = dict(r1.get("결과") or {})
    # 프론트 칸채우기: 금액은 0 보다 클 때만, 사용 목적이 비면 원문, 날짜는 사용일자, 거래처는 한 문장에 적혔을 때만(normalize_run)
    정리칸 = {"품목": 서버정규화.get("품목") or "",
           "금액": 서버정규화.get("금액") if isinstance(서버정규화.get("금액"), (int, float)) and 서버정규화["금액"] > 0 else None,
           "용도": 서버정규화.get("용도") or 설정.한문장.strip(),
           "집행예정일": 서버정규화.get("사용일자"), "거래처": 서버정규화.get("거래처")}
    print("\n[화면 0 결과] 정리한 칸:"); 보기(정리칸)
    if not r1.get("결과"):
        print("  [화면] 정리하지 못했어요. 칸을 직접 확인해 주세요.")
화면칸 = {**{"품목": "", "금액": None, "용도": "", "집행예정일": None, "거래처": None}, **{k: v for k, v in 정리칸.items() if v}, **고친칸}
print("\n[화면 1 「이렇게 확인했어요!」]"); 보기({**화면칸, "판매URL": 설정.판매URL})
if not (화면칸["품목"] and 화면칸["금액"] and 화면칸["용도"]):
    print("  ⚠ 지출 항목 · 예상 금액 · 사용 목적은 필수 칸입니다(화면은 다음으로 못 갑니다). 설정.칸 에 채우세요.")
# 프론트 확인다음: 지출 항목 · 금액 · 사용 목적이 정리 결과와 같으면 정규화를 다시 부르지 않는다. 다르면(또는 한 문장 없이 칸을 채웠으면) 폼으로 다시
_같음 = bool(설정.한문장) and bool(r1.get("결과")) and all(
    (화면칸[k] or None) == (정리칸.get(k) or None) for k in ("품목", "금액", "용도"))


def 폼본문(시도: int = 1) -> dict:
    return {"품목": 화면칸["품목"], "금액": 화면칸["금액"] or 0, "용도": 화면칸["용도"],
            "집행예정일": 화면칸["집행예정일"], "거래처": (화면칸["거래처"] or "").strip() or None,
            "사업명": 사업명, "부가세구분": 부가세구분, **({"입력검증_시도": 시도} if 시도 > 1 else {})}


if not _같음:
    r1 = 정규화_요청("입력정리_폼", 폼본문())
    서버정규화 = dict(r1.get("결과") or {})
# ① 지출 항목 입력 검증(v057): 지출 아님이면 화면은 안내 + [고쳐 쓰기]. 고쳐 쓴 것도 지출 아님이면 한 문장 설명 칸(비면 못 감)
입력검증 = 입력검증_보기(서버정규화)
입력검증_시도 = 1
while 입력검증.get("다음행동") == "품목재입력" and 입력검증_시도 < 설정.분류_최대요청:
    입력검증_시도 += 1
    새품목 = 설정.고쳐쓴_품목 or 묻기("[고쳐 쓰기] 지출 항목", None, "")
    if not 새품목:
        raise 화면멈춤("고쳐 쓴 지출 항목이 비어 있으면 화면은 다음으로 가지 않습니다(설정.고쳐쓴_품목 에 채우세요).")
    화면칸["품목"] = 새품목
    r1 = 정규화_요청("입력정리_고쳐쓰기", 폼본문(입력검증_시도))
    서버정규화 = dict(r1.get("결과") or {})
    입력검증 = 입력검증_보기(서버정규화)
입력설명 = None                                  # ① 설명입력 으로 받은 한 문장. ② 첫 요청(과 ③ 요청)에 품목설명 으로 싣는다
if 입력검증.get("다음행동") == "설명입력":
    입력설명 = 한문장_설명(입력검증.get("안내") or "")
    if not 입력설명:
        raise 화면멈춤("설명이 비면 화면은 다음으로 가지 않습니다(설정.품목설명 에 채우세요).")
거래처 = (화면칸["거래처"] or "").strip() or None
판매URL = 설정.판매URL if isinstance(설정.판매URL, str) and 설정.판매URL.strip().lower().startswith(("http://", "https://")) else None
사용일자 = (화면칸["집행예정일"] or "")[:10] or None           # 프론트는 계획의 집행예정일을 판정의 사용일자로 보낸다(lib/judge.ts)
print("\n[① 결과] 판정 입력(서버 정규화)")
보기({k: 서버정규화.get(k) for k in ("품목", "금액", "용도", "사용일자", "거래처", "질의문", "질문원문", "수치")})
품목확인 = 서버정규화.get("품목확인")
if not isinstance(품목확인, dict):
    print("  ⚠ 응답에 품목확인 이 없습니다: 옛 경로 사업(비목 고르기 화면)이라 이 키트의 새 흐름과 다릅니다.")
    품목확인 = {}
별칭일치 = 품목확인.get("별칭일치") is True
웹조사 = not 별칭일치                                    # v046: 별칭 일치일 때만 품목 조사를 건너뛴다
print(f"  별칭 일치: {'예 → ② 조사 건너뜀' if 별칭일치 else '아니오 → ② 품목 조사'}"
      + (f" · 화면 안내: {품목확인.get('안내')}" if 품목확인.get("안내") else ""))
# 화면이 ② ③ 요청에 넣는 공통 값. 확정비목 · 세목 · 세부유형 · 공개검색어는 보내지 않는다(서버가 ③ 에서 정한다)
질문공통 = {"사업명": 사업명, "부가세구분": 부가세구분, "품목": 서버정규화.get("품목") or "",
         "금액": 서버정규화.get("금액"), "용도": 서버정규화.get("용도") or "",
         "질문원문": 서버정규화.get("질문원문") or None, "웹조사": 웹조사, "거래처": 거래처, "판매URL": 판매URL}
조사ID = 서버정규화.get("조사ID")
예산_보기()
멈춤("① 입력 정리 끝")

# %% 2. ② 품목 조사 → POST /api/questions {웹조사: true} (화면 2 「…맞나요?」 [맞아요] [직접 입력하기]) ──────────
운영연결.터널_확인()
조사답변: list[dict] = []
품목설명 = 입력설명
조사확인단계 = False                               # 프론트 조사확인단계: 이 뒤 ③ 요청에 조사ID · 조사답변 · 품목설명 을 싣는다
if 별칭일치:
    print("별칭이 맞아 ② 조사를 건너뜁니다. ③ 요청 한 번으로 분류 · 질문을 받습니다.")
else:
    for 회 in range(1, 설정.분류_최대요청 + 1):
        with 기록.단계("품목조사" if 회 == 1 else f"품목조사_다시{회}"):
            r2 = api.post("/api/questions", {**질문공통, "조사ID": 조사ID, **({"품목설명": 입력설명} if 입력설명 else {})})
        완료 = r2.get("완료") or r2.get("값") or {}
        조사ID = 완료.get("조사ID") or 조사ID
        if r2.get("HTTP") == 200 and 완료.get("상태") == "분류없음" and 완료.get("다음행동") == "다시시도":
            # v053: ② 가 시스템 오류로 끝났다. 화면(v048)은 안내 문장과 [다시 시도] 를 보이고 같은 요청을 다시 보낸다
            print(f"\n[② 다시 시도 안내] 화면: 「{완료.get('분류안내')}」 [다시 시도]")
            예산_보기()
            묻기("[다시 시도] (Enter)", None, "")
            continue
        break
    조사 = 완료.get("조사") or {}
    print(f"\n[② 결과] 상태 {완료.get('상태')} · 조사 상태 {조사.get('상태')} · 후보 {len(조사.get('후보') or [])}개 · 출처 {len(조사.get('출처') or [])}개")
    if 조사.get("사유"):
        print("  사유:", 조사["사유"])
    if 조사.get("부족정보"):
        print("  [부족 정보]", 조사["부족정보"])
    if r2.get("HTTP") != 200 or 완료.get("상태") != "조사확인" or not 완료.get("조사ID"):
        # 프론트(lib/questions.ts): 조사확인 · 조사ID 가 아니면 오류로 받고 화면 1 로 돌아간다
        raise 화면멈춤("② 응답이 조사확인이 아니라 화면은 「공개 정보를 확인하지 못했어요. 잠시 후 다시 확인해 주세요.」 와 함께 화면 1 로 돌아갑니다.")
    조사확인단계 = True
    조사답변 = 카드_답(조사.get("후보") or [], 설정.조사_답, "품목 확인")
    if not 조사.get("후보"):
        품목설명 = 한문장_설명("확인된 공개 정보가 없어요. (어떤 제품·서비스인지 한 문장, 비워도 됨)")
    예산_보기()
    멈춤("② 품목 조사 끝")

# %% 3. ③ 분류 → ④-2 판정용 조사 → ⑤ 확인 질문 → POST /api/questions ──────────────
재조사답변: list[dict] = []
질문결과: dict = {}
분류없이_판정 = False
for 회 in range(1, 설정.분류_최대요청 + 1):
    운영연결.터널_확인()
    본문 = {**질문공통, "조사ID": 조사ID,
          **({"조사답변": 조사답변, **({"품목설명": 품목설명} if 품목설명 else {})} if 조사확인단계 else {}),
          **({"재조사답변": 재조사답변} if 조사확인단계 and 재조사답변 else {})}
    with 기록.단계(f"분류_질문{회}" if 회 > 1 else "분류_질문"):
        r3 = api.post("/api/questions", 본문)
    질문결과 = r3.get("완료") or r3.get("값") or {}
    if r3.get("HTTP") != 200:
        raise 화면멈춤(f"HTTP {r3.get('HTTP')} {질문결과}: 화면은 400(만료) · 409(입력 변경)이면 조사ID 없이 ① 부터 한 번 더 묻습니다.")
    조사ID = 질문결과.get("조사ID") or 조사ID
    if 질문결과.get("상태") == "실패" and not 질문결과.get("분류"):
        # 요청 처리 실패(조사 답 오류 · 처리 중 예외). 서버는 질문 결과를 세션에 저장하지 않아 이대로 판정하면 400 이다
        if not 조사확인단계:
            raise 화면멈춤("상태 실패 · 분류 없음: 화면은 「확인할 내용을 받지 못했어요. 다시 확인해 주세요.」 와 함께 화면 1 로 돌아갑니다.")
        print("\n[③ 요청 처리 실패] 화면: 「확인할 내용을 받지 못했어요. 다시 확인해 주세요.」 — 같은 요청을 다시 보냅니다")
        묻기("[규정 확인하기] 다시 (Enter)", None, "")
        continue
    s = 세션()
    if s and s.설명정리:
        print("  [디버그] 한 문장 → 분류 자료 JSON(item_classify.설명_반영):"); 보기(s.설명정리, 1500)
    for r in [r for r in 기록.LLM if r.get("단계") == 기록.현재단계 and "item_classify" in (r.get("호출위치") or "")]:
        # v056: 분류 LLM 은 프롬프트 변형 A · B · C 3번 + 불일치면 조정 1번(프롬프트에 <분류기록>) + 한 문장이면 설명 정리
        위치, 메시지 = r.get("호출위치") or "", str(r.get("메시지") or "")
        종류 = ("조정" if "_조정" in 위치 or "<분류기록>" in 메시지 else
              "설명 정리" if "설명_반영" in 위치 or "<설명>" in 메시지[-2000:] else "분류 변형")
        print(f"  [디버그] {종류} LLM #{r['번호']} · {r.get('지연ms')}ms · 출력 {str(r.get('출력'))[:300]}")
    for x in [x for x in 기록.분류디버그 if x["단계"] == 기록.현재단계]:
        분류디버그_보기(x)
    if 질문결과.get("상태") != "분류없음":
        break
    다음 = 질문결과.get("다음행동")
    조사확인단계 = True                            # 프론트: 분류없음이면 조사확인단계로 두고 같은 화면에서 다시 묻는다
    print(f"\n[③ 분류없음] 다음행동 {다음} · 화면 안내: {질문결과.get('분류안내')}")
    if 다음 == "판정":
        분류없이_판정 = True                      # 프론트 submit(조사ID, 조사답, 질문세트 null): 분류 없이 판정(확인 필요 + 문의 초안)
        break
    if 다음 == "재조사확인":
        후보 = (질문결과.get("재조사") or {}).get("후보") or []
        if (질문결과.get("재조사") or {}).get("종류") == "거래유형":
            # v056 거래 유형 확인 카드: 거래 유형마다 한 문장, 하나만 고른다(출처 없음). 고른 하나만 재조사답변 으로 보낸다
            print(f"  [거래 유형 확인 카드] 후보 {len(후보)}개 · 거래유형 {[c.get('거래유형') for c in 후보]}")
            for i, c in enumerate(후보):
                print(f"    {i + 1}. {c.get('질문')}")
            고른 = (설정.재조사_답 or {}).get("거래유형") or 묻기("맞는 설명 번호", [str(i + 1) for i in range(len(후보))], "1")
            재조사답변 = [{"id": 후보[int(고른) - 1]["id"], "답": "맞아요"}]
        else:
            print(f"  [③-R 재조사] 후보 {len(후보)}개 (세션 재조사 결과 {'있음' if s and s.재조사 is not None else '없음'})")
            재조사답변 = 카드_답(후보, 설정.재조사_답, "더 찾은 품목 정보")
    elif 다음 == "설명입력":
        품목설명 = 한문장_설명(질문결과.get("분류안내") or "") or 품목설명
        if not 품목설명:
            print("  설명 없이 같은 요청을 다시 보냅니다(서버는 설명을 받은 적이 없으면 또 설명입력 을 냅니다).")
    else:                                          # 다시시도(DB · LLM 장애) · 모르는 값
        묻기("[다시 시도] (Enter)", None, "")
    예산_보기()
    멈춤(f"③ 분류없음({다음}) — 같은 화면에서 다시 요청")
else:
    print(f"  ⚠ ③ 요청을 {설정.분류_최대요청}번 보냈는데도 분류 · 판정으로 가지 않았습니다(설정.분류_최대요청).")

질문세트 = 질문결과.get("질문세트")
분류 = 질문결과.get("분류") or {}
확정비목, 확정세목 = 분류.get("비목"), 분류.get("세목")
print(f"\n[③ 결과] 화면 안내: {질문결과.get('분류안내')}")
print(f"  분류: 품목 {분류.get('품목')} → 세목 {확정세목} → 비목 {확정비목} · 표시문 {분류.get('표시문')}" if 분류 else "  분류: 없음 → 분류 없이 판정(확인 필요 + 문의 초안)")
s = 세션()
if s and s.노드결정:
    print(f"  [디버그] 노드결정 {s.노드결정}")
if not MOCK and not 원격:
    try:
        import ontology_lookup  # 디버그: 같은 함수를 직접 다시 불러 L1 대신 L2 로 버린 후보를 본다
        from server._common import 비목_ENUM
        with db.connect(connect_timeout=5, autocommit=True) as _c:
            _cur = _c.cursor()
            _레 = [x for x in ontology_lookup.레이블_목록(_cur, 사업명) if x["비목"] in 비목_ENUM]
            _남, _버 = ontology_lookup.후보_정리(_cur, 사업명, _레)
            print(f"  [디버그] 후보 {len(_남)}개 · L1 대신 L2 로 버린 후보 {len(_버)}개: {[b['정식명'] for b in _버]}")
    except Exception as e:  # noqa: BLE001
        print("  [디버그] 후보 다시 보기 실패:", type(e).__name__, e)
# ④-2 판정용 조사(서버 전용 값, 화면에는 「공개 웹에서 규정에 필요한 정보 N개를 확인했어요.」 줄만)
if s and s.판정조사:
    pj = s.판정조사
    print(f"\n[④-2 판정용 조사] 상태 {pj.get('상태')} · 출처 {len(pj.get('출처') or [])}개")
    for 키, v in (pj.get("요건결과") or {}).items():
        if isinstance(v, dict):
            print(f"  C{v.get('번호')} {v.get('결과')} · 값 {v.get('값')!r} · {v.get('URL') or ''} · 이유 {str(v.get('이유'))[:120]}"
                  + (f"\n      발췌 {str(v.get('발췌'))[:150]}" if v.get("발췌") else ""))
elif not 분류없이_판정:
    print("\n[④-2 판정용 조사] 결과 없음(mock · 시간 부족 · 조건 줄 없음)")
예산_보기()
print(f"\n[⑤ 확인 질문] 상태 {질문결과.get('상태')} · 질문 {len(질문결과.get('질문') or [])}개 · 추가입력 칸 {len(질문결과.get('추가입력') or [])}개")
답변: list[dict] = []
for q in (질문결과.get("질문") or []) if 질문세트 else []:      # 프론트: 질문세트가 없으면 질문을 보이지 않는다
    print(f"\n  {q['id']}. {q['질문문']}\n     이유: {q.get('이유')}")
    if q.get("근거"):
        g = q["근거"]
        print(f"     근거: {g.get('doc_id')} {g.get('조번호')} {g.get('항호') or ''} — {str(g.get('원문'))[:200]}")
    값들 = [x["값"] for x in q.get("선택지") or []]
    답 = (설정.질문_답 or {}).get(q["id"]) or 묻기("답", 값들, "모름" if "모름" in 값들 else (값들[0] if 값들 else ""))
    답변.append({"code": q["id"], "값": 답})
추가입력값: dict = dict(설정.추가입력값 or {})
for 칸 in 질문결과.get("추가입력") or []:
    if 칸["키"] in 추가입력값:
        continue
    선택 = 칸.get("선택지")
    단위 = f" ({칸['단위']})" if 칸.get("단위") else ""
    v = 묻기(f"[칸] {칸['라벨']}{단위} — 비우면 안 보냄", 선택 + [""] if 선택 else None,
            str((서버정규화.get("수치") or {}).get(칸["키"]) or ""))
    if v != "":
        추가입력값[칸["키"]] = v if 선택 else float(v) if "." in v else int(v)
if 질문결과.get("용어풀이"):
    print("  용어풀이:", [x.get("용어") for x in 질문결과["용어풀이"]])
if not (질문결과.get("질문") or 질문결과.get("추가입력")):
    print("  화면 3 없이 바로 판정합니다" + (f" (판정 진행 맨 위 줄: {질문결과.get('분류안내')})" if 질문결과.get("분류안내") else ""))
멈춤("③ 분류 · ④-2 · ⑤ 확인 질문 끝")

# %% 4. ⑥ ⑦ 판정 → POST /api/judge (판정 내부 단계마다 멈춤) ─────────────────
if not 원격:
    운영연결.터널_확인()
# 프론트는 계획을 저장(POST /api/plans)한 뒤 그 계획의 정규화로 판정한다. 같은 프로세스(단계별)는 계획을 저장하지 않으므로(읽기 전용)
# plan_id 없이 같은 값을 보낸다. 디버그 서버(원격)로 보낼 때는 화면처럼 계획을 저장한다(서버 메모리 저장소). 저장할정규화: 서버 정규화 + 수치(입력 칸 값 · 부가세구분) + 사용일자 + 확정세목(서버 분류)
_수치 = {k: v for k, v in (서버정규화.get("수치") or {}).items() if k not in {c["키"] for c in 질문결과.get("추가입력") or []}}
판정정규화 = {**{k: v for k, v in 서버정규화.items() if k != "조사ID"},
          "수치": {**_수치, **추가입력값, "부가세구분": 부가세구분}, "사용일자": 사용일자,
          **({"확정세목": 확정세목} if 확정세목 else {})}
import uuid  # noqa: E402
멱등키 = uuid.uuid4().hex                       # v052/v053: 화면이 점검을 시작할 때 한 번 만들고 [다시 점검] 에서 같은 값을 보낸다
판정본문 = {"정규화": 판정정규화, "확정비목": 확정비목, "사업명": 사업명, "답변": 답변, "멱등키": 멱등키,
          "조사ID": 조사ID, "조사답변": 조사답변 if 조사확인단계 else [],
          **({"질문세트": 질문세트} if 질문세트 and not 분류없이_판정 else {}),
          **({"확정세목": 확정세목} if 확정세목 else {}),
          **({"추가입력값": 추가입력값} if 추가입력값 else {}),
          "부가세구분": 부가세구분, **({"사용일자": 사용일자} if 사용일자 else {})}
계획번호 = None
if 원격:
    # 프론트 저장 단계: POST /api/plans(계획생성) → 실패하면 판정을 시작하지 않는다. 판정은 plan_id 를 싣는다
    _계획 = api.c.post("/api/plans", json={"사업명": 사업명, "품목": 판정정규화.get("품목") or 화면칸["품목"],
                                         "금액": 판정정규화.get("금액") or 화면칸["금액"] or 0, "용도": 판정정규화.get("용도") or "",
                                         "제목": 화면칸["품목"] or 판정정규화.get("품목"), "집행예정일": 사용일자,
                                         "거래처": 거래처, "확정비목": 확정비목, "정규화": 판정정규화},
                       headers={"authorization": "Bearer debug"})
    print(f"  [계획 저장] POST /api/plans → HTTP {_계획.status_code} {str(_계획.text)[:200]}")
    if not 200 <= _계획.status_code < 300:
        raise 화면멈춤("계획 저장 실패: 화면은 판정을 시작하지 않는다")
    계획번호 = _계획.json()["plan_id"]
    판정본문["plan_id"] = 계획번호
for 회 in range(1, 3):
    with 기록.단계("판정" if 회 == 1 else f"판정_다시점검{회}"):
        r4 = api.post("/api/judge", 판정본문)
    if r4.get("HTTP") != 200:
        print(f"  ⚠ HTTP {r4.get('HTTP')} {r4.get('값')}: 화면은 「지출 계획은 저장했지만 AI 점검을 마치지 못했습니다」 로 상세 화면에 보냅니다.")
    화면결과 = r4.get("결과") or {}
    if not 화면결과.get("판정실패"):
        break
    # v052 · v053: 시스템 오류가 예산까지 이어졌다. 화면은 요약 문장과 [다시 점검] [닫기]. 다시 점검은 같은 멱등키로 보낸다
    # v062: 결정적 요청 오류(400 등)면 판정실패원인 「요청오류」 → 화면은 「AI가 이 점검 요청을 처리하지 못했어요.」(다시 점검해도 같을 수 있음)
    print(f"\n[판정실패] 화면: 「{화면결과.get('요약')}」 [다시 점검] [닫기] · 판정실패원인 {화면결과.get('판정실패원인') or '없음(연결 불안정)'}"
          f" · 실패단계 {(기록.판정_결과 or {}).get('실패단계')}")
    if 회 == 2 or 묻기("[다시 점검] / [닫기]", ["다시 점검", "닫기"], "닫기") != "다시 점검":
        break
예산_보기()
(기록.단계폴더 / "화면결과.json").write_text(추적._json(화면결과), encoding="utf-8")
print(f"\n[판정 결과] 판정 {화면결과.get('판정')} · 신뢰등급 {화면결과.get('신뢰등급')}")
print("요약:", 화면결과.get("요약"))
# 판정 앙상블(SUDDOE_JUDGE_MODELS): 모델별 판정 · 상태 · 토큰 · 지연과 다수결 · 대표 출력. 화면에는 다수결 판정과 대표 출력만 나간다
추적.앙상블_보기((기록.판정_결과 or {}).get("모델"), (기록.판정_결과 or {}).get("검증사유"))
for 키 in ("해야할일", "조건", "기한조건", "금지조건", "해당시조건", "공통사항", "확인한조건", "코드판정", "근거경로", "분류"):
    if 화면결과.get(키):
        print(f"\n[{키}]"); 보기(화면결과[키], 2000)
_초안 = next((v for n, v in r4.get("이벤트") or [] if n == "문의초안"), None)
print("\n[문의초안]" if _초안 else "\n[문의초안] 없음 (근거부족이 아님)")
if _초안:
    보기(_초안, 2000)
print("\n[인용]")
for c in 화면결과.get("인용") or []:
    print(f"  · {c.get('doc_id')} {c.get('조번호')} {c.get('항호') or ''}: {str(c.get('원문') or c.get('문장') or '')[:200]}")
if 계획번호:
    _상세 = api.get(f"/api/plans/{계획번호}").get("값") or {}
    print(f"\n[계획 상세 GET /api/plans/{계획번호}] 상태 {_상세.get('상태')} · 판정 {_상세.get('판정')} · "
          f"latest_decision_id {_상세.get('latest_decision_id')} · 할 일 {len(_상세.get('할일') or [])}건")
print("\n[내부: 화면에는 안 나옴]")
_내부 = 기록.판정_결과 or {}                   # orchestrate.판정() 반환(라우트가 화면 응답으로 바꾸기 전)
보기({k: _내부.get(k) for k in ("게이트", "처리단계", "실패단계", "실패경로", "검증코드", "검증사유", "지연ms", "모델") if k in _내부}, 3000)


def _판정출력(r: dict) -> dict | None:
    try:
        v = json.loads(r.get("출력") or "")
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) and "조건판단" in v else None


_판정LLM = next((v for r in reversed(기록.LLM) if r.get("단계") == 기록.현재단계 and (v := _판정출력(r))), None)
if _판정LLM:
    print("  [디버그] 판정 LLM 조건판단(코드 검사 전, 마지막으로 끝난 판정 호출):"); 보기(_판정LLM.get("조건판단"), 3000)
    print(f"  [디버그] 판정 LLM 판정 {_판정LLM.get('판정')} → 화면 판정 {화면결과.get('판정')}")
# 판정 앙상블이면 모델마다 호출이 따로 남는다(LLM.md 에 프롬프트 · 출력 전문). 모델별 마지막 출력의 판정
for _모델, _출력 in {r.get("모델"): v for r in 기록.LLM if r.get("단계") == 기록.현재단계 and (v := _판정출력(r))}.items():
    print(f"  [디버그] 판정 LLM {_모델}: 판정 {_출력.get('판정')} · 인용 {_출력.get('인용')}")
# 재시도(v052 · v053): 판정 행 모델 칸(시도 · 멱등키 · 재요청 · 재시도)과 처리단계 판정재요청:n
_재요청단계 = [x for x in str((기록.판정_결과 or {}).get("처리단계") or "").split("+") if x.startswith("판정재요청")]   # 처리단계는 「+」 로 이은 글
print(f"\n[재시도] 처리단계 판정재요청: {_재요청단계 or '없음'}")
if 기록.저장될_판정:
    try:
        _모델 = json.loads(기록.저장될_판정[-1].get("모델") or "{}")
    except (TypeError, ValueError):
        _모델 = {}
    print(f"  [저장될 판정행 모델] 판정 LLM 시도 {_모델.get('시도')} · 멱등키 {_모델.get('멱등키')} · 재요청 {_모델.get('재요청')}")
    추적.재시도_보기(_모델.get("재시도") or [], "판정 요청 재시도(모델.재시도)")
else:
    print("  [저장될 판정행] 없음(mock · 판정실패면 행을 저장하지 않는다)")
멈춤("판정 끝")


# %% 5. 판정만 다시 (선택) ──────────────────────────────────────
def 판정_다시(**바꿀값):
    """4번 칸에서 라우트가 orchestrate.판정() 에 넘긴 인자 그대로(바꿀값만 덮어써) 판정만 다시 돈다. 화면 응답 모양으로 돌려준다.

    예) 판정_다시(확정세목="소프트웨어 이용료")   판정_다시(사용자답변=[])
    설정.핫리로드_대상 모듈(assemble_context · retrieve · orchestrate …)을 고쳤으면 먼저 다시 읽는다(핫 리로드).
    그 밖의 모듈(server/* · llm_qwen 등)을 고쳤으면 커널을 재시작하고 0번 칸부터 다시 돈다.
    """
    리로드.확인()
    if not 기록.판정_인자:
        raise RuntimeError("4번 칸 판정을 먼저 돌려야 인자가 있습니다(mock 은 orchestrate.판정 을 부르지 않습니다)")
    운영연결.터널_확인()
    인자 = {**기록.판정_인자, **바꿀값}
    질문 = 인자.pop("질문")
    with 기록.단계("판정다시"):
        with research_runtime.activate(research_runtime.Budget(), emit=lambda e: 기록.이벤트_받음("진행", e)):
            r = orchestrate.판정(질문, **인자)
    out = judge_response.파이프라인결과_매핑(r, 판정요청(**판정본문))
    print(f"판정 {out.get('판정')} · {out.get('요약')}")
    return out

# 판정_다시()

# %% 6. 전체 요약 ─────────────────────────────────────────────────
print("\n━━━━━━━━━━━━ 전체 요약 ━━━━━━━━━━━━")
for r in 기록.LLM:
    print(f"  LLM #{r['번호']} [{r['단계']}] {r['호출위치']} · {r.get('모델')} · {r.get('지연ms')}ms · "
          f"{'실패 ' + r['오류'][:80] if r.get('오류') else r.get('종료이유')}")
print(f"  SQL {len(기록.SQL)}개 · 화면 이벤트 {len(기록.이벤트)}개 · 판정 내부 단계 {len(기록.판정내부)}개")
print(f"  ① 입력 검증 판단 {입력검증.get('판단')} · 시도 {입력검증_시도}번 · 설명입력 {'예' if 입력설명 else '아니오'}")
for x in 기록.분류디버그:
    d = x.get("디버그") or {}
    print(f"  ③ [{x['단계']}] 변형 {[(v.get('변형'), v.get('표시문'), v.get('거래유형')) for v in d.get('변형') or []]} · "
          f"불일치 {d.get('불일치')} · 조정 {'예' if d.get('조정') else '아니오'} · 거래 유형 확인 질문 {d.get('거래유형질문')} · "
          f"다음행동 {x.get('다음행동')}")
추적.재시도_보기(기록.재시도, "이번 실행 시스템 오류 재시도 전부")
if 기록.막힌_쓰기:
    print("  막힌 DB 쓰기:", 기록.막힌_쓰기)
print("  기록 폴더:", 기록.폴더)
print("  Interactive 에서: 추적.LLM_보기(기록, 1) · 추적.SQL_보기(기록, 3, 다시실행=True) · 추적.호출_찾기(기록, '온톨로지_조회')")
