# -*- coding: utf-8 -*-
"""정산 리허설 — F3 집행내역 전건 판정 + 리스크 요약 (`Agent.md` §9 우선순위 4).

무엇을 푸는가
  정산은 끝나고 나서 터진다. 이미 쓴 돈을 환수당하는 게 이 도메인의 최악 결과다.
  그래서 **정산 전에** 집행내역을 통째로 다시 판정해 위험 건을 먼저 보여준다.
  판정기 자체는 이미 있다 — 이 파일은 **일괄 실행기 + 리스크 집계기**다.

🔴 **판정 경로에 끼어들지 않는다.** 사용자 요청으로 도는 비동기 배치이고 지연 예산이 없다.
   건수가 많으면 오래 걸린다 — 그건 정상이다. 온라인 판정과 같은 코드를 쓰되 같은 창에서 돌지 않는다.

🔴 **A9(`judge_cli`) 완성 후에 실호출로 붙는다.** 그전까지는 `--dry` 가 기본이고
   `orchestrate.판정(dry=True)` 로 배관만 태운다 (LLM 0회). `--live` 로 실호출한다.
   dry 결과로 위험 건수를 세지 마라 — 그 숫자는 판정 품질이 아니라 배관이 뚫렸는지만 말한다.

⚠️ **현물이 없다** (2026-08-31). `f_exec.형태` 가 DROP 됐다.
   집계 축은 `재원`(정부지원|자기부담) 뿐이다. 현금/현물로 그룹하던 코드는 이제 깨진다.

리스크 등급
    높음   판정이 `불가`                       → 환수 위험. 금액을 합산해 보여준다
    보통   `조건부` 인데 증빙·사전승인이 남았다  → 서류로 막을 수 있다
    확인   `판단불가` — **모델이 스스로 고른 것**  → 기관 문의가 필요하다 (화면 9 경로)
    점검   `판단불가` — **실패 경로로 닫힌 것**    → 🔴 사용자 리스크가 아니라 **우리 결함**이다
    낮음   `가능`

🔴 **`확인` 과 `점검` 을 섞으면 리허설이 거짓말을 한다.** 2026-09-01 실전 E2E 에서
   판단불가 5건이 **전부 `max_tokens=1500` 잘림**이었고 모델이 스스로 고른 건 0건이었다.
   그걸 뭉뚱그려 "확인 5건 — 기관에 문의하세요" 라고 내놓으면, 우리 배관 결함을
   사용자에게 숙제로 떠넘기는 것이 된다. 가르는 기준은 `score_judgment.py` 와 **같다** —
   `실패단계` 가 있거나 `경로` 에 실패·예외·dry 가 박혔으면 모델의 선택이 아니다.
   두 곳이 다른 기준을 쓰면 회귀 게이트와 리허설이 서로 다른 말을 한다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/rehearsal.py --profile <uuid>
    PYTHONIOENCODING=utf-8 python scripts/rehearsal.py --fixture     # f_exec 0행일 때
    PYTHONIOENCODING=utf-8 python scripts/rehearsal.py --profile <uuid> --live   # A9 이후
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from agent_a4 import 적재, 접기                                        # noqa: E402

DSN = os.environ.get("SUDDOE_DSN", "postgresql://postgres:devpw@localhost:5432/suddoe")

# f_exec 0행이라 배관을 태울 입력이 없다. 판정 경로가 실제로 도는지 보기 위한 합성 6건.
# 🔴 **실데이터가 아니다.** 이 숫자로 리스크를 논하지 말 것.
_픽스처 = [
    {"비목": "기계장치", "재원": "정부지원", "거래처": "○○컴퓨터", "인력역할": "대표",
     "귀속월": "2026-03", "금액": 2_500_000},
    {"비목": "지급수수료", "재원": "정부지원", "거래처": "○○컨설팅", "인력역할": None,
     "귀속월": "2026-04", "금액": 300_000},
    {"비목": "인건비", "재원": "정부지원", "거래처": None, "인력역할": "개발자",
     "귀속월": "2026-04", "금액": 3_000_000},
    {"비목": "여비", "재원": "정부지원", "거래처": "○○항공", "인력역할": "대표",
     "귀속월": "2026-05", "금액": 1_200_000},
    {"비목": "광고선전비", "재원": "자기부담", "거래처": "○○기획", "인력역할": None,
     "귀속월": "2026-05", "금액": 800_000},
    {"비목": "교육훈련비", "재원": "정부지원", "거래처": "○○아카데미", "인력역할": "개발자",
     "귀속월": "2026-06", "금액": 500_000},
]

등급표 = {"불가": "높음", "조건부": "보통", "판단불가": "확인", "가능": "낮음"}
등급순 = ("높음", "보통", "확인", "점검", "낮음")


def 실패로닫힌건가(r: dict) -> bool:
    """🔴 `score_judgment.py` 의 판단불가 분해와 **같은 기준**을 쓴다 (A 세션, 2026-09-01)."""
    경로 = str(r.get("경로") or "")
    return bool(r.get("실패단계")) or any(k in 경로 for k in ("실패", "예외", "dry"))


def 등급(r: dict) -> str:
    g = 등급표.get(r.get("판정"), "확인")
    return "점검" if g == "확인" and 실패로닫힌건가(r) else g


def 질문문장(행: dict) -> str:
    """집행 1건 → 판정기가 먹을 자연어 한 줄.

    🔴 정규화를 건너뛰지 않는다. 집행내역은 이미 구조화돼 있지만, 온라인 판정과
       **같은 경로**로 넣어야 리허설 결과가 실제 판정과 같은 뜻을 갖는다.
       구조화 값을 바로 룰 조회에 꽂으면 정규화 단계의 오차가 리허설에서만 사라진다.
    """
    조각 = [f"{행['비목']}으로", 행.get("거래처") and f"{행['거래처']}에서", ]
    조각 += [f"{int(행['금액']):,}원을", f"{행.get('귀속월') or ''}에 집행했습니다."]
    끝 = " ".join(x for x in 조각 if x)
    if 행.get("인력역할"):
        끝 += f" 대상 인력 역할은 {행['인력역할']}입니다."
    return 끝 + " 정산에 문제가 없나요?"


def 집행내역(conn, profile_id: str | None, 픽스처: bool) -> tuple[list[dict], str]:
    if 픽스처:
        return list(_픽스처), "합성 픽스처 (🔴 실데이터 아님)"
    행들 = conn.execute("""
        SELECT "비목", "재원", "거래처", "인력역할", "귀속월", "금액"
        FROM tenant.f_exec
        WHERE (%s::uuid IS NULL OR profile_id = %s::uuid)
        ORDER BY "귀속월", exec_id
    """, (profile_id, profile_id)).fetchall()
    키 = ("비목", "재원", "거래처", "인력역할", "귀속월", "금액")
    if not 행들:
        return list(_픽스처), "f_exec 0행 → 합성 픽스처로 대체 (🔴 실데이터 아님)"
    return [dict(zip(키, r)) for r in 행들], f"tenant.f_exec {len(행들)}건"


def 한건(질문: str, 사업명: str | None, org_id: str | None, live: bool,
        *, conn=None, 기관ID: str | None = None) -> dict:
    """판정 1건. 🔴 A9(`judge_cli`)와 **같은 엔진**(`orchestrate.판정`)을 부른다 —
    리허설이 다른 코드로 판정하면 "리허설은 통과했는데 실제로는 불가" 가 나온다.

    `conn` 을 넘겨 커넥션을 재사용한다. 전건 배치라 건마다 새로 붙으면 그것만으로
    수백 번 접속한다 (`판정` 은 `닫기 = conn is None` 이라 넘긴 커넥션을 닫지 않는다).
    """
    import orchestrate
    t = time.time()
    try:
        r = orchestrate.판정(질문, 사업명=사업명, org_id=org_id, 기관ID=기관ID,
                            dry=not live, 기록=False, conn=conn)
    except Exception as e:                                            # noqa: BLE001
        # 🔴 실패의 기본값은 판단불가다. 리허설이 조용히 건을 빠뜨리면 안 된다
        return {"판정": "판단불가", "요약": f"판정 실패 ({type(e).__name__}: {str(e)[:120]})",
                "해야할일": [], "인용": [], "신뢰등급": None, "경로": "예외",
                "실패단계": "리허설", "강등코드": ["REHEARSAL_ERROR"],
                "지연ms": int((time.time() - t) * 1000)}
    지연 = r.get("지연ms")
    return {"판정": r.get("판정") or "판단불가", "요약": r.get("요약") or "",
            "해야할일": r.get("해야할일") or [],
            # 인용은 조번호만 남긴다 — 원문까지 담으면 결과 JSON 이 수 MB 가 된다
            "인용": [c.get("조번호") for c in (r.get("인용") or []) if isinstance(c, dict)],
            "신뢰등급": r.get("신뢰등급"), "경로": r.get("경로"),
            "실패단계": r.get("실패단계"),
            "강등코드": r.get("강등코드") or [],
            "지연ms": 지연.get("총") if isinstance(지연, dict) else 지연}


def main() -> None:
    ap = argparse.ArgumentParser(description="정산 리허설 — 집행내역 전건 판정 + 리스크 요약")
    ap.add_argument("--profile", help="tenant.f_profile.profile_id")
    ap.add_argument("--org", help="tenant.orgs.org_id (L3 경로)")
    ap.add_argument("--기관", dest="기관ID", help="기관ID — 인용 누수(TENANT_LEAK) 검사 기준")
    ap.add_argument("--사업명", dest="사업명")
    ap.add_argument("--fixture", action="store_true", help="f_exec 대신 합성 픽스처")
    ap.add_argument("--live", action="store_true",
                    help="🔴 A9(judge_cli) 완성 후. LLM 을 실제로 부른다")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--queue", action="store_true",
                    help="'높음' 건을 corpus.recheck_queue 에 남긴다")
    ap.add_argument("--out", default="scripts/_work/_리허설_결과.json")
    a = ap.parse_args()

    with psycopg.connect(DSN) as conn:
        행들, 출처 = 집행내역(conn, a.profile, a.fixture)
        if a.limit:
            행들 = 행들[:a.limit]
        모드 = "live (LLM 실호출)" if a.live else "dry (LLM 0회 — 배관 검증만)"
        print(f"정산 리허설 — 입력 {len(행들)}건 · 출처 {출처} · 모드 {모드}")
        if not a.live:
            print("🔴 dry 결과로 리스크를 논하지 마라. 배관이 뚫렸는지만 말한다 (A9 대기).")

        결과, 집계, 위험금액 = [], {}, {}
        for i, 행 in enumerate(행들, 1):
            q = 질문문장(행)
            r = 한건(q, a.사업명, a.org, a.live, conn=conn, 기관ID=a.기관ID)
            g = 등급(r)
            집계[g] = 집계.get(g, 0) + 1
            위험금액[g] = 위험금액.get(g, 0) + float(행["금액"])
            결과.append({**행, "질문": q, **r, "리스크": g})
            print(f"  [{g:2}] {i:>3}/{len(행들)} {행['비목']:<10} "
                  f"{int(행['금액']):>10,}원  {r['판정']:<5} {r['요약'][:48]}")

        강등 = {}
        for x in 결과:
            for c in x.get("강등코드") or []:
                강등[c] = 강등.get(c, 0) + 1
        총액 = sum(float(x["금액"]) for x in 행들)
        print("\n" + "=" * 74)
        print(f"집행 총액 {int(총액):,}원 · {len(행들)}건")
        for g in 등급순:
            if g in 집계:
                print(f"  {g:<3} {집계[g]:>3}건  {int(위험금액[g]):>12,}원  "
                      f"({위험금액[g]/총액*100:4.1f}%)")
        if 집계.get("점검"):
            # 🔴 사용자에게 보여줄 리스크가 아니다. 우리가 고칠 것이다
            print(f"  🔴 '점검' {집계['점검']}건은 사용자 리스크가 아니라 **파이프라인 실패**다. "
                  f"기관 문의로 안내하지 마라 (실패단계·경로 참조)")
        재원 = {}
        for x in 행들:            # ⚠️ 형태(현금/현물)는 DROP 됐다. 재원만 남는다
            재원[x["재원"]] = 재원.get(x["재원"], 0) + float(x["금액"])
        print(f"  재원별: " + " · ".join(f"{k} {int(v):,}원" for k, v in 재원.items()))
        if 강등:
            # 🔴 등급과 별개다. '가능' 인데 강등코드가 붙었으면 그 근거를 못 믿는다는 뜻이다
            print(f"  강등코드: " + " · ".join(f"{k} {v}건" for k, v in sorted(강등.items())))

        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(
            {"모드": "live" if a.live else "dry", "출처": 출처,
             "집계": 집계, "위험금액": 위험금액, "총액": 총액,
             "강등코드": 강등, "건": 결과},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ {a.out}")

        if a.queue:
            recs = 접기([{
                "종류": "H4정산리허설", "사유코드": "REHEARSAL_RISK",
                "대상종류": "none", "대상ID": None,
                "사업명": a.사업명, "비목": x["비목"],
                "doc_id": None, "조번호": None, "구doc_id": None, "구조번호": None,
                "변경유형": None, "유사도": None,
                "요약": f"정산 리허설 '높음' — {x['비목']} {int(x['금액']):,}원 "
                        f"({x.get('귀속월') or '?'}) · {x['요약'][:80]}",
                "상세": {"집행": {k: str(v) for k, v in x.items() if k != "해야할일"},
                        "모드": "live" if a.live else "dry",
                        "주의": "dry 모드 결과라면 판정 품질이 아니다" if not a.live else None},
            } for x in 결과 if x["리스크"] == "높음"])
            if recs:
                _n, msg = 적재(conn, recs, False)
                print(f"적재: {msg}")
            else:
                print("적재: '높음' 건이 없어 큐에 넣을 것이 없다")


if __name__ == "__main__":
    main()
