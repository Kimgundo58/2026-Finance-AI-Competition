# -*- coding: utf-8 -*-
"""대조 로직 자체를 검산한다 — 어제 캡처를 넣어 어제 결론이 나오는가.

왜 필요한가 (중앙 지시 2026-09-03):
    팟을 켠 뒤에 스크립트 버그를 찾으면 그게 제일 비싸다. 어제 값은 이미 알고 있으니
    **아는 답이 나오는지** 로 자를 먼저 검정한다.

기대값 — 어제 첫 실판정에서 관측된 것:
    DB `tenant.decisions` 인용  4275=3 · 4276=1 · 4277=4
    SSE `인용` 이벤트          전부 **0건**  ← 이것이 B3 결함
    DB 해야할일               4275=10 · 4276=1 · 4277=4

🔴 4275(원문주입 벌)의 SSE 원본은 남아 있지 않다 (git·디스크 모두 없음).
   그 벌은 DB 쪽만 검정한다 — 없는 것을 있는 척 세지 않는다.

쓰는 법:  python scratchpad/wgpu_대조검산.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wgpu_e2e import _db, _인용키, _조번호들                          # noqa: E402  같은 자를 쓴다

OUT = Path(__file__).resolve().parent

# 파일 ↔ decisions 행 짝짓기. 질문원문으로 확인한 것이다 (추측 아님):
#   4275 "디자이너가 쓸 맥북 프로 250만원…"  = 원문주입 벌 (SSE 원본 없음)
#   4276 "맥북 프로 디자이너가 쓸 2500000원" = 되짚은 문장 → `_sse_judge_자연어.txt`
#   4277 "맥북 프로 디자이너 작업용 2500000.0원" = 폼 → `_sse_judge_폼.txt`
짝 = [
    ("자연어(되짚기)", "_sse_judge_자연어.txt", 4276, {"DB인용": 1, "DB할일": 1, "SSE인용": 0}),
    ("폼", "_sse_judge_폼.txt", 4277, {"DB인용": 4, "DB할일": 4, "SSE인용": 0}),
    ("원문주입", None, 4275, {"DB인용": 3, "DB할일": 10, "SSE인용": None}),
]


def 이벤트파싱(경로: Path) -> list[tuple[str, object]]:
    """`sse()` 가 하던 것과 같은 규칙으로 저장본을 읽는다."""
    이벤트, 현재 = [], None
    for 줄 in 경로.read_text(encoding="utf-8").splitlines():
        if 줄.startswith("event: "):
            현재 = 줄[7:].strip()
        elif 줄.startswith("data: "):
            d = 줄[6:]
            try:
                d = json.loads(d)
            except Exception:
                pass
            이벤트.append((현재 or "?", d))
    return 이벤트


def 행읽기(did: int) -> dict:
    r = _db("SELECT decision_id, 판정, 인용, 해야할일, 질문원문 "
            "FROM tenant.decisions WHERE decision_id = %s", (did,))
    if not r:
        return {}
    행 = r[0]
    인용 = 행["인용"] if isinstance(행["인용"], list) else json.loads(행["인용"] or "[]")
    할일 = 행["해야할일"] if isinstance(행["해야할일"], list) else json.loads(행["해야할일"] or "[]")
    return {"판정": 행["판정"], "질문원문": 행["질문원문"], "인용": 인용, "할일": 할일}


def main() -> int:
    표, 실패 = [], 0
    for 이름, 파일, did, 기대 in 짝:
        행 = 행읽기(did)
        줄 = {"벌": 이름, "decision_id": did, "질문원문": (행.get("질문원문") or "")[:34],
             "판정": 행.get("판정"),
             "DB_인용": len(행.get("인용") or []), "DB_할일": len(행.get("할일") or []),
             # 🔴 `_할일_중복제거` 는 **API 계층에만** 있다 (main.py:1010). DB 에는
             #    오케 원본이 그대로 들어간다 — 그래서 DB 할일 > SSE 할일 이 정상이다.
             #    이 둘을 안 갈라 세면 「중복 제거가 안 먹었다」로 잘못 읽는다.
             "DB_할일_중복": (lambda h: len(h) - len({(x.get("code"), x.get("항목"),
                                                   x.get("설명")) for x in h
                                                  if isinstance(x, dict)}))(행.get("할일") or []),
             "DB_조번호": _조번호들(행.get("인용")), "DB_인용키": _인용키(행.get("인용")),
             "SSE_인용": None, "SSE_조번호": None, "SSE_할일_총": None, "SSE_할일_중복": None}

        if 파일 and (OUT / 파일).exists():
            ev = 이벤트파싱(OUT / 파일)
            인용ev = next((d for n, d in ev if n == "인용"), None)
            인용ev = 인용ev if isinstance(인용ev, list) else []
            할일ev = next((d for n, d in ev if n == "해야할일"), None)
            할일ev = 할일ev if isinstance(할일ev, list) else []
            키 = [(h.get("code"), h.get("항목"), h.get("설명"))
                 for h in 할일ev if isinstance(h, dict)]
            줄["SSE_인용"] = len(인용ev)
            줄["SSE_조번호"] = _조번호들(인용ev)
            줄["SSE_인용키"] = _인용키(인용ev)
            줄["SSE_할일_총"] = len(할일ev)
            줄["SSE_할일_중복"] = len(키) - len(set(키))
        elif 파일:
            줄["비고"] = f"{파일} 없음"
        else:
            줄["비고"] = "SSE 원본 미보존 — DB 만 검정"

        # ── 아는 답과 맞는가 ────────────────────────────────────────
        검정 = []
        검정.append(("DB 인용", 줄["DB_인용"], 기대["DB인용"]))
        검정.append(("DB 할일", 줄["DB_할일"], 기대["DB할일"]))
        if 기대["SSE인용"] is not None:
            검정.append(("SSE 인용", 줄["SSE_인용"], 기대["SSE인용"]))
        for 무엇, 실제, 기댓 in 검정:
            표시 = "OK " if 실제 == 기댓 else "🔴 "
            if 실제 != 기댓:
                실패 += 1
            print(f"  {표시}{이름:14s} {무엇:8s} 실제={실제} 기대={기댓}", flush=True)
        표.append(줄)

    (OUT / "_대조검산.json").write_text(
        json.dumps({"표": 표, "불일치": 실패}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n불일치 {실패}건 · 표 scratchpad/_대조검산.json", flush=True)
    if 실패 == 0:
        print("자는 맞다 — 어제 값을 넣으면 어제 결론이 그대로 나온다.", flush=True)
    return 1 if 실패 else 0


if __name__ == "__main__":
    sys.exit(main())
