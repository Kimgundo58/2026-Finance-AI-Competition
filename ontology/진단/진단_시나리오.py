# -*- coding: utf-8 -*-
"""진단용 화면 경로 시나리오 실행기 (저장소 밖, 쓰기 없음).

화면 순서를 함수 호출로 따라간다: _live_정규화 → 질문생성 → _live_판정(routes_judge) → 파이프라인결과_매핑.
캐시 · 저장은 부르지 않고 orchestrate.판정 은 기록=False 로 강제한다. tenant 에 쓰지 않는다.
  --mode dry   LLM 없음. 정규화는 폼 값, 질문은 코드 재료만, 판정은 dry=True
  --mode real  실제 LLM (정규화 · 질문 · 판정). 시나리오당 1회
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(os.environ["REPO"])
OUT = Path(os.environ["OUT"])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["SUDDOE_MOCK"] = "0"
사업 = "모두의 창업 프로젝트"
os.environ["SUDDOE_ONTOLOGY_PROGRAMS"] = 사업
os.environ.pop("SUDDOE_JUDGE_ANSWERS", None)

from _lib import db  # noqa: E402
import normalize_run  # noqa: E402
import ontology_gate  # noqa: E402
import ontology_context  # noqa: E402
import ontology_lookup  # noqa: E402
import orchestrate  # noqa: E402
import question_generate  # noqa: E402
from server.catalog import _비목_표준화  # noqa: E402
from server.models import 정규화요청, 판정요청  # noqa: E402
from server import routes_judge, routes_normalize  # noqa: E402
from server.judge_response import 할일_공개  # noqa: E402

_국소 = threading.local()
_원래_판정 = orchestrate.판정


def _세기(이름, 원래):
    def f(프롬프트, *a, **k):
        t = time.time()
        try:
            r = 원래(프롬프트, *a, **k)
            메타 = r[1] if isinstance(r, tuple) and len(r) > 1 else {}
            _국소.llm.append({"단계": 이름, "자수": len(프롬프트), "초": round(time.time() - t, 1), "성공": True,
                            "모델": (메타 or {}).get("모델")})
            return r
        except Exception as e:
            _국소.llm.append({"단계": 이름, "자수": len(프롬프트), "초": round(time.time() - t, 1), "성공": False,
                            "오류": f"{type(e).__name__}: {str(e)[:160]}"})
            raise
    return f


normalize_run.llm_호출 = _세기("정규화", normalize_run.llm_호출)
question_generate.llm_호출 = _세기("질문", question_generate.llm_호출)
orchestrate.llm_호출 = _세기("판정", orchestrate.llm_호출)


def _판정_감싸기(질문, **k):
    k["기록"] = False
    if getattr(_국소, "dry", False):
        k["dry"] = True
    r = _원래_판정(질문, **k)
    _국소.r = r
    return r


orchestrate.판정 = _판정_감싸기

내부값식 = re.compile(r"규범:|세목:|품목:|비목:창업|조:[가-힣]|절차:|주체:|증빙:|사업:|시간:|미특정:|\[S\d+\]|\bS\d{2}\b|[①-⑳]#\d|#\d|"
                   r"\b[A-Z]{3,}(?:_[A-Z]+)+\b|L[123]_|verified|AI검증|AI대조|None|null")


def _내부값_찾기(값, 경로="") -> list[str]:
    out: list[str] = []
    if isinstance(값, dict):
        for k, v in 값.items():
            out += _내부값_찾기(v, f"{경로}.{k}")
    elif isinstance(값, list):
        for i, v in enumerate(값):
            out += _내부값_찾기(v, f"{경로}[{i}]")
    elif isinstance(값, str):
        for m in 내부값식.finditer(값):
            out.append(f"{경로}: {m.group(0)} ← {값[max(0, m.start() - 30):m.end() + 30]}")
    return out


def _조건_풀이(조회: dict) -> list[dict]:
    """게이트 조건 문장 → 규범 ID · 수범자 · 자연어 조건(진단용. 화면에 나가지 않는다)."""
    규범들 = 조회["적용"]["규범"]
    표: dict[str, list[dict]] = {}
    for n in 규범들:
        표.setdefault(ontology_gate._규범문장(n), []).append(n)
    out = []
    for c in 조회["게이트"].get("조건") or []:
        ns = 표.get(c["문장"]) or []
        out.append({"문장": c["문장"], "구분": c.get("구분"), "시점": c.get("시점"),
                    "규범": [n["norm_id"] for n in ns],
                    "수범자": sorted({s for n in ns for s in n.get("수범자") or []}),
                    "자연어조건": [ontology_gate._자연어조건(n) for n in ns if ontology_gate._자연어조건(n)][:2],
                    "구조화조건": [ontology_gate._구조화조건(n) for n in ns if ontology_gate._구조화조건(n)][:2],
                    "대상": sorted({v for n in ns for v in n.get("대상") or []})[:4]})
    return out


def _내부_조회(cur, s, 비목, 세목, 부가세, 품목) -> dict:
    입력 = orchestrate.게이트_입력(s.get("금액"), 부가세, s.get("추가"))
    조회 = orchestrate.온톨로지_조회(cur, 사업, 비목, 세목, 입력, 확정=True, 품목=품목)
    g = 조회["게이트"]
    종류별: dict[str, int] = {}
    for n in 조회["적용"]["규범"]:
        종류별[n["종류"]] = 종류별.get(n["종류"], 0) + 1
    return {"시작노드": sorted(조회["노드들"]), "규범수": len(조회["적용"]["규범"]), "종류별": 종류별,
            "AI검증이상": sum(1 for n in 조회["적용"]["규범"] if n.get("verified")),
            "게이트": {"종류": g.get("종류"), "상한": g.get("상한"), "미검증만": g.get("미검증만"), "검증코드": g.get("검증코드"),
                     "코드판정": g.get("코드판정"), "발동규범": (g.get("발동규범") or {}).get("norm_id"),
                     "쓴규범": g.get("쓴규범"), "제외규범": g.get("제외규범"),
                     "활성의무": [n["norm_id"] for n in g.get("활성의무") or []]},
            "조건풀이": _조건_풀이(조회), "조건수": len(g.get("조건") or []),
            "우선순위_적용": [{k: v for k, v in u.items() if k in ("채택", "제외")} for u in 조회["적용"]["우선순위_적용"]][:20]}


def 실행(s: dict, mode: str) -> dict:
    _국소.llm = []
    _국소.r = None
    _국소.dry = mode == "dry"
    t0 = time.time()
    out: dict = {"id": s["id"], "제목": s["제목"], "출처": s.get("출처"), "입력": {k: s.get(k) for k in
                 ("품목", "용도", "금액", "부가세", "확정비목", "확정세목", "추가", "답정책", "부가세답", "org")}, "기대": s.get("기대")}
    org = s.get("org")
    부가세 = s.get("부가세")
    # ① 입력 → 분류 후보
    body = 정규화요청(품목=s["품목"], 금액=s.get("금액"), 용도=s.get("용도"), 사업명=사업, 부가세구분=부가세)
    if mode == "real":
        정규화, 실패 = routes_normalize._live_정규화(body)
        정규화.setdefault("질문원문", routes_normalize._합성_질문(body))
        out["정규화_실패"] = 실패
    else:
        with db.connect(autocommit=True) as c:
            레이블 = [x for x in ontology_lookup.레이블_목록(c.cursor(), 사업) if x["비목"] in routes_normalize.비목_ENUM]
            일치 = ontology_lookup.별칭_후보(c.cursor(), s["품목"], 사업, 레이블)
        정규화 = {"품목": s["품목"], "금액": s.get("금액"), "금액_추정여부": False, "용도": s.get("용도") or "",
               "질문원문": routes_normalize._합성_질문(body), "질의문": f'{s.get("용도") or ""} {s["품목"]}'.strip(),
               "분류후보": [{k: c.get(k) for k in ("비목", "세목", "표시문")} for c in 일치],
               "비목후보": [{"비목": c["비목"], "신뢰도": 1.0} for c in 일치], "수치": {"부가세구분": 부가세 or "모름"}}
    out["정규화"] = {k: 정규화.get(k) for k in ("품목", "용도", "금액", "질의문", "분류후보", "비목후보", "수치", "거래처유형")}
    # ② 분류 확인: 시나리오가 정한 값, 없으면 첫 후보(화면 자동 선택)
    if s.get("확정비목"):
        비목, 세목 = s["확정비목"], s.get("확정세목")
        out["확정방식"] = "시나리오 지정"
    else:
        첫 = (정규화.get("분류후보") or [{}])[0]
        비목, 세목 = 첫.get("비목"), 첫.get("세목")
        out["확정방식"] = "첫 후보"
    out["확정"] = {"비목": 비목, "세목": 세목}
    if not 비목:
        out["끝"] = "분류 후보 없음: 사용자가 비목을 직접 골라야 한다"
        out["llm"] = _국소.llm
        return out
    비목 = _비목_표준화(비목)
    # ③ 추가 입력 + 확인 질문
    입력q = {"사업명": 사업, "비목": 비목, "세부유형": 세목, "품목": s["품목"], "용도": 정규화.get("용도"),
            "금액": s.get("금액"), "질문": 정규화.get("질문원문"), "확정세목": 세목, "부가세구분": 부가세}
    저장값 = None
    if mode == "real":
        r = question_generate.질문생성(입력q, org_id=org)
        저장값 = {"질문": r.get("질문") or []}
        out["질문단계"] = {"상태": r.get("상태"), "추가입력": r.get("추가입력"),
                       "질문": [{**question_generate.질문_공개(q), "충족답_내부": q.get("충족답"), "종류": q.get("종류")}
                              for q in r.get("질문") or []],
                       "메타": {k: v for k, v in (r.get("메타") or {}).items() if k in ("LLM호출", "사유", "llm_ms", "프롬프트자수", "버린질문")}}
    else:
        with db.connect(autocommit=True) as c:
            if org:
                c.execute("SELECT set_config('app.org_id', %s, false), set_config('app.account_id', '', false)", (org,))
            try:
                준비 = question_generate._새경로_준비(c.cursor(), 입력q, org)
            except Exception as e:  # noqa: BLE001
                준비 = {"바로": f"예외:{type(e).__name__}:{e}"}
        out["질문단계"] = {"바로": 준비.get("바로"), "추가입력": 준비.get("추가입력"),
                       "코드질문": [q["질문문"] for q in 준비.get("코드질문") or []],
                       "문맥": {k: 준비["문맥"][k] for k in ("항목수", "글자수", "빠진수", "온톨로지항목수")} if 준비.get("문맥") else None}
    # ④ 판정: 사용자 답
    사용자답변, 부가세값 = None, 부가세
    if 저장값 and 저장값["질문"]:
        정책 = s.get("답정책") or "충족"
        답변들 = []
        for q in 저장값["질문"]:
            if q.get("종류") == "부가세":
                if s.get("부가세답"):
                    답변들.append({"code": q["id"], "값": s["부가세답"]})
                continue
            값 = q.get("충족답") if 정책 == "충족" else ({"예": "아니오", "아니오": "예"}.get(q.get("충족답"), "모름") if 정책 == "불충족" else "모름")
            답변들.append({"code": q["id"], "값": 값})
        out["답변"] = 답변들
        사용자답변 = question_generate.답변_구성(저장값, 답변들) or None
        부가세답 = question_generate.부가세_답(저장값, 답변들)
        if 부가세 in (None, "모름") and 부가세답:
            부가세값 = 부가세답
    jb = 판정요청(정규화=정규화, 확정비목=비목, 사업명=사업, org_id=org, 확정세목=세목,
              추가입력값=s.get("추가") or {}, 부가세구분=부가세값)
    jb.정규화 = {k: v for k, v in jb.정규화.items() if k != "심층질문"}
    응답 = routes_judge._live_판정(jb, None, 사용자답변, False)
    r = _국소.r or {}
    공개 = {k: v for k, v in 응답.items() if k not in ("증빙목록", "decision_id", "_실패")}
    공개["해야할일"] = 할일_공개(공개.get("해야할일"))
    out["판정응답"] = 공개
    out["파이프라인"] = {k: r.get(k) for k in ("판정", "게이트", "처리단계", "검증코드", "검증사유", "프롬프트길이", "문장맵_크기", "문맥요약", "지연ms", "경로", "실패경로")}
    if mode == "dry" and r.get("프롬프트"):
        (OUT / f"dry_{s['id']}_prompt.txt").write_text(r["프롬프트"], encoding="utf-8")
    with db.connect(autocommit=True) as c:
        out["내부"] = _내부_조회(c.cursor(), s, 비목, 세목, 부가세값, 정규화.get("품목"))
    out["내부값_검출"] = _내부값_찾기({k: 공개.get(k) for k in ("요약", "조건", "해야할일", "근거경로", "코드판정", "전제", "분류", "문의초안")}
                               | {"인용": [{k: v for k, v in c.items() if k in ("조번호", "조제목", "항호", "문서", "문서명", "원문")} for c in 공개.get("인용") or []],
                                  "질문": [{k: q.get(k) for k in ("질문문", "이유", "근거")} for q in (out.get("질문단계", {}).get("질문") or [])],
                                  "추가입력": out.get("질문단계", {}).get("추가입력")})
    out["llm"] = _국소.llm
    out["총초"] = round(time.time() - t0, 1)
    return out


def 한줄(o: dict) -> str:
    j = o.get("판정응답") or {}
    i = o.get("내부") or {}
    g = i.get("게이트") or {}
    칸 = [x.get("키") for x in (o.get("질문단계") or {}).get("추가입력") or []]
    return (f"{o['id']:<5} {str(j.get('판정')):<5} 게이트={g.get('종류')} 상한={g.get('상한')} 규범={i.get('규범수')} 조건={len(j.get('조건') or [])} "
            f"할일={len(j.get('해야할일') or [])} 인용={len(j.get('인용') or [])} 칸={칸} llm={[(x['단계'], x['초'], x['성공']) for x in o.get('llm', [])]} "
            f"내부값={len(o.get('내부값_검출') or [])} | {o['제목']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["dry", "real"], default="dry")
    p.add_argument("--ids", default="")
    p.add_argument("--workers", type=int, default=1)
    a = p.parse_args()
    sys.path.insert(0, str(Path(__file__).parent))
    from 진단_시나리오표 import 시나리오
    고른 = [s for s in 시나리오 if not a.ids or s["id"] in a.ids.split(",")]
    요약: list[str] = []

    def 하나(s):
        try:
            o = 실행(s, a.mode)
        except Exception as e:  # noqa: BLE001
            o = {"id": s["id"], "제목": s["제목"], "예외": f"{type(e).__name__}: {e}", "트레이스": traceback.format_exc()[-1500:]}
        (OUT / f"{a.mode}_{s['id']}.json").write_text(json.dumps(o, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        줄 = 한줄(o) if "예외" not in o else f"{s['id']} 예외 {o['예외']}"
        print(줄, flush=True)
        return 줄

    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        요약 = list(ex.map(하나, 고른))
    (OUT / f"{a.mode}_요약.txt").write_text("\n".join(요약), encoding="utf-8")


if __name__ == "__main__":
    main()
