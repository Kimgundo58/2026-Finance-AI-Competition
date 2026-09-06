# -*- coding: utf-8 -*-
"""D5 — 정답셋 실전 E2E. `orchestrate.판정()` 을 끝까지 돌리고 `eval.runs` 에 남긴다.

**오늘 이게 내는 유일하게 중요한 숫자.**

    판정 단계 격리 (근거를 정답으로 주면)   일치율 84.7% · 치명 오답 0
    검색이 근거를 찾는 비율               hit@5 52.9%
    ─────────────────────────────────────────────────────────────
    실전 E2E                              ← 여기. 둘 사이의 낙폭이 내일 무엇을 고칠지 정한다

**채점은 전부 결정론적이다.** 계약 §10 이 LLM-as-judge(RAGAS 등)를 금지한다 —
심판이 LLM 이면 같은 산출물에 다른 점수가 나온다.
  · 판정 일치   4-way (가능/조건부/불가/판단불가) 문자열 일치
  · 치명 오답   정답이 불가·조건부인데 예측이 '가능'  🔴 1건이라도 나오면 머지 금지
  · 인용 정확   예측 인용의 doc_id·조번호가 `eval.golden_chunks` 고정분과 겹치는가
  · 근거 적중   top5 ∩ 고정 정답청크 (검색이 근거를 물어왔는가)

**필수 분해 출력** (계약 §8-D5, 2026-09-08 갱신): 공통 / 사업지정 / 세트:L3.
🔴 옛 "L3경로" 차원(`묶음(lambda it: "L3" in it["원출력"]["경로"])`)은 지웠다 — `경로`
   필드는 "1정규화+3검색+4조립+6검증" 같은 파이프라인 단계 문자열이라 "L3" 부분문자열이
   «절대 안 나온다». 그래서 이 차원은 항상 0이었다(ai-33 실측, 2026-09-07 — run202를
   0/27로 오독해 「L3 안 실렸다」로 잘못 판단했었다). L3 문항의 진짜 판정력은
   `세트:L3`(golden_set.세트 기준, 이미 있던 일반 분해)가 처음부터 맞게 재고 있었다.
🔴 D2 이후 공통 문항은 `사업명 IS NULL AND 적용범위 IS NOT NULL` 이다.
   `사업명='공통(지침 제14차)'` 로 가르던 옛 코드는 여기서 0건이 된다.

**판단불가율을 같이 낸다.** 계약 §7: E2E 판단불가율이 0% 여도 실패다 —
격리에서 0% 였던 건 근거를 정답으로 줬기 때문이고, hit@5 52.9% 인 실전에서 0% 면
근거 없이 답을 만들고 있다는 뜻이다.

실행:
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --dry        # LLM 없이 배관만 (GPU 전)
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py              # 실전. GPU 창에서
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --limit 5 --dry
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --세트 본세트
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --폐포 off    # A1 — B3 없이 재기
    PYTHONIOENCODING=utf-8 python scripts/eval_e2e.py --동시 3      # A2 — 문항 3개씩 동시
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

import argparse
import hashlib
import json
import os
import re
import sys
import time
import traceback

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib import db  # noqa: E402
import eval_store  # noqa: E402

DSN = db.DSN

판정4 = ("가능", "조건부", "불가", "판단불가")

# ════════════════════════════════════════════════════════════════════════════
# 원본 포획 (env `SUDDOE_EVAL_RAW=1` · 기본 off)
# ════════════════════════════════════════════════════════════════════════════
# 🔴 **이건 임시방편이다. 언젠가 orchestrate 에 관측 훅을 다는 게 정답이다.**
#
# run 191(93문항)이 리플레이 불가였던 이유는 eval 의 결손이 아니라 **`orchestrate.판정()`
# 의 반환 계약 결손**이다:
#   · `프롬프트길이` 는 dry 분기(orchestrate.py:625)에만 있고 실전 update(:688~697)에 없다
#     → DB 실측 run 191: 프롬프트길이 non-null 0/93
#   · 검증 «전» LLM 원 JSON(`출력`) · `룰들` · 폐기된 인용·전제는 함수 밖으로 아예 안 나온다
#     (`llm_validate.검증` 은 `(d, 사유)` 만 돌려준다 — 폐기분은 사유 «문자열» 로만 남는다)
#   · `dangling` 은 응답 안에 있지만 eval 이 안 떴다 → run 191 원출력에 `검색` 키 0/93
#
# orchestrate.py 는 백엔드 레인이 도는 본 트리와 같은 파일이라 이 레인이 못 고친다.
# 그래서 **모듈 전역을 감싸 반환 계약을 우회한다.** 소유 밖 파일은 0줄 고친다.
# 성립 근거: orchestrate.py:49~52 가 전부 `from X import Y` 꼴이라 `조립`·`정규화`·
# `검증`·`llm_호출` 이 orchestrate 의 모듈 전역이고 호출도 수식 없이 부른다.
#
# 🔴 **조용한 결손 금지.** 감싸기가 안 먹었으면 경고가 아니라 예외로 죽는다 —
#    빈 칸이 「LLM 이 그걸 안 냈다」로 읽히면 다음 사람이 또 벽을 만난다.
# 🔴 2판 「꽂기」 — P1(ai-2c) 사양 2026-09-03.
#    묻는 것은 «정답 조가 top-5 에 있었으면 맞혔겠는가» 다.
#    기존 `--isolated`(orchestrate 의 `격리근거`)는 이 물음에 답하지 못한다:
#    그쪽은 B2·B3 를 통째로 갈아치워 **방해물 40여 개까지 같이 지운다**
#    (격리 프롬프트가 실전의 1/7 — 자수 2,115 vs 14,258 · S번호 9 vs 42, P1 실측).
#    그래서 좋게 나와도 「판정력」인지 「방해물 제거」인지 원리적으로 안 갈린다.
#    꽂기는 **실전 검색 top-5 를 그대로 두고 정답 청크만 앞에 끼운다** — 방해물이 남아
#    조건이 실전과 같다. `격리근거` 인자를 안 쓰므로 L3·게이트 경로도 실전 그대로다.
#    🔴 orchestrate 를 안 고친다. 이미 걸어 둔 `조립` 래퍼의 `검색` 인자에 끼운다.
#    🔴 리스트를 «다시 묶지 않고» 내용만 갈아 끼운다(`[:] =`). 재바인딩하면 래퍼가
#       잡아 둔 옛 리스트를 계속 보게 되어 앞 문항 정답이 뒤 문항에 샌다.
INJECT = os.environ.get("SUDDOE_EVAL_INJECT_GOLD") == "1"
_꽂을청크: list[int] = []

RAW = os.environ.get("SUDDOE_EVAL_RAW") == "1"
RAW_PROMPT = os.environ.get("SUDDOE_EVAL_RAW_PROMPT") == "1"

_포획: dict = {}          # 문항 1건 분량. 매 문항 시작에 비운다
_계수: Counter = Counter()  # run 전체 누계 — 정체성 가드 ③ 의 재료

# 조립 결과는 "\n\n".join(블록) 이고 블록 머리는 `## B<n>. …` 이다
# (assemble_context.원문블록:164 · :183). 첫 머리 앞은 B0(고정 시스템 지시).


class 포획실패(RuntimeError):
    """감싸기가 안 먹었거나 지나간 자취가 안 맞는다. run 을 저장하지 않고 죽는다."""


# S번호 형식은 `assemble_context._s`: f"S{i:02d}". 자리수가 3 이상으로 넘어가도
# (S100) 앞 두 자리만 잘라 세지 않게, 앞뒤에 영숫자가 안 붙는 것만 센다.
_RE_S = re.compile(r"(?<![A-Za-z0-9])S\d{2,}(?![0-9])")


_RE_강등코드 = re.compile(r'"([A-Z][A-Z0-9_]{4,})"')
_강등코드_제외 = re.compile(r"^(SUDDOE_|VLLM_|RUNPOD_|HF_|PYTHON)")
_RE_강등코드_SQL = re.compile(r"'([A-Z][A-Z0-9_]{4,})'")


# 묶음 정의는 한 곳에서만 적는다 — `설정.묶음` 과 위 집계가 갈리면 표와 run 이 안 맞는다
_묶음정의 = {"튜닝52": ["보강", "적대적"], "held-out41": ["본세트", "공식"]}


def _주입됨(it: dict) -> bool:
    """이 문항에 정답 청크가 «실제로» 새로 들어갔나.

    🔴 「꽂으려 했다」가 아니라 「프롬프트가 달라졌다」를 묻는다. 정답 청크가 이미
       top-5 에 있던 문항은 꽂아도 프롬프트가 바이트 단위로 같다 — 실측 93문항 중 67건.
    """
    꽂 = ((it["원출력"].get("원본") or {}).get("조립") or {}).get("꽂기") or {}
    return bool(꽂.get("주입"))


def _강등코드_소스훑기() -> set[str]:
    """소스의 대문자 리터럴에서 코드 후보를 긁는다. **보조 닻이다.**

    🔴 이 그물은 코드가 아닌 것도 잡는다 — `SUDDOE_GATE_B` 같은 환경변수 이름이
       같은 꼴이다(P1 지적). 접두어로 거르지만 거르는 목록 자체가 손으로 적은 것이라
       또 낡는다. 그래서 **기준은 DB CHECK 로 두고 이건 대조용으로만 쓴다.**
    """
    여기 = os.path.dirname(os.path.abspath(__file__))
    본: set[str] = set()
    for f in ("llm_validate.py", "orchestrate.py"):
        try:
            with open(os.path.join(여기, f), encoding="utf-8") as fh:
                본 |= {m for m in _RE_강등코드.findall(fh.read())
                      if not _강등코드_제외.match(m)}
        except OSError:
            pass
    return 본


def _강등코드목록(cur=None) -> tuple[list[str], str, dict]:
    """강등코드 «전량». 발화한 것만 세면 「안 울린 코드」가 기록에서 사라진다.

    🔴 목록을 이 파일에 손으로 박지 않는다. P1(ai-2c)이 21종이라 했고 뽑아 보니
       **22종**이었다 — 베끼는 순간 그 오차가 run 에 굳는다.
       (P1·ai-e8 둘 다 정규식이 `[A-Z_]+` 라 `L3_ONLY_DOWNGRADE` 의 `3` 을 놓쳤다.
        같은 날 두 세션이 같은 실수를 했다. 세는 방법이 답을 만든다.)
    🔴 **기준은 `tenant.decisions` 의 CHECK 제약이다** (P1 권고). 선언적 목록이고,
       여기 없는 코드는 애초에 저장이 안 되니 그게 진짜 경계다. 소스 훑기는 대조용.
       2026-09-03 실측: CHECK 22 · 소스 22 · 차집합 «양방향 0».
    🔴 둘이 어긋나면 조용히 합치지 않는다 — 양쪽 차집합을 그대로 남기고 run 이 찍는다.
    """
    소스 = _강등코드_소스훑기()
    체크: set[str] = set()
    if cur is not None:
        try:
            cur.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
                           WHERE conrelid = 'tenant.decisions'::regclass AND contype = 'c'
                             AND conname LIKE %s""", ("%강등코드%",))
            for (d,) in cur.fetchall():
                체크 |= set(_RE_강등코드_SQL.findall(d))
        except Exception:
            체크 = set()
    기준, 출처 = (체크, "DB CHECK") if 체크 else (소스, "소스훑기(CHECK 를 못 읽었다)")
    대조 = {"CHECK수": len(체크), "소스수": len(소스),
          "CHECK에만": sorted(체크 - 소스), "소스에만": sorted(소스 - 체크)}
    return sorted(기준), 출처, 대조


