# -*- coding: utf-8 -*-
"""두 run 이 «대응비교로 뺄 수 있는 짝인지» 를 run 전·후에 확인한다.

🔴 이 파일의 존재 이유는 CLAUDE.md 의 한 줄이다 —
   「조건이 다른 run 끼리 hit@k·일치율을 빼지 마라」. 이 프로젝트가 여기서 다섯 번 틀렸다.
   `eval.runs.설정` 에 조건이 «전부 박혀 있는데» 아무도 대조를 안 해서 났다.
   그래서 **읽는 규칙을 코드로 만든다.**

쓰는 법
    # run 이 끝난 뒤 두 run 을 대조
    PYTHONIOENCODING=utf-8 python scratchpad/run대조.py 195 196
    # 아직 안 돈 run 을 «돌리기 전에» 지금 트리 상태와 대조 (기준 run 하나만 준다)
    PYTHONIOENCODING=utf-8 python scratchpad/run대조.py 195

두 번째 형태가 중요하다. **run 을 2.5시간 돌린 «뒤» 에 「조건이 달랐다」를 알면 늦다.**

## 무엇을 「달라도 되는 것」으로 보나
실험 변수 하나만 달라야 한다. 그 «하나» 를 `--허용` 으로 명시한다 — 명시 안 한 게
다르면 빨간 줄이 뜬다. 🔴 기본 허용은 «없음» 이다. 「rules 는 당연히 다르지」를
코드가 알아서 봐주면 그 순간 이 도구가 무력해진다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from _lib import db  # noqa: E402

# 대조하는 키. 🔴 「판정 결과를 바꿀 수 있는가」로 골랐다 — 라벨·시각처럼 결과와
#    무관한 것은 안 본다. 그리고 `dirty` 는 scripts/ 만 본다는 한계가 있어
#    `코퍼스버전`·`rules수`·`적용대상분포` 를 «같이» 봐야 한다 (2026-09-05 실측).
_대조키 = ["변형", "b0_sha1", "폐포사용", "top_k", "동시", "max_model_len",
         "코퍼스버전", "rules수", "적용대상분포", "문항수", "부분집합",
         "SUDDOE_플래그", "정답고정", "꽂기", "원본포획"]


def _설정(run_id: int) -> tuple[str, dict]:
    with db.connect() as c:
        r = c.execute("SELECT 라벨, 설정 FROM eval.runs WHERE run_id=%s", (run_id,)).fetchone()
    if not r:
        sys.exit(f"run {run_id} 가 없다")
    return r[0], (r[1] or {})


def _지금() -> dict:
    """아직 안 돈 run 을 대조하기 위해 «지금 트리·DB» 에서 같은 모양을 만든다.

    🔴 `eval_store.코퍼스버전()` 과 `eval_e2e._b0해시()` 는 **그대로 불러 쓴다.**
       여기서 다시 구현하면 두 곳이 갈려서, 대조기가 「같다」고 해도 실제 run 은 다른
       값을 박을 수 있다.
    ⚠️ `rules수`·`적용대상분포` 는 `eval_e2e` 안에 «함수가 없고» 본문에 인라인으로
       박혀 있다(`eval_e2e.py:491~494`). 그래서 이 두 개만 SQL 을 베꼈다 —
       🔴 저쪽이 바뀌면 여기도 같이 바꿔야 한다. 베낀 자리는 이 셋이 전부다.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    import eval_e2e  # noqa: PLC0415
    import eval_store  # noqa: PLC0415
    out: dict = {}
    with db.connect() as c:
        cur = c.cursor()
        out["코퍼스버전"] = eval_store.코퍼스버전(cur)
        cur.execute("SELECT count(*), count(*) FILTER (WHERE verified) FROM corpus.rules")
        out["rules수"] = dict(zip(("총", "verified"), cur.fetchone()))
        cur.execute("SELECT 적용대상, count(*) FROM corpus.chunks GROUP BY 1 ORDER BY 2 DESC")
        out["적용대상분포"] = {r[0]: r[1] for r in cur.fetchall()}
    out["b0_sha1"] = eval_e2e._b0해시("V0")
    out["SUDDOE_플래그"] = {k: v for k, v in sorted(os.environ.items()) if k.startswith("SUDDOE_")}
    out["_git"] = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace").stdout.strip()[:12]
    return out


