# -*- coding: utf-8 -*-
"""판정 파이프라인 CLI: 질문 하나 판정, 골든셋 일괄 실행, 장애 주입 점검, eval.runs 기록.

orchestrate.판정 을 부르는 평가·운영 도구다. `python scripts/orchestrate.py ...` 도 여기로 위임한다.
읽는 테이블: eval.golden_set. 쓰는 테이블: eval.runs·eval.run_items(--eval-log), tenant.decisions(--no-log 없이 실행 시).
장애 주입 점검은 모든 주입이 판단불가로 끝나야 통과다(실패 시 종료 코드 1). 채점은 결정론적이며 LLM 채점은 없다.
실행: PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --golden --dry --no-log --limit 5
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
from _lib import db, paths  # noqa: E402

paths.ensure_on_path()
import orchestrate  # noqa: E402
import retrieve  # noqa: E402
from assemble_context import 프롬프트_버전  # noqa: E402
from eval_store import 평가용_사업명  # noqa: E402

_log = logging.getLogger(__name__)

_주입종류 = ("db", "timeout", "schema", "empty", "cite")
_장애주입_질문 = "노트북 200만원 구매해도 되나요"


def 워밍업() -> None:
    """임베딩 모델·형태소 분석기를 미리 로드해 첫 문항의 지연을 측정에서 뺀다. 실패해도 계속한다."""
    try:
        retrieve.워밍업()
    except Exception:                                    # noqa: BLE001
        _log.exception("검색 워밍업 실패(계속 진행)")


def _장애주입_실행(종류: list[str], 질문: str | None, 사업명: str | None,
              참조확장_사용: bool) -> int:
    """장애 주입별로 판정을 dry 로 실행하고 판단불가로 끝나지 않은 개수를 돌려준다.

    LLM 서버 없이도 각 주입이 자기 단계에서 걸려야 한다. 모두 정규화에서 실패하면 아무것도 검증하지 못한다.
    """
    실패수 = 0
    for f in 종류:
        r = orchestrate.판정(질문 or _장애주입_질문, 사업명=사업명, dry=True, 기록=False,
                           주입=f, 참조확장_사용=참조확장_사용)
        ok = r["판정"] == "판단불가"
        if f in ("schema", "cite"):
            # 검증 단계에서 판정이 하향되면 LLM 이 쓴 요약("주입")이 아니라 고정 문구(_하향_요약)가 나가야 한다
            ok = ok and r.get("요약") not in ("", "주입")
        실패수 += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} 주입={f:8} 판정={r['판정']:6} "
              f"실패단계={r.get('실패단계')} 게이트={r.get('게이트')} "
              f"코드={r.get('검증코드')} · {str(r.get('검증사유'))[:110]}")
    print("\n" + ("PASS: 모든 장애 주입이 판단불가로 끝났다" if not 실패수
                 else f"FAIL: {실패수}건이 판단불가가 아니다"))
    return 실패수


def _golden_실행(a: argparse.Namespace, 참조확장_사용: bool) -> list[dict]:
    워밍업()
    with db.connect(autocommit=True) as conn:
        # 공통 문항은 사업명 IS NULL 이고 적용범위 에 원래 표기가 있다
        컬럼 = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_schema='eval' AND table_name='golden_set'").fetchall()}
        적용 = "적용범위" if "적용범위" in 컬럼 else "NULL::text"
        rows = conn.execute(f"SELECT gold_id, 세트, 질문, 사업명, 정답판정, {적용} "
                            "FROM eval.golden_set ORDER BY gold_id").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    out = []
    t0 = time.time()
    for gid, 세트, q, 사업, 정답, 적용범위 in rows:
        r = orchestrate.판정(q, 사업명=평가용_사업명(사업, 적용범위), dry=a.dry, 기록=not a.no_log,
                           참조확장_사용=참조확장_사용)
        out.append({"gold_id": gid, "세트": 세트, "정답": 정답, **r})
        print(f"{gid:3} [{세트:4}] 게이트={r.get('게이트')} 처리단계={r.get('처리단계')} "
              f"문장맵={r.get('문장맵_크기', len(r.get('문장맵') or {}))} "
              f"프롬프트={r.get('프롬프트길이', 0):,}자 "
              f"{r.get('지연ms', {}).get('총', 0):,}ms "
              + (f"실패단계={r.get('실패단계')}" if r.get("실패단계") else ""))
    print(f"\n{len(out)}건 · {time.time()-t0:.0f}초 · 프롬프트버전={프롬프트_버전}")
    return out


def _eval_기록(out: list[dict], a: argparse.Namespace, 참조확장_사용: bool) -> int:
    """골든 실행 결과를 eval.runs/run_items 에 기록하고 run_id 를 돌려준다.

    설정에 사업필터·프롬프트 버전 등 실행 조건을 함께 남긴다. 조건 없이 남은 지표는 비교할 수 없다.
    """
    from eval_store import 기록
    n = len(out) or 1
    일치 = sum(1 for r in out if r.get("판정") == r.get("정답"))
    # 가능오판: 정답이 '가능' 이 아닌데 '가능' 이라 답한 것 전부(정답 판단불가 포함)
    가능오판 = sum(1 for r in out if r.get("정답") != "가능" and r.get("판정") == "가능")
    가능오판_불가조건부 = sum(1 for r in out if r.get("정답") in ("불가", "조건부")
                       and r.get("판정") == "가능")
    판단불가수 = sum(1 for r in out if r.get("판정") == "판단불가")
    return 기록({"종류": "e2e",
                 "설정": {"프롬프트버전": 프롬프트_버전, "사업필터": orchestrate.사업필터,
                        "검색최소유사도": orchestrate.검색_최소유사도, "dry": a.dry,
                        "top_k": 5, "온도": orchestrate.판정_온도,
                        "참조확장사용": 참조확장_사용},
                 "문항수": len(out),
                 "지표": {"일치율": 일치 / n * 100, "가능오판율": 가능오판 / n * 100,
                        "판단불가율": 판단불가수 / n * 100,
                        "일치": 일치, "가능오판": 가능오판, "판단불가": 판단불가수,
                        "가능오판_불가조건부": 가능오판_불가조건부},
                 "라벨": f"e2e-{프롬프트_버전}"},
                [{"gold_id": r["gold_id"], "예측": r.get("판정"),
                  "정답": r.get("정답"),
                  "적중": r.get("판정") == r.get("정답"),
                  "원출력": {k: r.get(k) for k in
                           ("판정", "인용목록", "전제목록", "검증코드",
                            "검증사유", "게이트", "처리단계", "지연ms", "문장맵")}}
                 for r in out])


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="판정 파이프라인 실행·평가")
    ap.add_argument("--q", help="질문 한 줄")
    ap.add_argument("--사업명")
    ap.add_argument("--org-id")
    ap.add_argument("--account-id", help="계정 단위 데이터(프로필 B5·decisions)를 읽고 쓸 계정. 없으면 계정 없는 행")
    ap.add_argument("--dry", action="store_true", help="LLM 없이 프롬프트 구성까지만")
    ap.add_argument("--golden", action="store_true", help="골든셋 전체 실행")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--fault", help="|".join(_주입종류) + "|all")
    ap.add_argument("--no-log", action="store_true", help="tenant.decisions 기록 생략")
    ap.add_argument("--out", help="골든 결과 JSONL 경로")
    ap.add_argument("--eval-log", action="store_true", dest="eval_log",
                    help="골든 결과를 eval.runs 에 기록")
    ap.add_argument("--참조확장", dest="참조확장", choices=["on", "off"], default="on",
                    help="B3(참조 확장 조문)을 프롬프트에 넣을지. 기본 on")
    a = ap.parse_args()
    참조확장_사용 = a.참조확장 != "off"

    if a.fault:
        종류 = list(_주입종류) if a.fault == "all" else [a.fault]
        sys.exit(1 if _장애주입_실행(종류, a.q, a.사업명, 참조확장_사용) else 0)

    if a.golden:
        out = _golden_실행(a, 참조확장_사용)
        if a.eval_log:
            try:
                print(f"eval.runs run_id={_eval_기록(out, a, 참조확장_사용)}")
            except Exception as e:                       # noqa: BLE001
                print(f"경고: eval.runs 기록 실패(결과 파일은 남는다): {type(e).__name__}: {e}")
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            print(f"-> {a.out}")
        return

    if not a.q:
        ap.error("--q · --golden · --fault 중 하나")
    r = orchestrate.판정(a.q, 사업명=a.사업명, org_id=a.org_id, account_id=a.account_id,
                       dry=a.dry, 기록=not a.no_log,
                       참조확장_사용=참조확장_사용)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