def _헤드() -> dict:
    """지금 돌고 있는 코드가 «어느 커밋인가». 안 박으면 다음 사람이 못 가른다.

    🔴 2026-09-03 실측: 기준선 스냅샷을 뜨는 90초 사이에 HEAD 가 051b5fd -> 67ef75f
       -> 81bb2f4 로 세 번 움직였고, 그중 하나가 B4 문장을 바꿔 93문항 중 5건의
       프롬프트 해시가 갈렸다. 라벨에만 적으면 적는 걸 잊는다.
    🔴 `git archive` 로 뜬 격리 사본에서 돌리면 `.git` 이 없어 git 이 답을 못 준다.
       그때는 `SUDDOE_EVAL_GIT_HEAD` 로 «무엇을 archive 했는지» 직접 박아라.
       모르면 None 이다 — 지어내지 않는다.
    """
    import subprocess
    def _(*a):
        try:
            # 🔴 `encoding="utf-8"` 을 «반드시» 준다 (2026-09-05 ai-04 실측).
            #    `text=True` 만 주면 Windows 에서 locale(cp949)로 디코딩한다. 이 저장소는
            #    파일명에 한글이 흔해서 `git status --porcelain` 이 UnicodeDecodeError 로
            #    터졌고, 아래 `except Exception` 이 그걸 삼켜 **`dirty` 가 조용히 None 이
            #    됐다.** None 은 "깨끗하다" 가 아니라 "못 쟀다" 인데 읽는 쪽은 구분이 안 된다
            #    — 재현성 기록이 통째로 거짓말이 되는 자리다(CLAUDE.md 「잴 수 없는 것을
            #    값이 0 으로 읽지 마라」). run 194 때는 미추적 파일이 ASCII 뿐이라 안 터졌다.
            #    `errors="replace"` 는 그래도 못 읽는 바이트가 있을 때 예외 대신 문자를 남긴다.
            r = subprocess.run(a, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=os.path.dirname(os.path.abspath(__file__)), timeout=10)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        except Exception:
            return None
    직접 = os.environ.get("SUDDOE_EVAL_GIT_HEAD")
    return {"commit": _("git", "rev-parse", "HEAD") or 직접,
            "출처": "git" if _("git", "rev-parse", "HEAD") else ("직접지정" if 직접 else None),
            "브랜치": _("git", "rev-parse", "--abbrev-ref", "HEAD"),
            # 🔴 dirty 면 커밋 해시만으로는 재현이 안 된다. 어느 파일이 더러운지까지 남긴다
            "dirty": (_("git", "status", "--porcelain", "--", ".") or "").splitlines() or None}


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _b0해시(변형: str) -> str | None:
    """그 run 이 실제로 쓴 B0 의 바이트 해시. 계측이 본 실행을 죽이면 안 되니 감싼다."""
    try:
        import assemble_context
        return _sha(assemble_context.b0(변형))
    except Exception:
        return None


def _블록분해(프롬프트: str) -> dict:
    """B0~B6 구간별 자수·sha1.

    🔴 전체 해시 하나로는 「어느 블록이 변했나」를 못 가른다 — P3 의 행분해 전/후
       비교가 정확히 그걸 묻는다.
    🔴 **경계 정의는 여기 두지 않는다.** P3(ai-1e)이 `assemble_context` 에
       확정해 둔 것을 부른다 — 정의가 둘이면 P3 의 표와 이 run 이 안 맞는다.
    """
    from assemble_context import 블록분해, 블록자수, 블록해시
    자수, 해시, 원문 = 블록자수(프롬프트), 블록해시(프롬프트), 블록분해(프롬프트)
    # 🔴 P3 이 준 검산: 자수 합 == len(프롬프트). 안 맞으면 조립기 쪽 사고다.
    if sum(자수.values()) != len(프롬프트):
        raise 포획실패(f"블록 자수 합 {sum(자수.values())} != 프롬프트 {len(프롬프트)}")
    # 🔴 S번호는 «자수와 따로» 센다. 행분해(P3)는 같은 근거를 더 잘게 쪼개므로
    #    자수는 거의 그대로인데 S번호만 늘 수 있다 — 그 갈림이 인용 정확도를 가른다.
    #    중복 제거한 개수다(같은 S번호가 두 번 나와도 1). 형식은 assemble_context._s: S01
    return {k: {"자수": v, "sha1": 해시.get(k),
                "S번호수": len(set(_RE_S.findall(원문.get(k, ""))))}
            for k, v in 자수.items()}


def 감싸기() -> None:
    """orchestrate 모듈 전역을 기록 래퍼로 바꾼다. `SUDDOE_EVAL_RAW=1` 일 때만 부른다."""
    import assemble_context
    import llm_validate
    import normalize_run
    import orchestrate as orch

    # 🔴 2026-09-07 레인 Q(ai-33 확정) — SUDDOE_LLM=vllm|qwen 스위치를 감싸기 «전»에
    #    적용한다. 아래 가드①이 「현재 값 == 정본」을 확인하므로, 스위치를 먼저 걸어야
    #    무엇을 감쌌는지(vLLM 인지 Qwen 인지)가 `쌍`의 정본에 그대로 반영된다.
    from llm_qwen import 스위치_적용
    스위치_적용()

    # ── 가드 ① 감싸기 «전» 정체성. import 꼴이 바뀌면 여기서 깨진다 ────────
    쌍 = [(orch, "조립", assemble_context.조립),
          (orch, "정규화", normalize_run.정규화),
          (orch, "검증", llm_validate.검증),
          (orch, "llm_호출", normalize_run.llm_호출),
          (normalize_run, "llm_호출", normalize_run.llm_호출)]
    for m, 이름, 정본 in 쌍:
        if getattr(m, 이름) is not 정본:
            raise 포획실패(
                f"가드①: {m.__name__}.{이름} 이 정본과 다르다. import 꼴이 바뀌었거나 "
                f"이미 누가 감쌌다 — 이 상태로는 포획이 조용히 빈다")

    본_조립, 본_정규화, 본_검증, 본_호출 = (assemble_context.조립, normalize_run.정규화,
                                    llm_validate.검증, normalize_run.llm_호출)

    def w_조립(*a, **kw):
        꽂 = None
        if INJECT:
            원검색 = list(kw.get("검색") or [])
            추가 = [c for c in _꽂을청크 if c not in 원검색]
            # 🔴 앞에 끼운다. 뒤에 붙이면 S번호가 뒤로 밀려 「닿았지만 안 봤다」와
            #    「닿지 않았다」가 섞인다. 원래 top-5 의 «상대 순서» 는 그대로 둔다.
            kw["검색"] = (추가 + 원검색) or None
            꽂 = {"주입": 추가, "이미있던것": [c for c in _꽂을청크 if c in 원검색],
                 "원검색": 원검색, "최종검색": kw["검색"] or []}
        프롬프트, s맵, 사슬 = 본_조립(*a, **kw)
        rec = {"프롬프트길이": len(프롬프트), "sha1": _sha(프롬프트),
               "블록": _블록분해(프롬프트), "s맵크기": len(s맵),
               "참조사슬수": len(사슬 or []), "변형": kw.get("변형")}
        if 꽂 is not None:
            rec["꽂기"] = 꽂
        if RAW_PROMPT:
            rec["프롬프트"] = 프롬프트
        # 🔴 게이트 C(비목갈림)는 `판정()` 을 **재귀로 두 번** 부른다
        #    (orchestrate.py:539~547 — 비목 후보 2개를 각각 끝까지 태운다).
        #    그래서 «한 문항에 조립이 2회» 일어난다. 마지막 것만 남기면 프롬프트가
        #    하나 조용히 사라지고, 길이 분포와 max-model-len 예산이 그만큼 덜 센다.
        #    실측(dry 93): gold_id 360·441 이 여기 걸린다.
        _포획.setdefault("조립_전부", []).append(
            {"프롬프트길이": rec["프롬프트길이"], "sha1": rec["sha1"],
             **({"프롬프트": 프롬프트} if RAW_PROMPT else {})})
        _포획["조립"] = rec          # 마지막 1건 — 기존 소비자 호환
        _계수["조립"] += 1
        return 프롬프트, s맵, 사슬

    def w_정규화(*a, **kw):
        출력, 메타 = 본_정규화(*a, **kw)
        # ① 의 1겹 원본. P1: 「④만으로는 리플레이가 안 닫힌다」
        _포획["①정규화"] = {"출력": 출력, "메타": 메타}
        _계수["①정규화"] += 1
        return 출력, 메타

    def _w_호출(태그, *, 메타통째: bool):
        """🔴 **인자를 남긴다.** run 191 을 못 닫은 이유가 이것이다 — 라벨은
        max_tokens3000 인데 코드는 1500 이었고 어느 쪽이 돌았는지 확인할 수단이 0이었다.
        ①(400)과 ④(1500)은 값이 다르다. 뭉뚱그리지 않고 자리마다 따로 센다.

        `메타통째` 는 ① 자리만 True 다. ④ 의 finish_reason·usage 는 ai-e8 이
        orchestrate 쪽에 직접 넣어 `응답["모델"]` 로 항상 나온다(플래그와 무관하게).
        여기서 또 적으면 같은 값이 두 군데 남아 나중에 어느 쪽이 기준인지 갈린다.
        """
        def f(프롬프트, 스키마=None, **kw):
            _계수[태그] += 1
            출력, 메타 = 본_호출(프롬프트, 스키마, **kw)
            rec = {"프롬프트길이": len(프롬프트), "sha1": _sha(프롬프트),
                   "인자": {k: v for k, v in kw.items() if k != "스키마"},
                   "원출력": 출력}
            if 메타통째:
                rec["메타"] = 메타          # ① 의 종료이유·토큰. ④ 는 orchestrate 가 낸다
            if RAW_PROMPT:
                rec["프롬프트"] = 프롬프트
            _포획.setdefault(태그, []).append(rec)
            return 출력, 메타
        return f

    def w_검증(llm출력, s맵, **kw):
        d, 사유 = 본_검증(llm출력, s맵, **kw)
        전_인용 = [x for x in (llm출력.get("인용") or []) if isinstance(x, str)]
        후_인용 = [c.get("s번호") for c in (d.get("인용목록") or []) if isinstance(c, dict)]
        전_전제 = [(p.get("사실") or "").strip()
                 for p in (llm출력.get("전제") or []) if isinstance(p, dict)]
        후_전제 = [(p.get("사실") or "").strip()
                 for p in (d.get("전제목록") or []) if isinstance(p, dict)]
        전_할일 = [h.get("항목") for h in (llm출력.get("해야할일") or []) if isinstance(h, dict)]
        후_할일 = [h.get("항목") for h in (d.get("해야할일") or []) if isinstance(h, dict)]
        _포획["④검증전"] = llm출력
        _포획["검증"] = {
            # 🔴 폐기분은 `검증()` 안에서 사라지고 사유 문자열만 남는다. 차집합으로 되살린다
            "폐기_인용": [x for x in 전_인용 if x not in 후_인용],
            "폐기_전제": [x for x in 전_전제 if x not in 후_전제],
            "폐기_해야할일": [x for x in 전_할일 if x not in 후_할일],
            "폐기사유": 사유,
            "룰들": kw.get("룰들"),
            "dangling": kw.get("dangling"),
            "체크코드수": len(list(kw.get("체크코드") or [])),
            "프롬프트전달됨": bool(kw.get("프롬프트")),   # 층 B 가 깨어 있었나
            "f사실전달됨": kw.get("f사실") is not None,
        }
        _계수["검증"] += 1
        return d, 사유

    orch.조립 = w_조립
    orch.정규화 = w_정규화
    orch.검증 = w_검증
    # 🔴 두 이름은 같은 함수를 «가리키지만» 서로 다른 변수다. 한쪽만 갈면 다른 쪽이 샌다.
    #    ① 의 실제 호출은 normalize_run.py:280 이 **자기 모듈 전역**을 부르는 것이라
    #    `orchestrate.llm_호출` 만 감싸면 ① 이 통째로 안 잡힌다.
    orch.llm_호출 = _w_호출("④판정LLM", 메타통째=False)
    normalize_run.llm_호출 = _w_호출("①정규화LLM", 메타통째=True)

    # ── 가드 ② 감싼 «후» ────────────────────────────────────────────────
    for m, 이름, 정본 in 쌍:
        if getattr(m, 이름) is 정본:
            raise 포획실패(f"가드②: {m.__name__}.{이름} 이 안 감싸졌다")


