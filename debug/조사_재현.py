# -*- coding: utf-8 -*-
"""② 품목 조사의 마지막 LLM 호출을 같은 프롬프트 · 스키마로 다시 보내 흔들림을 본다(2026-11-12, 11-15 키트 갱신).

디버그 실행이 멈춰 있을 때 「디버그 콘솔」 에 한 줄을 넣는다(키 · 서버 경유가 그 프로세스에만 있다):
    exec(open("debug/조사_재현.py", encoding="utf-8").read())

가장 최근 실행기록의 NN_품목조사/LLM.md 마지막 LLM 호출의 프롬프트 · 스키마를 읽어 다시 보낸다. 폴더 번호는 실행마다 다르다
(한 문장만 01_한문장정리 → 02_품목조사, 칸을 고치면 02_입력정리_폼 → 03_품목조사). 운영 배포 main efe9653 (PR #106 · #108 · #109 · #110 · #113 · #107 · #112 · #111 · #115 · #116) 기준.
조사는 계획 · 후보 작성 두 역할로 나뉘어 있다(web_research._후보역할: 스키마 칸이 후보 하나뿐이면 후보 작성).
본문을 읽은 조사의 마지막 호출은 대개 후보 작성이다.
    원본          그대로(재현 · 흔들림 확인)
    후보먼저      스키마 칸 순서만 바꿈: 후보 → 부족정보 → 행동 … (프롬프트 같음)
    공식빼기      프롬프트의 검색용어 「공식 서비스 소개 / official product service」 → 「서비스 소개」
    둘다          후보먼저 + 공식빼기
LLM 3회. DB 는 쓰지 않는다.
"""
import glob
import json
import os
import re
import sys
import types

# 터미널에서 단독 실행(PYTHONIOENCODING=utf-8 python debug/조사_재현.py): 키가 없으면 운영에 따로 붙는다.
# 디버그 실행과 동시에 돌 수 있게 터널 포트를 다르게 쓴다(DB 15436 · SOCKS 11082)
if not (os.environ.get("SUDDOE_LLM_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")):
    _루트 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(_루트)
    sys.path[:0] = [os.path.join(_루트, "debug"), os.path.join(_루트, "scripts"), _루트]
    import 설정 as _설정
    import 운영연결
    _따로 = types.SimpleNamespace(**{k: getattr(_설정, k) for k in dir(_설정) if not k.startswith("_")})
    _따로.DB포트, _따로.SOCKS포트 = 15436, 11082
    print("운영 연결:", {k: v for k, v in 운영연결.연결(_따로).items() if k != "코드고침"})

from llm_qwen import llm_호출, 기본_모델

# web_research._기본_계획호출 과 같은 모델(SUDDOE_RESEARCH_MODEL, 비어 있으면 기본 모델)로 보낸다
_모델 = os.environ.get("SUDDOE_RESEARCH_MODEL") or 기본_모델

_기록들 = sorted(glob.glob(os.path.join("debug", "실행기록", "*", "*_품목조사", "LLM.md")))
if not _기록들:
    raise SystemExit("debug/실행기록/*/NN_품목조사/LLM.md 가 없습니다. 판정_단계별.py 를 ② 품목 조사까지 먼저 돌리세요.")
_글 = open(_기록들[-1], encoding="utf-8").read()
_마지막 = _글.split("## LLM #")[-1]
_프롬프트 = re.search(r"### 요청 메시지 \(user\)\s*```text\n(.*?)\n```\s*\n", _마지막, re.S).group(1)
_스키마 = json.loads(re.search(r"### 응답 스키마\s*```json\n(.*?)\n```", _마지막, re.S).group(1))
print("기록:", _기록들[-1], "· 역할:", "후보 작성" if "행동" not in (_스키마.get("properties") or {}) else "계획")


def _후보먼저(s):
    순서 = ["후보", "부족정보", "행동", "유형", "검색어", "URL"]
    s = json.loads(json.dumps(s))
    s["properties"] = {k: s["properties"][k] for k in 순서 if k in s["properties"]}
    s["required"] = [k for k in 순서 if k in s["required"]]
    return s


def _공식빼기(p):
    return p.replace("공식 서비스 소개 / official product service", "서비스 소개")


_후보규칙 = ("제공된 본문이 있으면 먼저 후보를 채운다. 본문에 이 품목이 무엇을 제공하는지(예: 회의룸 · 공간 대여 · 구독 서비스) "
           "보이는 문장이 있으면 그 문장을 발췌해 제품성격 후보를 낸다. 후보를 낸 유형은 부족정보에 넣지 않는다.\n")


def _부족빼기(s):
    s = json.loads(json.dumps(s))
    s["properties"].pop("부족정보")
    s["required"].remove("부족정보")
    return s


# 1차(11-12): 원본 · 후보먼저 · 공식빼기 · 둘다 모두 8/8 후보 0개 → 2차: 「먼저 후보를 채운다」 문장을 맨 앞에 → 2/2 (web_research 반영)
# 3차(11-12): 출처마다 후보를 내 사용자에게 다 물었다 → 유형마다 하나만, 거래처(공개검색어)와 같은 업체 본문을 고르게
_하나만 = ("지출과 맞을 수 있는 본문이면 본문마다 후보를 하나씩 낸다.",
         "유형마다 후보는 하나만 낸다. 본문이 여럿이면 공개검색어(거래처)와 이름이 같은 업체 · 상품의 본문을 고르고, "
         "그런 본문이 없으면 지출과 가장 맞는 본문 하나를 고른다.")
# 4차(11-12): 유형마다 하나도 여럿일 수 있다 → 후보는 모두 합쳐 하나, 읽은 본문을 종합한 확인 문장(web_research 반영본과 같은 문구)
# 5차(11-12): 경량 모델 + 행동 고르기 규칙(web_research._계획_지시)으로 바뀌었다. 4차의 후보 문구는 지금 프롬프트에 들어 있어
# 옛 문구로 갈아 끼우지 않고 기록된 프롬프트를 그대로 다시 보낸다
# 6차(11-12): 계획 · 후보 작성 두 역할로 나눴고 모델은 기본 모델(qwen3.7-plus). 기록의 마지막 호출을 그대로 다시 보낸다
변형 = {
    "원본": (_프롬프트, _스키마, False),
}
결과 = {}
for 이름, (p, s, 추론) in 변형.items():
    for i in range(3):
        try:
            out = llm_호출(p, s, 모델=_모델, 재시도=0, 타임아웃=90, 추론=추론)[0]
            결과.setdefault(이름, []).append(out)
            print(f"[{이름} #{i + 1}] 행동={out.get('행동')} 부족={out.get('부족정보')} 후보={len(out.get('후보') or [])}개",
                  json.dumps([{k: x.get(k) for k in ("유형", "내용", "발췌", "URL")} for x in out.get("후보") or []], ensure_ascii=False)[:500])
        except Exception as e:  # noqa: BLE001
            print(f"[{이름} #{i + 1}] 실패 {type(e).__name__}: {str(e)[:120]}")
_저장 = os.path.join(os.path.dirname(_기록들[-1]), "조사_재현.json")
json.dump(결과, open(_저장, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("저장:", _저장)
