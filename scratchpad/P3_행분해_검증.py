# -*- coding: utf-8 -*-
"""P3 — 표 행분해(SUDDOE_ROWSPLIT) 합격 기준 4개를 93문항에서 잰다.

읽기 전용이다. `orchestrate.판정(dry=True, 기록=False)` 만 부르고 DB 에 한 행도 쓰지 않는다.

## 한 문항에서 프롬프트를 세 벌 만든다 (검색은 한 번만 돈다 — 비싼 건 검색이다)
    기준  `git show HEAD:scripts/assemble_context.py` 를 그대로 불러온 것.
          🔴 **내 수정 전 코드가 곧 기준선이다.** 자수 비교가 아니라 문자열 동일성을 본다
    off   현재 코드 · 플래그 없음   → 합격 기준 ① (기준과 바이트 단위 동일)
    on    현재 코드 · 플래그 1      → 합격 기준 ②③

## 합격 기준 (ai-e8, 2026-09-03)
    ① off 에서 블록해시 93문항 전부 기존과 동일
    ② on 에서 S번호 한 개가 맡는 최대 자수 <= 3,000
    ③ S번호 총수 == len(s맵)
    ④ dry 93문항 완주, 예외 0

## 같이 남기는 것 (P1 ai-2c 요청 · 2026-09-03)
    전/후 프롬프트 자수 — 행 하나당 표시머리가 다시 붙어 58자씩 는다. 컨텍스트 여유가
    이 값으로만 잡힌다. 토큰 환산 계수는 아직 실측이 없다(0.7 은 추정치다).
    전/후 인용 후보 doc 집합 — 늘면 `VLM_DOWNGRADE` 가 새로 울릴 수 있다.

실행:
    PYTHONIOENCODING=utf-8 python scratchpad/P3_행분해_검증.py --out scratchpad/P3_행분해_검증.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time

_여기 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_여기), "scripts"))

from _lib import db          # noqa: E402
import eval_store            # noqa: E402
import assemble_context as A  # noqa: E402
import orchestrate           # noqa: E402

RE_S = re.compile(r"^\[(S\d+)\] ", re.M)
플래그 = "SUDDOE_ROWSPLIT"


def 기준모듈():
    """HEAD 커밋의 assemble_context 를 별도 모듈로 올린다."""
    p = os.path.join(_여기, "_P3_기준_assemble.py")
    if not os.path.exists(p):
        sys.exit(f"기준 파일이 없다 — git show HEAD:scripts/assemble_context.py > {p}")
    spec = importlib.util.spec_from_file_location("_P3_기준_assemble", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def S마디(프롬프트: str) -> list[tuple[str, int, int]]:
    """[(S번호, 마디자수, 원문자수)] — 마디는 표시머리 줄을 포함하고 원문은 뺀 것."""
    자리 = [(m.start(), m.group(1)) for m in RE_S.finditer(프롬프트)]
    out = []
    for k, (st, s) in enumerate(자리):
        end = 자리[k + 1][0] if k + 1 < len(자리) else len(프롬프트)
        마디 = 프롬프트[st:end]
        머리끝 = 마디.find("\n")
        out.append((s, len(마디), len(마디) - (머리끝 + 1 if 머리끝 >= 0 else 0)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(_여기, "P3_행분해_검증.json"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--top-k", type=int, default=5)
    a = ap.parse_args()

    기준 = 기준모듈()
    원조립 = A.조립
    포획: dict = {}

    def spy(cur, 질문, 정규화, **kw):
        os.environ.pop(플래그, None)
        p기준, s기준, _ = 기준.조립(cur, 질문, 정규화, **kw)
        p오프, s오프, 사슬 = 원조립(cur, 질문, 정규화, **kw)
        os.environ[플래그] = "1"
        try:
            p온, s온, _ = 원조립(cur, 질문, 정규화, **kw)
        finally:
            os.environ.pop(플래그, None)
        포획.clear()
        포획.update(기준=(p기준, s기준), off=(p오프, s오프), on=(p온, s온),
                    폐포=list(kw.get("폐포") or []))
        return p오프, s오프, 사슬

    orchestrate.조립 = spy

    결과, t0 = [], time.time()
    with db.connect(autocommit=True) as conn:
        cur = conn.cursor()
        코퍼스 = eval_store.코퍼스버전(cur)
        문항 = eval_store.평가대상(cur)
        if a.limit:
            문항 = 문항[:a.limit]
        print(f"코퍼스버전 {코퍼스} · 채점대상 {len(문항)}문항 · top_k={a.top_k}", flush=True)

        for i, m in enumerate(문항, 1):
            gid = m["gold_id"]
            포획.clear()
            try:
                orchestrate.판정(m["질문"], 사업명=eval_store.사업키(m["사업명"]),
                                dry=True, top_k=a.top_k, conn=conn, 기록=False)
            except Exception as e:
                결과.append({"gold_id": gid, "오류": f"{type(e).__name__}: {e}"})
                print(f"  🔴 {gid} {type(e).__name__}: {e}", flush=True)
                continue
            if not 포획:
                결과.append({"gold_id": gid, "오류": "조립 미호출(경로 조기 종료)"})
                continue

            p기준, s기준 = 포획["기준"]
            p오프, s오프 = 포획["off"]
            p온, s온 = 포획["on"]
            마디온 = S마디(p온)
            마디오프 = S마디(p오프)
            결과.append({
                "gold_id": gid, "세트": m["세트"], "적용범위": m["적용범위"],
                # ① — 바이트 단위 동일성
                "동일_문자열": p기준 == p오프,
                "동일_블록해시": A.블록해시(p기준) == A.블록해시(p오프),
                "동일_s맵": s기준 == s오프,
                # ②
                "최대마디_off": max([c for _, c, _ in 마디오프], default=0),
                "최대마디_on": max([c for _, c, _ in 마디온], default=0),
                "최대원문_on": max([c for _, _, c in 마디온], default=0),
                # ③
                "S마커수_on": len(마디온), "s맵크기_on": len(s온),
                "S마커수_off": len(마디오프), "s맵크기_off": len(s오프),
                # 자수 (ai-2c 요청)
                "자수_off": len(p오프), "자수_on": len(p온),
                "블록_off": A.블록자수(p오프), "블록_on": A.블록자수(p온),
                # 전/후 인용 후보 집합 — 늘면 VLM_DOWNGRADE 가 새로 울릴 수 있다(ai-2c)
                "인용후보_동일": ({(v[0], v[1]) for v in s오프.values()}
                                  == {(v[0], v[1]) for v in s온.values()}),
                "폐포수": len(포획["폐포"]),
            })
            if i % 10 == 0 or i == len(문항):
                print(f"  {i}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"코퍼스버전": 코퍼스, "top_k": a.top_k, "문항수": len(결과),
                   "항목": 결과}, f, ensure_ascii=False, indent=1)
    print(f"\n저장 {a.out}")

    산 = [x for x in 결과 if "오류" not in x]
    오류 = [x for x in 결과 if "오류" in x]
    n = len(산) or 1
    깨짐 = [x["gold_id"] for x in 산 if not (x["동일_문자열"] and x["동일_s맵"])]
    초과 = [(x["gold_id"], x["최대원문_on"]) for x in 산 if x["최대원문_on"] > 3000]
    어긋 = [x["gold_id"] for x in 산 if x["S마커수_on"] != x["s맵크기_on"]]
    증가 = [x["자수_on"] - x["자수_off"] for x in 산]
    S증가 = [x["s맵크기_on"] - x["s맵크기_off"] for x in 산]

    print(f"\n① off 바이트 동일       {len(산)-len(깨짐)}/{len(산)}"
          + (f"  🔴 깨진 문항 {깨짐[:8]}" if 깨짐 else "  통과"))
    print(f"② on 최대 원문자수      최대 {max([x['최대원문_on'] for x in 산], default=0):,}"
          f" (off {max([x['최대마디_off'] for x in 산], default=0):,})"
          + (f"  🔴 3000 초과 {초과[:8]}" if 초과 else "  통과"))
    print(f"③ S마커수 == len(s맵)   {len(산)-len(어긋)}/{len(산)}"
          + (f"  🔴 {어긋[:8]}" if 어긋 else "  통과"))
    print(f"④ dry 완주              {len(산)}/{len(결과)}"
          + (f"  🔴 오류 {[x['gold_id'] for x in 오류][:8]}" if 오류 else "  예외 0"))
    print(f"\n자수 증가   평균 {sum(증가)//n:+,} · 최대 {max(증가, default=0):+,} "
          f"· 중앙 {sorted(증가)[len(산)//2]:+,}")
    벌어짐 = [x["gold_id"] for x in 산 if not x["인용후보_동일"]]
    print(f"인용후보 집합 동일  {len(산)-len(벌어짐)}/{len(산)}"
          + (f"  🔴 {벌어짐[:8]}" if 벌어짐 else "  (쪼갤 뿐 새 문서가 안 든다)"))
    print(f"S번호 증가  평균 {sum(S증가)/n:+.1f} · 최대 {max(S증가, default=0):+} "
          f"· 최대 s맵 {max([x['s맵크기_on'] for x in 산], default=0)}")


if __name__ == "__main__":
    main()