def _가드3(items: list[dict], dry: bool) -> None:
    """가드 ③ — 「진짜로 지나갔나」. ①② 는 import 시점만 본다.

    🔴 어긋나면 run 을 **저장하지 않고** 죽는다. 반쯤 빈 run 이 남는 게 제일 나쁘다.

    🔴 ai-e8 의 초안은 「llm_호출 횟수/2」였는데 그건 성립하지 않는다.
       `정규화()` 는 **자기 모듈 전역**의 `llm_호출`(normalize_run.py:280)을 부르므로
       `orchestrate.llm_호출` 을 감싸도 ① 은 안 걸린다. 그래서 두 자리를 따로 감싸고
       (`④판정LLM` · `①정규화LLM`) 여기서 **문항 단위 불변식**으로 센다.
    """
    if not RAW:
        return
    if _계수["조립"] == 0:
        raise 포획실패("가드③: 조립 래퍼가 한 번도 안 걸렸다 — 포획이 통째로 비었다")
    나쁨 = []
    for it in items:
        원 = it["원출력"].get("원본") or {}
        경로 = it["원출력"].get("경로") or ""
        if "4조립" in 경로 and not 원.get("조립"):
            나쁨.append((it["gold_id"], "4조립 을 지났는데 조립 포획이 없다"))
        if "6검증" in 경로:
            if not 원.get("검증"):
                나쁨.append((it["gold_id"], "6검증 을 지났는데 검증 포획이 없다"))
            if not 원.get("④판정LLM"):
                나쁨.append((it["gold_id"], "6검증 을 지났는데 ④ LLM 포획이 없다"))
    # 실전에서 6검증을 지난 문항 수 == ④ 호출 누계 (재시도는 llm_호출 «안» 이라 안 샌다)
    검증문항 = sum(1 for it in items if "6검증" in (it["원출력"].get("경로") or ""))
    if not dry and _계수["④판정LLM"] != 검증문항:
        나쁨.append((None, f"가드③: ④ 호출 누계 {_계수['④판정LLM']} != 6검증 문항 {검증문항}"))
    if 나쁨:
        raise 포획실패("가드③ 위반 " + str(len(나쁨)) + "건: " + str(나쁨[:8]))


def _치명(정답: str, 예측: str) -> bool:
    """🔴 오답 비대칭. 안 되는 걸 된다고 하는 것만이 치명이다.
    반대(되는 걸 안 된다고)는 손해지 사고가 아니다."""
    return 정답 in ("불가", "조건부") and 예측 == "가능"


def _인용좌표(응답: dict) -> set[tuple]:
    """예측 인용에서 (doc_id, 조번호) 를 뽑는다. 키 이름이 갈릴 수 있어 넓게 받는다."""
    out = set()
    for c in 응답.get("인용목록") or []:
        if not isinstance(c, dict):
            continue
        doc = c.get("doc_id") or c.get("doc")
        조 = c.get("조번호") or c.get("조")
        if doc:
            out.add((doc, 조))
    return out


def _세트이름들() -> list[str]:
    """🔴 `--세트` 의 choices 를 손으로 적지 않는다 — 이 파일이 이미 한 번 그 사고를 냈다
    (주석 실측: '공식'·'보강' 이 golden_set 에 추가됐는데 목록은 2개로 굳어 있었다).
    2026-09-07 재발 — 'L3'(27건) 이 추가됐는데 여전히 4개로 굳어 있었다. `_부분집합이름들()`
    과 같은 관용구로 DB 에서 직접 읽는다. DB 를 못 열면(오프라인 개발 등) 마지막으로
    알던 값으로 폴백한다 — 조용히 빈 리스트를 주면 argparse choices=[] 가 전부를 막는다.
    """
    try:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT 세트 FROM eval.golden_set ORDER BY 1")
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return ["본세트", "적대적", "공식", "보강", "L3"]


def _부분집합표() -> tuple[str, dict]:
    경로 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scratchpad", "P4_부분집합_0903.json")
    with open(경로, encoding="utf-8") as f:
        return 경로, json.load(f)


def _부분집합이름들() -> list[str]:
    """🔴 `--부분집합` 의 choices 를 손으로 적지 않는다.

    파일에 `격리48` 을 넣었더니 argparse 가 「invalid choice」로 막았다 —
    `--세트` 주석이 경고해 둔 것과 **똑같은 사고를 이 인자에서 다시 냈다.**
    목록은 파일이 갖고 있고, 코드는 읽기만 한다.
    """
    try:
        return [k for k, v in _부분집합표()[1].items() if isinstance(v, list)]
    except Exception:
        return []


def _부분집합(이름: str | None,
           gold_ids: str | None) -> tuple[list[int] | None, str | None, str | None]:
    """🔴 부분집합은 **세트 이름이 아니라 gold_id 로 고정한다.**
    세트별로 빠지는 문항 수가 달라 이름으로 부르면 run 마다 다른 집합이 잡히고,
    튜닝분과 held-out 이 조용히 섞인다. 기준 파일은 scratchpad/P4_부분집합_0903.json.
    """
    if gold_ids:
        return [int(x) for x in re.split(r"[,\s]+", gold_ids.strip()) if x], "직접지정", None
    if not 이름:
        return None, None, None
    경로, 표 = _부분집합표()
    if 이름 not in 표:
        sys.exit(f"부분집합 '{이름}' 이 {경로} 에 없다. 있는 것: "
                 f"{[k for k in 표 if isinstance(표[k], list)]}")
    # 🔴 유보는 파일에만 두면 표에서 사라진다. `eval.runs.설정` 이 통째로 나르게 한다 —
    #    예: P2_선택12 의 「이 12건은 전부 튜닝52 다」. 이게 없으면 다음 사람이 일반화한다.
    return (list(표[이름]), f"{이름}@P4_부분집합_0903.json(기준run={표.get('기준run')})",
            표.get(f"{이름}_설명"))