def main() -> int:
    허용 = []
    argv = sys.argv[1:]
    if "--허용" in argv:
        i = argv.index("--허용")
        허용 = [x for x in argv[i + 1].split(",") if x]
        argv = argv[:i] + argv[i + 2:]
    a = int(argv[0])
    라벨A, A = _설정(a)
    if len(argv) > 1:
        b = int(argv[1]); 라벨B, B = _설정(b); 이름B = f"run {b}"
    else:
        b, 라벨B, B, 이름B = None, "(지금 트리·DB)", _지금(), "지금"
        print("🔴 «돌리기 전» 대조다 — 아직 안 박힌 값(동시·문항수 등)은 «미상» 으로 나온다.\n"
              "   미상은 「같다」가 아니다. run 명령의 인자로 네가 맞춰야 한다.\n")

    print(f"run {a}  {라벨A}")
    print(f"{이름B:<7}  {라벨B}")
    print(f"허용된 차이: {허용 or '없음 — 하나라도 다르면 짝이 아니다'}")
    print("=" * 74)
    다름, 미상, 못쟀다 = [], [], []
    for k in _대조키:
        va, vb = A.get(k, "(없음)"), B.get(k, "(미상)")
        if vb == "(미상)" and b is None:
            미상.append(k); continue
        # 🔴 «키가 아예 없는 것» 과 «값이 다른 것» 을 가른다. 없음은 「다르다」가 아니라
        #    **「그 run 때는 이 필드를 안 남겼다 = 못 쟀다」** 다. run 194 의 b0_sha1 이
        #    그렇다(그 필드가 나중에 생겼다). 둘을 같은 칸에 넣으면 「B0 가 바뀌었다」로
        #    읽혀 멀쩡한 짝을 버리게 된다 — 오늘 하루 종일 잡은 그 반이다.
        if va == "(없음)" or vb == "(없음)":
            못쟀다.append(k)
            print(f"⚠️ 못 쟀다  {k}  —  run {a}: {va if va=='(없음)' else '있음'} · "
                  f"{이름B}: {vb if vb=='(없음)' else '있음'}")
            print("      「같다」도 「다르다」도 아니다. 이 축은 이 두 run 사이에서 «확인 불가» 다")
            continue
        if json.dumps(va, ensure_ascii=False, sort_keys=True) == \
           json.dumps(vb, ensure_ascii=False, sort_keys=True):
            continue
        다름.append(k)
        표 = "✅ 허용된 변수" if k in 허용 else "🔴 예상 밖"
        print(f"{표}  {k}")
        print(f"      run {a}: {json.dumps(va, ensure_ascii=False)[:120]}")
        print(f"      {이름B}: {json.dumps(vb, ensure_ascii=False)[:120]}")
    gitA = (A.get("git") or {}).get("commit", "")[:12]
    gitB = (B.get("git") or {}).get("commit", "")[:12] if b else B.get("_git", "")
    if gitA != gitB:
        print(f"⚠️ git  run {a}: {gitA}  ·  {이름B}: {gitB}")
        print(f"      커밋이 다르면 «무엇이» 달라졌는지 봐라:  git diff {gitA}..{gitB} -- scripts/")
    dirtyA = (A.get("git") or {}).get("dirty")
    if dirtyA:
        print(f"⚠️ run {a} 이 더러운 트리에서 돌았다: {dirtyA}")
    if 미상:
        print(f"\n미상(아직 안 박힘 — run 명령에서 맞춰야 한다): {', '.join(미상)}")
    예상밖 = [k for k in 다름 if k not in 허용]
    print("\n" + "=" * 74)
    if 못쟀다:
        print(f"⚠️ 확인 불가 {len(못쟀다)}건: {', '.join(못쟀다)}")
        print("   한쪽 run 이 그 필드를 안 남겼다. **「같다」로 세지 마라** — 그 축은 못 잰 것이다.")
    if 예상밖:
        print(f"🔴 예상 밖 차이 {len(예상밖)}건: {', '.join(예상밖)}")
        print("   이 두 run 의 수치를 «빼지 마라». 변수가 둘 이상이라 무엇이 움직였는지 못 가른다.")
        return 1
    print(f"✅ 예상 밖 차이 없음. 허용 변수({', '.join(허용) or '없음'})만 다르다 — 대응비교가 선다.")
    print("   🔴 그래도 «같은 gold_id 집합» 인지는 따로 봐라 — 문항수가 같아도 구성이 다를 수 있다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