def 실행(*, dry: bool, limit: int | None, 세트: list[str] | None, 라벨: str | None,
        top_k: int, 기록: bool, 변형: str = "V0", 부분집합: str | None = None,
        gold_ids: str | None = None, max_model_len: int = 40960,
        스냅샷: str | None = None, 폐포사용: bool = True, 동시: int = 1) -> int:
    ids, 부분집합표기, 부분집합유보 = _부분집합(부분집합, gold_ids)
    if 부분집합유보:
        print(f"🔴 이 부분집합의 유보 — 수를 옮길 때 같이 옮겨라:\n   {부분집합유보}\n")
    with psycopg.connect(DSN) as conn:
        cur = conn.cursor()

        # 세트가 정확히 1개면 기존 SQL 경로 그대로 탄다 (현행과 바이트 단위 동일).
        # 여러 개면 SQL 은 전체로 두고 파이썬에서 거른다 — eval_store 는 P4 소유가 아니다.
        문항 = eval_store.평가대상(cur, 세트=(세트[0] if 세트 and len(세트) == 1 else None))
        if 세트 and len(세트) > 1:
            문항 = [m for m in 문항 if m["세트"] in set(세트)]
        if ids:
            있는 = {m["gold_id"] for m in 문항}
            빠짐 = [g for g in ids if g not in 있는]
            if 빠짐:
                # 🔴 조용히 빼지 않는다. 분모가 흔들리면 튜닝/held-out 대조가 무너진다
                sys.exit(f"--부분집합/--gold-ids 의 {len(빠짐)}건이 평가대상에 없다: {빠짐[:10]}")
            문항 = [m for m in 문항 if m["gold_id"] in set(ids)]
        if limit:
            문항 = 문항[:limit]
        if not 문항:
            sys.exit("평가 대상이 0건이다. scripts/archive/eval/pin_golden_chunks.py 를 먼저 돌려라"
                      "(단, 세트=L3 는 이 스크립트로 못 고친다 — 위 평가대상() 주석 참고).")

        # 🔴 L3(2026-09-07) — `tenant.l3_articles` 에 사는 문서라 org_id 를 몰라야 정상인
        #    corpus.chunks 기반 판정은 L3 컨텍스트를 못 본다. `orchestrate.판정()` 은 org_id
        #    를 이미 받는다(그동안 eval 이 안 넘겼을 뿐) — golden_set.정답근거[].article_id
        #    로 그 문항이 가리키는 기관을 되짚어 넘긴다. 이게 없으면 "판정일치율만 채점"이
        #    L3 문서를 «한 번도 안 보고» 낸 점수가 되어 측정 자체가 무의미해진다.
        org_id맵: dict[int, str] = {}
        l3_gid들 = [m["gold_id"] for m in 문항 if m["세트"] == "L3"]
        if l3_gid들:
            cur.execute("SELECT gold_id, 정답근거 FROM eval.golden_set WHERE gold_id = ANY(%s)",
                        (l3_gid들,))
            근거맵 = {gid: (근거 or []) for gid, 근거 in cur.fetchall()}
            art_ids = {b["article_id"] for gid in l3_gid들 for b in 근거맵.get(gid, [])
                       if b.get("article_id")}
            art_org: dict[int, object] = {}
            if art_ids:
                cur.execute("SELECT article_id, org_id FROM tenant.l3_articles "
                            "WHERE article_id = ANY(%s)", (list(art_ids),))
                art_org = dict(cur.fetchall())
            for gid in l3_gid들:
                orgs = {art_org[b["article_id"]] for b in 근거맵.get(gid, [])
                        if b.get("article_id") in art_org}
                if len(orgs) == 1:
                    org_id맵[gid] = str(orgs.pop())
                else:
                    # 조용히 넘어가지 않는다 — org_id 없이 돌면 L3 컨텍스트 없이 판정된다
                    print(f"⚠️ gold={gid} L3 근거의 article_id 로 org_id 를 못 정했다"
                          f"(매칭 {len(orgs)}곳) — org_id 없이 판정된다", file=sys.stderr)

        # 비교 앵커 재료. `with` 밖(집계 절)에서는 conn 이 닫혀 있어 여기서 미리 읽는다.
        코퍼스버전값 = eval_store.코퍼스버전(cur)
        # 🔴 정답지 고정도 지문을 뜬다 (2026-09-06). 코퍼스는 지문이 있는데 정답지는
        #    라벨뿐이라 비대칭이었다 — `eval_store.골든고정()` 주석 참조. conn 이 닫히기
        #    전에 여기서 읽는다 (코퍼스버전값과 같은 이유).
        골든고정값 = eval_store.골든고정(cur)
        cur.execute("SELECT count(*), count(*) FILTER (WHERE verified) FROM corpus.rules")
        rules수 = dict(zip(("총", "verified"), cur.fetchone()))
        cur.execute("SELECT 적용대상, count(*) FROM corpus.chunks GROUP BY 1 ORDER BY 2 DESC")
        적용대상분포 = {r[0]: r[1] for r in cur.fetchall()}
        # 🔴 `with` 밖(집계 절)에서는 conn 이 닫혀 있다. 여기서 미리 읽는다
        코드목록, 코드출처, 코드대조 = _강등코드목록(cur)
        # 🔴 «어느 DB 의 어느 롤로 돌았나». 비밀번호는 담지 않는다.
        #    ai-a3 실측: RLS 가 걸린 롤로 실판정을 돌리면 미매핑 전제 저장이 죽으면서
        #    약 46%가 「판단불가」로 잡힌다 — 모델 판단이 아니라 저장 실패다.
        #    ⚠️ 다만 이 하네스는 `판정(..., 기록=False)` 로 부르고 `_unmapped_적재` 는
        #       `orchestrate.py:340` 의 `if c and 기록:` 뒤에 있어 **발화 자체를 안 한다.**
        #       그래도 조건은 박는다 — 「안 걸렸다」를 다음 사람이 확인할 수 있어야 한다.
        cur.execute("SELECT current_database(), current_user, "
                    "coalesce(inet_server_addr()::text, 'local'), "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user), "
                    "version()")
        _d = cur.fetchone()
        DB신원 = {"db": _d[0], "role": _d[1], "host": _d[2], "bypassrls": _d[3],
                "서버": (_d[4] or "").split(" on ")[0],
                "미매핑적재_발화": False, "근거": "판정(기록=False) · orchestrate.py:340"}

        # 고정 정답 좌표를 미리 읽는다 — 문항마다 재계산하지 않는다(결정성).
        정답청크: dict[int, set[int]] = {}
        정답좌표: dict[int, set[tuple]] = {}
        for m in 문항:
            gid = m["gold_id"]
            정답청크[gid] = eval_store.정답청크(cur, gid)
            # 🔴 정답 좌표는 `golden_chunks.조번호` 가 아니라 **그 청크의 좌표**로 잡는다.
            #    golden_chunks.조번호 는 정답셋 원표기('제20조①' · '[붙임2] 외주용역비
            #    유의사항')를 그대로 보존한 것이라 인용 쪽 표기('제20조' · '붙임2')와 다르다.
            #    실측: 104행 중 85행이 형식 불일치 → 그대로 비교하면 **인용적중이 허위 0%** 로
            #    나온다. chunk_id 는 이미 고정돼 있으니 corpus.chunks 에서 좌표를 읽으면
            #    표기가 자동으로 맞춰지고 기준 문서가 한 곳(chunks)으로 유지된다.
            cur.execute("SELECT c.doc_id, c.조번호 FROM eval.golden_chunks gc "
                        "JOIN corpus.chunks c ON c.chunk_id = gc.chunk_id "
                        "WHERE gc.gold_id = %s", (gid,))
            정답좌표[gid] = {(r[0], r[1]) for r in cur.fetchall()}

        print(f"E2E {'드라이런' if dry else '실전'} · {len(문항)}문항 "
              f"(세트={','.join(세트) if 세트 else '전체'}"
              f"{' · 부분집합=' + 부분집합표기 if 부분집합표기 else ''}) · "
              f"top_k={top_k} · 변형={변형} · 폐포사용={폐포사용} · 동시={동시} · "
              f"원본포획={'on' if RAW else 'off'}"
              f"{'(프롬프트 전문 포함)' if RAW and RAW_PROMPT else ''}\n")

        import orchestrate  # 여기서 import 한다 — --help 만 볼 때 모델을 안 올리게
        if INJECT and not RAW:
            # 🔴 꽂기는 조립 래퍼 위에서만 돈다. 래퍼가 없으면 아무것도 안 꽂히는데
            #    run 은 멀쩡히 끝난다 — 「2판을 돌렸다」고 믿는 채로 1판이 하나 더 생긴다.
            sys.exit("SUDDOE_EVAL_INJECT_GOLD=1 은 SUDDOE_EVAL_RAW=1 이 필요하다 "
                     "(꽂기는 조립 래퍼가 한다)")
        if RAW:
            감싸기()

        # 🔴 A2(레인A, 2026-09-05) — `--동시 > 1` 은 RAW·INJECT 와 같이 못 쓴다.
        #    `_포획`·`_꽂을청크` 는 전역이고 문항 단위로 지우고 채운다(위 `감싸기()`,
        #    아래 순차 루프). 두 문항이 동시에 돌면 한쪽이 지운 걸 다른 쪽이 읽는다 —
        #    「레인 지표는 서로 오염된다」와 같은 반이다. 격리(contextvars·프로세스 분리)
        #    없이 그냥 스레드로 감싸면 조용히 틀린 값이 나오므로, 여기서는 **막는다**.
        if 동시 > 1 and (RAW or INJECT):
            sys.exit(
                "🔴 --동시 > 1 은 SUDDOE_EVAL_RAW=1 · SUDDOE_EVAL_INJECT_GOLD=1 과 "
                "같이 못 쓴다 — 그 둘은 전역 `_포획`·`_꽂을청크` 를 문항 단위로 지우고 "
                "채우는데, 문항이 동시에 돌면 서로 덮어쓴다(격리 미구현). "
                "--동시 1 로 돌리거나 그 환경변수들을 꺼라.")

        def _판정호출(m: dict, *, conn) -> tuple[int, dict, str | None]:
            """1문항 판정. (gold_id, r, 오류메시지) — 오류면 r 은 판단불가 스텁.

            🔴 순차·병렬 두 경로가 **이 함수 하나**를 부른다. 갈라 두면 언젠가
            한쪽만 고치고 다른 쪽을 잊는다(이 프로젝트가 여러 번 겪은 사고 형태).
            `conn` 은 호출부가 정한다 — 순차는 공유 커넥션을, 병렬은 `None`(문항마다
            제 커넥션)을 준다. psycopg 커넥션은 스레드 간 공유가 안 된다
            (`orchestrate._병렬_l3`·`_병렬_검색` 이 이미 같은 이유로 그렇게 한다).
            """
            gid = m["gold_id"]
            사업 = eval_store.사업키(m["사업명"])
            try:
                r = orchestrate.판정(m["질문"], 사업명=사업, dry=dry, top_k=top_k,
                                    conn=conn, 기록=False, 변형=변형,
                                    폐포사용=폐포사용,
                                    org_id=org_id맵.get(gid))  # 🔴 L3(2026-09-07). 위 주석
                return gid, r, None
            except Exception as e:
                traceback.print_exc(limit=2)
                # 🔴 예외도 판단불가다. 조용히 빼면 분모가 흔들린다 (계약 §7).
                r = {"판정": "판단불가", "요약": f"[예외] {type(e).__name__}",
                     "강등코드": [], "강등사유": [f"eval_e2e 예외: {e}"],
                     "경로": "예외", "인용목록": []}
                return gid, r, f"{type(e).__name__}: {e}"

        def _채점항목(gid: int, m: dict, r: dict) -> dict:
            """`_판정호출` 의 결과 하나를 `items` 행 하나로. 순차·병렬 공용."""
            예측 = r.get("판정") or "판단불가"
            정답 = m["정답판정"]
            검색 = r.get("검색") or {}
            top5 = list(검색.get("top5") or [])

            return {
                "gold_id": gid,
                "예측": 예측,
                "정답": 정답,
                "적중": 예측 == 정답,
                "원출력": {
                    "판정": 예측,
                    "요약": r.get("요약"),
                    "신뢰등급": r.get("신뢰등급"),
                    "인용목록": r.get("인용목록") or [],
                    "전제목록": r.get("전제목록") or [],
                    # 🔴 2026-09-07(ai-33 QA ③) — `orchestrate.판정()` 은 "해야할일" 을
                    #    돌려주는데 이 하네스가 그동안 안 담았다. 그래서 S1 변형이
                    #    해야할일을 0건으로 만드는 부작용을 A/B 가 못 잡았다 — 지표가
                    #    아니라 «수집 자체» 가 없었다. 원본 그대로 남긴다(가공은 지표()에서).
                    "해야할일": r.get("해야할일") or [],
                    "강등코드": r.get("강등코드") or [],
                    "강등사유": r.get("강등사유") or [],
                    "경로": r.get("경로"),
                    "실패단계": r.get("실패단계"),
                    # 🔴 2026-09-05 — 이게 없어서 Run A 의 실패경로 30여 건을 스택으로
                    #    못 짚었다. `orchestrate.py:832` 가 응답에 넣어 두는데 여기서
                    #    안 베끼고 있었다. 예외 «이름»은 `강등사유` 에 남지만 어느 줄에서
                    #    났는지는 스택이 있어야 안다.
                    "트레이스": r.get("트레이스"),
                    "게이트값": 검색.get("게이트값"),
                    # 🔴 `orchestrate.판정()` 은 **dry 분기에서만** 이걸 채운다 —
                    #    실전 update 에는 이 키가 없어서 run 191 은 93/93 None 이었다.
                    #    실전에서는 조립 래퍼가 잡은 값으로 메운다(`SUDDOE_EVAL_RAW=1`).
                    #    `--max-model-len` 이 문항 집합에 맞는지 재는 유일한 실측치다
                    #    (프롬프트가 넘치면 맨 뒤 B6=질문이 잘린다).
                    "프롬프트길이": (r.get("프롬프트길이")
                                or (_포획.get("조립") or {}).get("프롬프트길이")),
                    "지연ms": r.get("지연ms") or {},
                    "s맵": r.get("s맵") or {},
                    # 🔴 2026-09-05 — 게이트 C 는 두 갈래를 «실제로 판정한다»
                    #    (`orchestrate.py:611-620` 재귀 호출). 그런데 그 결과를 여기서
                    #    안 베껴서 **문항당 LLM 4회(2갈래 x 2회)를 태우고 버리고 있었다.**
                    #    run 195 에서 8문항 = 32회다. 그리고 채점은 부모의 "선택필요" 만
                    #    보므로 게이트 C 는 «반드시 오답» 이 된다(골든에 선택필요 정답 0건).
                    #    이걸 남겨야 「두 갈래가 합의하면 그 판정을 쓴다」로 바꿨을 때의
                    #    이득을 **GPU 없이** 계산할 수 있다. 안 남기면 매번 다시 돌려야 한다.
                    "갈래": [{"비목": g.get("비목"), "판정": g.get("판정"),
                             "경로": g.get("경로"),
                             # 근거적중은 eval 이 매기는 값이라 갈래엔 «없다». None 을
                             # 남기면 「안 닿았다」로 읽힌다 — 인용목록만 남기고 뺐다.
                             "인용목록": g.get("인용목록") or []}
                            for g in (r.get("갈래") or [])],
                    "비목후보": r.get("비목후보") or [],
                    # 🔴 아래 셋은 **플래그와 무관하게 항상** 남긴다 (원출력 키가 3개 는다).
                    #    `모델.종료이유`(finish_reason)는 «판단불가가 모델의 선택인가
                    #    잘림인가»를 가르는 값이라 플래그 뒤에 두면 안 된다 — 플래그를 안 켠
                    #    run 이 또 나오면 run 191 과 똑같이 못 닫힌다.
                    #    `dangling` 은 run 191 원출력에 `검색` 키가 0/93 이라 통째로 없었다.
                    "모델": r.get("모델") or {},
                    "dangling": (검색.get("dangling") or []),
                    "후보수": 검색.get("후보수"),
                    # 🔴 P2 요청. orchestrate:688·554 가 응답에 이미 넣어 둔 것을
                    #    eval 이 안 베끼고 있었을 뿐이다 — 문항당 200바이트 안팎.
                    #    run 191 은 `기록=False` 라 tenant.decisions 에도 안 남아
                    #    **실제 LLM 품목·용도가 어디에도 없다.** 이게 없으면 P2 의
                    #    금지예시·비목 축은 GPU 를 다시 켜야만 열린다.
                    "정규화": r.get("정규화"),
                    "비목": r.get("비목"),
                    "게이트": r.get("게이트"),
                    "금지근거": r.get("금지근거"),      # 게이트 A 일 때만 찬다
                    # 검증 «전» 1겹 원본 일습. `SUDDOE_EVAL_RAW=1` 일 때만 찬다.
                    **({"원본": dict(_포획)} if RAW else {}),
                    # 채점 재료. 나중에 재채점할 때 다시 안 돌려도 되게 같이 남긴다
                    "top5": top5,
                    "근거적중": bool(set(top5) & 정답청크[gid]),
                    "인용적중": bool(_인용좌표(r) & 정답좌표[gid]),
                    "치명": _치명(정답, 예측),
                    # 실패단계가 있거나 경로에 '실패'/'예외'가 박혔으면 §8 실패 경로가 닫은 것이다.
                    # 'dry중단' 도 모델의 선택이 아니다 — LLM 을 아예 안 불렀다.
                    # 이 셋 중 아무것도 아닌 판단불가만 "모델이 스스로 고른 판단불가" 다.
                    "실패경로": bool(r.get("실패단계")
                                  or any(k in (r.get("경로") or "")
                                         for k in ("실패", "예외", "dry"))),
                    "세트": m["세트"],
                    "적용범위": m["적용범위"],
                    "사업명": m["사업명"],
                    "골든_해야할일": m["해야할일"] or [],
                },
            }

        items: list[dict] = []
        오류: list[tuple[int, str]] = []
        꽂기없음: list[int] = []      # 정답청크가 0건이라 꽂을 게 없던 문항
        t0 = time.time()

        if 동시 <= 1:
            # 순차 — 기존 경로. **바이트 단위로 종전과 같다**(A2 검사점).
            for i, m in enumerate(문항, 1):
                gid = m["gold_id"]
                _포획.clear()          # 문항 단위 리셋. LLM 호출은 전부 메인 스레드다
                # 🔴 꽂기는 «문항별» 정답청크다. 매 문항 갈아 끼우지 않으면 앞 문항 것이
                #    다음 문항에 새어 들어가 전 문항이 오염된다.
                _꽂을청크[:] = sorted(정답청크[gid]) if INJECT else []
                if INJECT and not _꽂을청크:
                    꽂기없음.append(gid)
                _, r, err = _판정호출(m, conn=conn)
                if err:
                    오류.append((gid, err))
                items.append(_채점항목(gid, m, r))
                if i % 10 == 0 or i == len(문항):
                    print(f"  {i}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)
        else:
            # 병렬 — A2(레인A 2026-09-05). RAW·INJECT 는 위 가드가 이미 막았다.
            # 🔴 `conn=None` — 판정마다 제 커넥션을 연다. 순차 경로의 공유 `conn` 을
            #    그대로 스레드에 넘기면 psycopg 커넥션이 동시에 두 스레드에서 쓰여
            #    깨진다(순차 경로는 절대 이렇게 바꾸지 않는다 — 그게 위 등가성의 근거다).
            from concurrent.futures import ThreadPoolExecutor, as_completed
            결과: dict[int, tuple[dict, dict, str | None]] = {}
            완료 = 0
            with ThreadPoolExecutor(max_workers=동시) as ex:
                fut = {ex.submit(_판정호출, m, conn=None): m for m in 문항}
                for f in as_completed(fut):
                    m = fut[f]
                    gid, r, err = f.result()
                    결과[gid] = (m, r, err)
                    완료 += 1
                    # 🔴 «완료되는 대로» 한 줄. 저장은 아래에서 원래 순서로 다시 한다 —
                    #    이 print 는 관측용이라 items 를 만들지 않는다(순서 오염 금지).
                    try:
                        _정답 = m["정답판정"]
                        _예측 = (r.get("판정") or "판단불가") if not err else f"오류:{err[:40]}"
                        _검색 = r.get("검색") or {}
                        _top5 = list(_검색.get("top5") or [])
                        _근 = "○" if set(_top5) & 정답청크[gid] else "✗"
                        _인 = "○" if _인용좌표(r) & 정답좌표[gid] else "✗"
                        _치 = " 🔴치명" if _치명(_정답, _예측) else ""
                        _경 = r.get("경로") or ""
                        _강 = ",".join(r.get("강등코드") or [])
                        _표 = "OK" if _예측 == _정답 else "XX"
                        print(f"  [{완료}/{len(문항)}] gold={gid} {_표} 정답={_정답} "
                              f"예측={_예측}{_치} | 근거{_근} 인용{_인} | {_경}"
                              + (f" 강등={_강}" if _강 else ""), flush=True)
                    except Exception as _e:      # 관측이 본 실행을 죽이면 안 된다
                        print(f"  [{완료}/{len(문항)}] gold={gid} (표시실패 {_e})", flush=True)
                    if 완료 % 10 == 0 or 완료 == len(문항):
                        print(f"  {완료}/{len(문항)} · {time.time()-t0:.0f}초", flush=True)
            # 🔴 완료 순서(=응답이 빨리 온 순서)가 아니라 **문항 원래 순서**로 저장한다 —
            #    안 그러면 run 마다 items 순서가 흔들려 파일 diff 로 대조가 안 된다.
            for m in 문항:
                gid = m["gold_id"]
                _, r, err = 결과[gid]
                if err:
                    오류.append((gid, err))
                items.append(_채점항목(gid, m, r))

        경과 = time.time() - t0

    # ── 집계 ────────────────────────────────────────────────────────────
    def 묶음(pred) -> list[dict]:
        return [it for it in items if pred(it)]

    def 지표(부분: list[dict]) -> dict:
        n = len(부분) or 1
        # 🔴 **다수결 기준선을 같이 낸다** (2026-09-01. 오너 지시 «정답셋 성역을 깨라»).
        #    정답셋 정답이 «불가» 로 쏠려 있어(채점 62문항 기준 66.1%) 일치율 한 숫자만
        #    보면 실력인지 쏠림인지 갈리지 않는다. **상수 예측기('항상 불가')가 몇 점인지**를
        #    같은 분모로 찍어 두면, 지표가 기준선을 못 넘었을 때 그 사실이 표에서 바로 보인다.
        #    `초과`(일치율 - 기준선)가 이 평가의 진짜 신호다. 양수가 아니면 개선이 아니다.
        정답분포 = Counter(it["정답"] for it in 부분)
        # 🔴 판단불가만 빼던 조건을 «정답청크가 실제로 있는가» 로 일반화했다(2026-09-07).
        #    둘은 원래 같은 뜻이었다(판단불가는 고정할 청크가 없다) — 그런데 세트=L3 도
        #    똑같이 "고정할 청크가 없다"(청크가 아니라 tenant.l3_articles 에 산다)이면서
        #    정답판정은 판단불가가 아닌 경우가 있어, 그 경우만 걸러내던 옛 조건으로는 빠졌다.
        #    `정답청크` 는 이 함수를 감싸는 `실행()` 의 클로저 변수다(위에서 채움).
        인용분 = [it for it in 부분
                if it["정답"] != "판단불가" and 정답청크.get(it["gold_id"])]
        기준선 = (정답분포.most_common(1)[0][1] / n * 100) if 부분 else 0.0
        일치 = sum(it["적중"] for it in 부분) / n * 100
        return {
            "문항수": len(부분),
            "일치율": round(일치, 1),
            "다수결기준선": round(기준선, 1),
            "기준선초과": round(일치 - 기준선, 1),
            "최빈정답": 정답분포.most_common(1)[0][0] if 부분 else None,
            "정답분포": dict(정답분포),
            # 🔴 2026-09-07 — 「치명오답」 정의가 두 가지로 읽혀서(ai-33 QA) 둘 다 남긴다.
            #    _좁음 = 기존 `_치명()`(정답∈{불가,조건부} & 예측=='가능') — "안 되는 걸
            #    된다고 한 것"만. _넓음 = 정답=='불가' & 예측∈{조건부,가능} — "불가를
            #    조건부로 낮춘 것"까지 포함(불가→가능은 둘 다에 든다). 발표에선 정의를
            #    밝히고 나란히 쓴다 — 아무거나 «치명오답»으로 단독 인용하지 않는다.
            #    `치명오답`(키 이름 그대로)은 하위호환을 위해 _좁음과 같은 값을 유지한다.
            "치명오답": sum(it["원출력"]["치명"] for it in 부분),
            "치명오답_좁음": sum(it["원출력"]["치명"] for it in 부분),
            "치명오답_넓음": sum(it["정답"] == "불가" and it["예측"] in ("조건부", "가능")
                            for it in 부분),
            # 🔴 2026-09-07(ai-33 QA ③) — «존재 카운트» 기반이다. 골든 해야할일 항목과
            #    모델 항목의 텍스트를 의미로 대조하려면 LLM 판사가 필요한데, 이번엔
            #    비용·재현성 문제로 뺐다(ai-33 승인) — "냈다/안 냈다" 만 잰다.
            #    분모=골든에 해야할일이 있는 문항. 그 분모가 0이면 None(적용 불가를 숨기지 않는다).
            "해야할일_채택률": (round(
                sum(1 for it in 부분
                    if it["원출력"]["골든_해야할일"] and it["원출력"]["해야할일"])
                / sum(1 for it in 부분 if it["원출력"]["골든_해야할일"]) * 100, 1)
                if any(it["원출력"]["골든_해야할일"] for it in 부분) else None),
            "해야할일_채점분모": sum(1 for it in 부분 if it["원출력"]["골든_해야할일"]),
            "판단불가율": round(sum(it["예측"] == "판단불가" for it in 부분) / n * 100, 1),
            # 🔴 판단불가를 한 숫자로 세면 안 된다 (2026-09-01 실전 E2E 에서 드러난 결함).
            #    §7 은 "판단불가율 0% 면 근거 없이 답을 만들고 있다는 뜻" 이라고 경고하는데,
            #    그 경고가 풀렸는지는 **모델이 스스로 골랐는가**로만 판정된다.
            #    실전 1차(run_id=189)에서 판단불가 5건이 전부 max_tokens=1500 잘림
            #    (§8 실패 경로)이었다 — 6.5% 만 보면 경고가 풀린 것처럼 보이지만
            #    모델이 스스로 고른 횟수는 0 이었다. 두 개를 갈라서 센다.
            "판단불가_모델선택": sum(it["예측"] == "판단불가" and not it["원출력"]["실패경로"]
                                for it in 부분),
            "판단불가_실패경로": sum(it["예측"] == "판단불가" and it["원출력"]["실패경로"]
                                for it in 부분),
            # 🔴 정답이 «판단불가» 인 문항은 근거·인용 채점에서 뺀다 (2026-09-01).
            #    규범에 답이 없다는 것이 정답이라 **고정할 정답 청크가 없다** — 분모에
            #    두면 맞힐 수 없는 문항이 섞여 인용적중률이 구조적으로 깎인다.
            #    판정일치율에는 그대로 든다. 분모가 다르므로 따로 찍는다.
            "인용채점분모": len(인용분),
            "근거적중률": (round(sum(it["원출력"]["근거적중"] for it in 인용분)
                              / len(인용분) * 100, 1) if 인용분 else None),
            "인용적중률": (round(sum(it["원출력"]["인용적중"] for it in 인용분)
                              / len(인용분) * 100, 1) if 인용분 else None),
        }

    전체 = 지표(items)
    분해 = {
        "공통": 지표(묶음(lambda it: it["원출력"]["적용범위"] is not None)),
        "사업지정": 지표(묶음(lambda it: it["원출력"]["사업명"] is not None)),
    }
    # 🔴 "L3경로" 죽은 차원은 여기서 뺐다(위 모듈 docstring 참고) — 바로 아래 세트별
    #    루프가 세트='L3' 일 때 자동으로 "세트:L3" 를 만든다. 별도 차원 불필요.
    for s in sorted({it["원출력"]["세트"] for it in items}):
        분해[f"세트:{s}"] = 지표(묶음(lambda it, s=s: it["원출력"]["세트"] == s))

    # 🔴 묶음별. 통 숫자로 인용하면 «판정력» 이 아니라 «정답셋 난이도 배합» 을 잰다 —
    #    run 191 닿음: 튜닝52 58.8% · held-out41 94.3% · 전체 76.8%. 세 수가 다 다르다.
    for 이름, 세트들 in _묶음정의.items():
        분해[f"묶음:{이름}"] = 지표(묶음(
            lambda it, S=set(세트들): it["원출력"]["세트"] in S))

    # 🔴 2판(꽂기) 채점 규칙 — ai-e8 확정 2026-09-03. **합산 금지.**
    #    실측: 격리48 중 «프롬프트가 실제로 바뀐» 문항은 20 뿐이고 28 은 정답청크가
    #    이미 top-5 에 있어 프롬프트가 바이트 단위로 같다. 온도 0 이면 1판과 같은 답이다.
    #    합쳐 내면 「2판 ≈ 1판」이 **구조적으로** 나오고 그걸 「병목은 판정」으로 읽는다.
    #      · 판정력 물음은 «주입됨» 에서만 답한다
    #      · «동일» 이 1판과 다르면 그 자체가 신호다 — 온도 0 인데 달라졌다 = 비결정성
    if INJECT:
        분해["꽂기:주입됨"] = 지표(묶음(_주입됨))
        분해["꽂기:동일"] = 지표(묶음(lambda it: not _주입됨(it)))

    # 4-way 혼동행렬 — "어디로 틀렸나" 는 일치율 한 숫자로는 안 보인다
    혼동 = {f"{a}->{b}": 0 for a in 판정4 for b in 판정4}
    for it in items:
        키 = f"{it['정답']}->{it['예측']}"
        if 키 in 혼동:
            혼동[키] += 1
    혼동 = {k: v for k, v in 혼동.items() if v}

    코드빈도: dict[str, int] = {}
    for it in items:
        for c in it["원출력"]["강등코드"]:
            코드빈도[c] = 코드빈도.get(c, 0) + 1

    # 🔴 경로 빈도 — 일치율 한 숫자로는 "어느 분기에서 죽었나" 가 안 보인다.
    #    드라이런에서 이게 전부 'dry중단' 이 아니면 배관이 중간에 끊긴 것이다.
    경로빈도: dict[str, int] = {}
    for it in items:
        k = it["원출력"]["경로"] or "(없음)"
        경로빈도[k] = 경로빈도.get(k, 0) + 1

    # top5 를 한 건도 못 받았으면 근거적중률은 0% 가 아니라 **미측정**이다.
    # 0% 로 적으면 "검색이 다 틀렸다" 로 오독된다.
    근거측정 = any(it["원출력"]["top5"] for it in items)

    if not 근거측정:
        for g in [전체, *분해.values()]:
            g["근거적중률"] = None
    # 🔴 P1(ai-2c) 요청: run 마다 강등코드 «전량» 을 0 포함해 남긴다.
    #    「안 울린 0」은 뜻이 서로 다르다 — 정답셋으로 원리적으로 못 태우는 0
    #    (`PRECEDENCE_FLIP`·`TENANT_LEAK` — L3 0/93 · org_id None),
    #    guided_json 이 앞에서 막아서 나는 건강한 0(`INVALID_JUDGMENT` 등),
    #    그냥 조건 미충족인 0. 어느 0 이 다음 run 에서 1 이 되는지가 신호다.
    #    2판(꽂기)은 근거를 꽂으니 `PREMISE_UNMAPPED`·`CITE_HANG_MISMATCH` 가
    #    움직여야 정상이고, 안 움직이면 그게 사고다.
    강등표 = {c: 코드빈도.get(c, 0) for c in 코드목록}
    목록밖 = sorted(set(코드빈도) - set(코드목록))
    for c in 목록밖:
        강등표[c] = 코드빈도[c]
    print(f"\n강등코드 {len(코드목록)}종 중 발화 {sum(1 for v in 강등표.values() if v)}종"
          f" (기준 {코드출처})")
    if 목록밖:
        print(f"  🔴 목록에 «없는» 코드가 울렸다: {목록밖} — 저장이 CHECK 에 막힌다")
    if 코드대조["CHECK에만"] or 코드대조["소스에만"]:
        print(f"  🔴 DB CHECK 와 소스가 어긋난다: CHECK에만 {코드대조['CHECK에만']} · "
              f"소스에만 {코드대조['소스에만']}")

    지표전체 = {**전체, "분해": 분해, "혼동": 혼동, "강등코드빈도": 코드빈도,
              "강등코드표": 강등표, "강등코드_목록출처": 코드출처,
              "강등코드_목록밖": 목록밖, "강등코드_닻대조": 코드대조,
              "경로빈도": 경로빈도, "근거측정": 근거측정,
              "소요초": round(경과, 1), "오류": len(오류)}

    # ── 출력 ────────────────────────────────────────────────────────────
    print(f"\n{'='*66}\nE2E {'드라이런' if dry else '실전'} 결과  ({경과:.0f}초)\n{'='*66}")
    # 🔴 프롬프트 길이 분포 — `pod_serve.sh --max-model-len` 의 근거다 (2026-09-01).
    #    24576 은 **구 77문항 실측**으로 잡은 값이라 문항이 늘면 다시 재야 한다.
    #    넘치면 에러가 아니라 **앞이 잘린 채 조용히 답한다** — 질문이 사라진 오답이다.
    길이 = sorted(x for x in (it["원출력"].get("프롬프트길이") for it in items) if x)
    if 길이:
        # 🔴 MML 이 24576 으로 **하드코딩**돼 있었다. run 191 은 40960 으로 떴는데
        #    이 줄은 24576 기준으로 「초과 14건」을 찍는다 — 표와 서버가 다른 수를 본다.
        #    이제 `--max-model-len` 으로 받고 `설정` 에 박는다. 기본값은 24576 그대로라
        #    안 주면 현행과 같은 줄이 나온다.
        # 🔴 0.7 은 «가정» 이었다. 2026-09-03 에 실제 Qwen3-32B-AWQ 토크나이저로
        #    이 하네스가 뽑은 프롬프트 95건을 전부 쟀다:
        #      토큰/자  최소 0.407 · 중앙 0.732 · **최대 0.757**
        #      (짧을수록 토큰/자가 작고, 길수록 0.73~0.76 으로 수렴한다)
        #    0.7 을 쓰면 가장 긴 문항에서 토큰을 **덜 세서** 없는 「OK」가 나온다.
        #    한도는 토큰인데 여기서 세는 건 자수다 — 그래서 상한(0.76)으로 잡는다.
        #    ⚠️ 이 줄은 여전히 «추정» 이다. 확정 수치는 밖에서 토크나이저로 잰다.
        MML, 자당토큰 = max_model_len, 0.76
        여유 = int((MML - 3000) / 자당토큰)  # max_tokens=3000 을 뺀 입력 예산(자)
        넘 = [x for x in 길이 if x > 여유]
        p90 = 길이[min(len(길이) - 1, int(len(길이) * 0.9))]
        print(f"프롬프트 자수  중앙 {길이[len(길이)//2]:,} · p90 {p90:,} · "
              f"최장 {길이[-1]:,} · n={len(길이)}")
        print(f"  입력 예산 {여유:,}자 (max-model-len {MML:,} - max_tokens 3,000, {자당토큰}토큰/자)"
              f" -> 초과 {len(넘)}건" + ("  🔴 --max-model-len 을 올려라" if 넘 else "  OK"))
        # 🔴 위 줄은 «문항당 1개» 를 센다. 게이트 C 재귀 문항은 조립이 2회라
        #    실제로 서버에 나가는 프롬프트가 더 많다 — 예산은 «나간 것 전부» 로 봐야 한다.
        전부 = sorted(x["프롬프트길이"] for it in items
                     for x in ((it["원출력"].get("원본") or {}).get("조립_전부") or []))
        if 전부 and len(전부) != len(길이):
            넘2 = [x for x in 전부 if x > 여유]
            print(f"  조립 «전부» 기준 n={len(전부)} (문항 {len(길이)} + 게이트C 재귀 "
                  f"{len(전부)-len(길이)}) · 최장 {전부[-1]:,} -> 초과 {len(넘2)}건")
    머리 = (f"{'구간':12}{'문항':>5}{'일치율':>9}{'기준선':>8}{'초과':>8}{'치명':>6}"
            f"{'판단불가(모델)':>17}{'근거적중':>10}{'인용적중':>10}")
    print(머리)
    print("-" * len(머리))

    def 줄(이름, g):
        근거 = f"{g['근거적중률']:9.1f}%" if g["근거적중률"] is not None else f"{'미측정':>9} "
        인용 = f"{g['인용적중률']:9.1f}%" if g["인용적중률"] is not None else f"{'미측정':>9} "
        불가 = f"{g['판단불가율']:.1f}%(모델{g['판단불가_모델선택']})"
        초과 = f"{g['기준선초과']:+.1f}"
        print(f"{이름:12}{g['문항수']:5}{g['일치율']:8.1f}%{g['다수결기준선']:7.1f}%{초과:>8}"
              f"{g['치명오답']:6}{불가:>17}{근거}{인용}")

    줄("전체", 전체)
    if 전체["기준선초과"] <= 0:
        print(f"\n🔴 일치율이 다수결 기준선({전체['다수결기준선']}%, 항상 "
              f"'{전체['최빈정답']}')을 넘지 못했다. 이 수치는 판정 실력의 증거가 아니다.\n"
              f"   정답 분포: {전체['정답분포']}")
    for k, v in 분해.items():
        if v["문항수"]:
            줄("  " + k, v)

    print("\n4-way 혼동 (정답->예측, 0건 생략)")
    for k, v in sorted(혼동.items(), key=lambda x: -x[1]):
        표 = "  🔴" if _치명(k.split("->")[0], k.split("->")[1]) else "    "
        print(f"{표} {k:22} {v}")

    print("\n경로 빈도 (어느 분기에서 끝났나)")
    for k, v in sorted(경로빈도.items(), key=lambda x: -x[1]):
        print(f"    {k:46} {v}")
    if not 근거측정:
        print("    ⚠️ top5 를 한 건도 못 받았다 — 근거적중률은 0% 가 아니라 미측정이다.")

    if 코드빈도:
        print("\n강등코드 빈도")
        for k, v in sorted(코드빈도.items(), key=lambda x: -x[1]):
            print(f"    {k:26} {v}")

    if 오류:
        print(f"\n🔴 예외 {len(오류)}건 (판단불가로 계상, 분모에서 빼지 않았다)")
        for gid, msg in 오류[:10]:
            print(f"    gold_id={gid} {msg}")

    치명 = 전체["치명오답"]
    if 치명:
        print(f"\n🔴 치명 오답 {치명}건 — 계약 §7 정지 조건. 머지 금지.")
        for it in items:
            if it["원출력"]["치명"]:
                print(f"    gold_id={it['gold_id']} 정답={it['정답']} 예측={it['예측']}")
    if not dry:
        모델선택 = 전체["판단불가_모델선택"]
        실패경로 = 전체["판단불가_실패경로"]
        print(f"\n판단불가 {모델선택 + 실패경로}건 = 모델 선택 {모델선택} + §8 실패 경로 {실패경로}")
        if 모델선택 == 0:
            print("🔴 **모델이 스스로 판단불가를 고른 횟수가 0 이다.** 계약 §7 의 경고는 "
                  "아직 해소되지 않았다 — 실패 경로가 닫은 건수는 이 경고를 풀어주지 않는다.")
            if 실패경로:
                print(f"   (판단불가 {실패경로}건은 전부 §8 실패 경로다. 사고를 안전하게 닫은 "
                      "결과일 뿐 '근거가 없으면 답하지 않는다' 의 증거가 아니다.)")

    # ── 적재 ────────────────────────────────────────────────────────────
    # 🔴 가드 ③ — **저장 «전»에** 죽는다. 반쯤 빈 run 이 남는 게 제일 나쁘다.
    _가드3(items, dry)

    # 🔴 run 191 의 `설정` 은 {dry, limit, top_k, 세트, 채점, 정답고정} 뿐이었다.
    #    변형·max-model-len·max_tokens·GPU·코퍼스버전·켜진 플래그가 하나도 없어
    #    「이 숫자가 무엇 때문에 나왔나」를 답할 수 없다. 전부 박는다.
    #    ① 의 최대토큰은 400, ④ 는 1500 이다(자리마다 다르다) — 실제로 나간 값을
    #    래퍼가 잰 대로 적고, 안 켰으면 None 으로 둔다. 주장하지 않는다.
    # ── 꽂기 결산 ──────────────────────────────────────────────────────
    # 🔴 「꽂았다」와 「꽂으려 했다」는 다르다. 게이트 A·B·C 로 조립 «전에» 끝난 문항은
    #    정답 청크를 봤을 리가 없다. 그 수를 안 세면 2판의 분모가 거짓이 된다.
    꽂기표 = None
    if INJECT:
        꽂 = [((it["원출력"].get("원본") or {}).get("조립") or {}).get("꽂기") for it in items]
        탄 = [x for x in 꽂 if x]
        꽂기표 = {"문항": len(items), "조립까지_간_문항": len(탄),
               "조립전_종료": len(items) - len(탄),
               "정답청크_0건이라_못꽂음": 꽂기없음,
               "실제주입_총건": sum(len(x["주입"]) for x in 탄),
               "이미_top5에_있던것_총건": sum(len(x["이미있던것"]) for x in 탄),
               # 🔴 채점 분모는 «총건» 이 아니라 이 둘이다 (ai-e8 확정)
               "주입된_문항": sorted(it["gold_id"] for it in items if _주입됨(it)),
               "동일한_문항": sorted(it["gold_id"] for it in items if not _주입됨(it))}
        print("\n꽂기: 조립까지 {}/{}문항 · 주입 {}건 · 이미 있던 것 {}건 · 못 꽂은 문항 {}건"
              .format(꽂기표["조립까지_간_문항"], len(items), 꽂기표["실제주입_총건"],
                      꽂기표["이미_top5에_있던것_총건"], len(꽂기없음)))
        print("  🔴 채점 분모: 프롬프트가 «실제로 바뀐» 문항 {}  ·  «바이트 동일» {}  — 합산 금지"
              .format(len(꽂기표["주입된_문항"]), len(꽂기표["동일한_문항"])))
        if 꽂기표["조립전_종료"]:
            print("  🔴 {}문항은 조립 «전» 에 끝났다 — 정답 청크를 못 봤다. 2판 분모에서 갈라 세라"
                  .format(꽂기표["조립전_종료"]))

    def _관측토큰(태그):
        vals = {v for it in items
                for v in [((it["원출력"].get("원본") or {}).get(태그) or [{}])[0]
                          .get("인자", {}).get("최대토큰")] if v is not None}
        return sorted(vals) or None

    설정 = {"dry": dry, "top_k": top_k, "세트": 세트, "limit": limit,
          "정답고정": {"표": "eval.golden_chunks(D3)", "지문": 골든고정값},
          "채점": "결정론 4-way + 치명오답 + 근거/인용 적중",
          "변형": 변형,
          # 🔴 2026-09-05 — `변형` 이름만으로는 «그 변형의 문면이 그날 무엇이었는지» 를
          #    못 잡는다. `git` 의 `dirty` 는 파일 경로만 나열해서(`M assemble_context.py`)
          #    커밋 전에 B0 를 두세 번 고쳐가며 돌린 run 들이 전부 같은 값으로 찍힌다.
          #    바이트 단위 해시를 같이 둬야 「이 run 이 어떤 B0 로 돌았나」가 닫힌다.
          #    F축 스키마가 바뀌어 허용경로 목록이 달라지는 것도 이 해시가 잡는다
          #    (git commit 으로는 못 잡는 드리프트다).
          "b0_sha1": _b0해시(변형),
          # 🔴 A1·A2(레인A 2026-09-05). 조건이 다른 run 끼리 수치를 빼면 안 된다는
          #    원칙 — 여기 안 박히면 다음 run 과 뭐가 다른지 못 가린다.
          "폐포사용": 폐포사용, "동시": 동시,
          "부분집합": 부분집합표기,
          "부분집합_유보": 부분집합유보,
          "문항수": len(items),
          "max_model_len": max_model_len,
          "관측_최대토큰_①": _관측토큰("①정규화LLM"),
          "관측_최대토큰_④": _관측토큰("④판정LLM"),
          "원본포획": RAW, "프롬프트전문": RAW and RAW_PROMPT,
          "꽂기": 꽂기표,
          # 🔴 2026-09-07 정정(ai-33·이 세션 교차확인) — 여기 있던 "org_id전달":None 이
          #    하드코딩 리터럴이라 org_id맵(위에서 채움)이 실제로 판정()에 넘어간 뒤에도
          #    계속 None을 찍고 있었다. 그걸 보고 「L3 안 실렸다」로 두 세션이 오독했다 —
          #    실측(원출력.인용목록의 doc_id가 L3 UUID인지)으론 26/27 이 L3 를 실제로
          #    인용했다. `org_id전달`은 이제 실측(org_id맵에 실제로 값이 있던 문항수)이다.
          #    `B1블록_문항수`는 SUDDOE_EVAL_RAW=1 없이는 항상 0이다(원본 캡처가 없어서
          #    "블록" 자체를 못 본다) — RAW 없을 때는 null 로 그 사실을 남긴다(0으로
          #    찍어 "안 실렸다"로 다시 오독되게 두지 않는다).
          "L3": {"B1블록_문항수": (sum(
                     1 for it in items
                     if "B1" in (((it["원출력"].get("원본") or {})
                                  .get("조립") or {}).get("블록") or {}))
                     if RAW else None),
                 "org_id전달": sum(1 for it in items if it["gold_id"] in org_id맵),
                 "기관ID전달": 0,
                 "메모": ("세트:L3 분해(위 분해.'세트:L3') 가 L3 판정력의 정본이다. "
                        "이 블록은 진단 보조일 뿐 «없음의 증거» 로 읽지 않는다.")},
          # 🔴 묶음 정의. 통 숫자로 인용하면 «판정력» 이 아니라 «정답셋 난이도 배합» 을 잰다
          #    (run 191 닿음: 튜닝52 58.8% · held-out41 94.3% · 전체 76.8%).
          "묶음": _묶음정의,
          "git": _헤드(),
          # 코퍼스버전은 컬럼에도 들어가지만 설정에도 박는다 — 표만 보고도 갈리게
          "코퍼스버전": 코퍼스버전값,
          # 🔴 코퍼스버전 해시는 (chunks, embedding, refs, documents, max chunk_id) 만
          #    본다. `corpus.rules` 와 `chunks.적용대상` 이 바뀌어도 해시는 그대로다 —
          #    그 둘은 검색 필터와 룰 경로의 입력이라 판정을 바꾼다. 따로 센다.
          "rules수": rules수, "적용대상분포": 적용대상분포,
          "DB": DB신원,
          "GPU": os.environ.get("SUDDOE_GPU"),
          # 🔴 env 가 아니라 «실제로 친 주소» 를 남긴다 (2026-09-05). 팟 주소를
          #    `ops.gpu_pod` 로 옮긴 뒤 env 는 낡은 팟을 가리킬 수 있고, 그 값을
          #    남기면 run 재현이 «다른 서버» 를 가리킨다. 이 프로젝트는 조건이 다른
          #    run 끼리 수치를 빼면 안 된다는 규칙이라 이 기록이 근거 자체다.
          "VLLM_URL": _실제_vllm_url(),
          "VLLM_URL_env": os.environ.get("VLLM_URL"),
          "VLLM_MODEL": os.environ.get("VLLM_MODEL"),
          "RUNPOD_POD_ID": os.environ.get("RUNPOD_POD_ID"),
          # 🔴 P2·P3 가 켜는 플래그가 무엇이었는지 남는다. 안 남기면 다음 run 과 못 뺀다
          "SUDDOE_플래그": {k: v for k, v in sorted(os.environ.items())
                        if k.startswith("SUDDOE_")}}
    # ── 블록 스냅샷 (`--스냅샷 <경로>`) ─────────────────────────────────
    # 🔴 P3 의 `SUDDOE_ROWSPLIT` on/off 대조용. **off 기준선을 먼저 떠 둔다** —
    #    on 을 돌린 뒤에 기준선을 뜨면 그건 대조가 아니라 사후 정당화다.
    #    비교는 이 파일 두 개의 순수 diff 다(문항 순서 고정 · gold_id 키).
    #    ⚠️ dry 에서 게이트가 먼저 닫히는 문항(`C비목갈림` 등)은 조립을 «안 탄다» —
    #       프롬프트가 없으므로 블록도 없다. 빼지 않고 `블록:null` 로 남긴다.
    if 스냅샷:
        if not RAW:
            sys.exit("--스냅샷 은 SUDDOE_EVAL_RAW=1 이 필요하다 (블록 해시는 조립 래퍼가 잡는다)")
        찍 = {"생성": datetime.now().isoformat(timespec="seconds"), "설정": 설정,
             "문항": {}}
        for it in items:
            조립 = ((it["원출력"].get("원본") or {}).get("조립") or {})
            찍["문항"][str(it["gold_id"])] = {
                "경로": it["원출력"].get("경로"),
                "프롬프트길이": 조립.get("프롬프트길이"),
                "프롬프트sha1": 조립.get("sha1"),
                "s맵크기": 조립.get("s맵크기"),
                "참조사슬수": 조립.get("참조사슬수"),
                # 게이트 C 재귀 문항은 여기가 2다. `블록`·`프롬프트sha1` 은 **마지막 1건**이다
                "조립호출수": len(((it["원출력"].get("원본") or {}).get("조립_전부") or [])),
                "조립_전부": [{k: v for k, v in x.items() if k != "프롬프트"}
                           for x in ((it["원출력"].get("원본") or {}).get("조립_전부") or [])],
                "블록": 조립.get("블록")}
        # 🔴 프롬프트 전문은 스냅샷 본문에 안 넣는다 — 스냅샷은 diff 로 읽는 파일이라
        #    1.6MB 원문이 섞이면 「어느 블록이 변했나」가 안 보인다. 옆 파일로 뺀다.
        #    토큰 수는 여기서 안 센다: 하네스에 transformers 를 끌어들이지 않는다
        #    (`scripts/` 는 API 이미지에 통째로 실린다). 밖에서 재라.
        if RAW_PROMPT:
            옆 = 스냅샷 + ".프롬프트.jsonl"
            with open(옆, "w", encoding="utf-8") as f:
                for it in items:
                    for i, x in enumerate(((it["원출력"].get("원본") or {})
                                           .get("조립_전부") or [])):
                        if "프롬프트" in x:
                            f.write(json.dumps(
                                {"gold_id": it["gold_id"], "회차": i,
                                 "경로": it["원출력"].get("경로"),
                                 "프롬프트": x["프롬프트"]}, ensure_ascii=False) + chr(10))
            print("프롬프트 전문 -> " + 옆)
        찍음 = sum(1 for v in 찍["문항"].values() if v["블록"])
        with open(스냅샷, "w", encoding="utf-8") as f:
            json.dump(찍, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"\n블록 스냅샷 -> {스냅샷}  (조립 탄 문항 {찍음}/{len(items)})")

    run_id = None
    if 기록:
        run_id = eval_store.기록(
            {"종류": "e2e",
             "설정": 설정,
             "문항수": len(items),
             "지표": 지표전체,
             "라벨": 라벨 or ("E2E 드라이런" if dry else "E2E 실전"),
             "비고": None},
            items)
        print(f"\neval.runs 기록 완료 — run_id = {run_id}")
    else:
        print("\n(--no-log) eval.runs 에 남기지 않았다")

    if 치명:
        sys.exit(2)          # 🔴 비0 종료로 머지를 막는다
    return run_id or 0



def _실제_vllm_url() -> str | None:
    """run 설정에 남길 «실제로 친» vLLM 주소. `ops.gpu_pod` 우선 · env 폴백."""
    try:
        from adapter import vllm_url
        return vllm_url()
    except Exception:                                             # noqa: BLE001
        return os.environ.get("VLLM_URL")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="LLM 없이 배관만. GPU 열기 전 필수")
    ap.add_argument("--limit", type=int)
    # 🔴 choices 를 손으로 적으면 세트가 늘 때마다 조용히 못 고르게 된다
    #    (실측: '공식'·'보강' 이 추가됐는데 목록은 2개로 굳어 있었다).
    # 🔴 choices 를 손으로 적으면 세트가 늘 때마다 조용히 못 고르게 된다 (위 주석).
    #    nargs='+' 로 여러 세트를 한 run 에 담는다 — 1개만 주면 기존 SQL 경로 그대로다.
    ap.add_argument("--세트", nargs="+", choices=_세트이름들())
    ap.add_argument("--top-k", type=int, default=5, dest="top_k")
    ap.add_argument("--변형", default="V0",
                    help="A12 프롬프트 변형. V0=기준선 · V1~V6 (assemble_context.변형들)")
    # 🔴 부분집합은 gold_id 로 고정한다. 세트 이름으로 다시 뽑지 마라 — 이름이 흔들리면
    #    튜닝분과 held-out 이 섞인다.
    ap.add_argument("--부분집합", choices=_부분집합이름들() or None,
                    help="scratchpad/P4_부분집합_0903.json 의 gold_id 목록 "
                         "(선택지는 그 파일이 정한다 — 여기 손으로 적지 마라)")
    ap.add_argument("--gold-ids", dest="gold_ids", help="쉼표구분 gold_id 직접 지정")
    # 🔴 기본값을 24576 -> 40960 으로 맞췄다 (2026-09-03). 24576 은 «구 77문항»
    #    실측치이고, 서버(`pod_serve.sh`)는 이제 40960 으로 뜬다. 기본값이 서버와
    #    다르면 이 줄이 또 없는 초과를 찍는다 — run 191 때 「초과 14건 🔴」이 그거였다.
    #    **단일 출처는 `pod_serve.sh` 다.** 거길 바꾸면 여기도 바꿔라.
    ap.add_argument("--max-model-len", type=int, default=40960, dest="max_model_len",
                    help="프롬프트 예산 계산·설정 기록용. 서버 기동값과 «같게» 줘라")
    ap.add_argument("--스냅샷", dest="스냅샷",
                    help="블록별 자수·sha1·S번호수를 JSON 으로 뜬다 (SUDDOE_EVAL_RAW=1 필요). "
                         "P3 행분해 on/off 대조의 기준선")
    ap.add_argument("--라벨")
    ap.add_argument("--no-log", action="store_true", help="eval.runs 에 남기지 않는다")
    # 🔴 A1(레인A 2026-09-05). 기본 on — 스위치를 넣기 전과 바이트 단위로 같다.
    ap.add_argument("--폐포", choices=["on", "off"], default="on",
                    help="B3(참조 확장·폐포) 을 조립에 넣을지. 기본 on(기존 동작). "
                         "off 면 프롬프트가 줄어든다 — 정확도 기여는 아직 안 쟀다")
    # 🔴 A2(레인A 2026-09-05). 기본 1(순차) — RAW·INJECT 는 1로 고정이다(위 가드).
    #    GPU 동시성 상한은 `pod_serve.sh` 부팅 로그 실측(KV cache/프롬프트토큰) 참고 —
    #    폐포 on(약 26k토큰)이면 약 3, off(약 5.5k토큰)면 더 갈 수 있다. 고정하지 않는다.
    ap.add_argument("--동시", type=int, default=1,
                    help="eval 문항 동시 처리 수. 1=순차(기존). RAW·INJECT 캡처는 "
                         "전역 상태라 --동시>1 과 같이 못 쓴다")
    a = ap.parse_args()

    # 🔴 2026-09-07(ai-33 실측 정정) — 스위치를 `감싸기()` 안에 두면 «안 걸린다».
    #    `감싸기()` 는 SUDDOE_EVAL_RAW=1 일 때만 불리는데, 평소 평가는 그걸 안 켠다.
    #    실측: SUDDOE_LLM=qwen 으로 20문항을 돌렸는데 `원출력.모델` 이 그대로
    #    `Qwen/Qwen3-32B-AWQ`(=GPU) 였다 — 스위치가 한 번도 실행되지 않았다.
    #    「환경변수를 줬다」 ≠ 「그 코드가 불린다」. 진입점 맨 앞으로 올린다.
    if not a.dry:
        from llm_qwen import 스위치_적용
        print(f"LLM 경로: {스위치_적용()}", flush=True)

    실행(dry=a.dry, limit=a.limit, 세트=a.세트, 라벨=a.라벨,
       top_k=a.top_k, 기록=not a.no_log, 변형=a.변형, 부분집합=a.부분집합,
       gold_ids=a.gold_ids, max_model_len=a.max_model_len, 스냅샷=a.스냅샷,
       폐포사용=(a.폐포 != "off"), 동시=a.동시)


if __name__ == "__main__":
    main()
