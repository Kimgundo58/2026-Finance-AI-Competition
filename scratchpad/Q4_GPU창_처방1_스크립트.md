# Q4 — GPU 창 ③ 무인 스크립트 (처방① 출력토큰·finish_reason·사고흔적 실측)

central(ai-c4) 지시: 창이 열리면 인자만 받고 15분 안에 무인으로 끝나야 한다. 대화형 판단 없음.
새 스크립트를 만들지 않고 **기존 `scripts/eval_e2e.py` CLI 를 그대로 쓴다** — Q3 소유 파일이라
재구현하지 않고 기존 하네스를 호출만 한다.

## 문항 선정 — 창업중심대학 4 + 긴 프롬프트 상위 5 (합 9건)

- 창업중심대학 4: `421,437,438,439` (오늘 데모 org 의 실제 사업)
- 긴 프롬프트 상위 5 (`docs/기록/_레인_P3.md` §1 dry 실측 기준 — 다시 안 잰다):
  `377`(44,926자·최장) `366`(+2,164) `370`(+1,832) `365`(+1,780) `355`(+1,763)

## ① 판정 호출 — 실전(LLM 호출), eval.runs 에 기록

```
export PYTHONIOENCODING=utf-8
python scripts/eval_e2e.py \
  --gold-ids 421,437,438,439,377,366,370,365,355 \
  --top-k 5 --변형 V0 --max-model-len 40960 \
  --라벨 "Q4_처방1_출력토큰실측_0904"
```

- `--dry` 를 **안 준다** — 실제 LLM 호출이 목적이다
- `--no-log` 를 **안 준다** — `eval.runs`/`eval.run_items` 에 남아야 사후 분석이 된다
- `기록=False` 로 `orchestrate.판정()` 을 부르므로 `tenant.decisions` 에는 안 남는다
  (eval 하네스 자체 설계 — `eval_e2e.py:540`). GPU 비용에 영향 없다
- 9문항 · 문항당 판정LLM p50 15.4초 실측 기준 추정 **소요 ~3분** (통신 지연 별도)
- 🔴 adapter.py 수정(사고흔적_걷기)이 이미 반영돼 있어야 이 창이 의미가 있다 —
  central 승인·머지·배포 후에 이 창을 연다

## ② 사후 분석 — GPU 없이, DB 만 읽는다

```python
# scratchpad 에서 실행: python scratchpad/Q4_사후분석.py <run_id>
import sys, json, psycopg
run_id = int(sys.argv[1])
conn = psycopg.connect('postgresql://postgres:devpw@localhost:5432/suddoe')
cur = conn.cursor()
cur.execute("select item_id, 원출력->'모델' from eval.run_items where run_id=%s", (run_id,))
rows = cur.fetchall()
사고있음 = [r for r in rows if (r[1] or {}).get('사고흔적있음')]
종료이유분포 = {}
토큰합 = {"prompt": 0, "completion": 0}
for _, m in rows:
    m = m or {}
    종료이유 = m.get('종료이유') or '(없음)'
    종료이유분포[종료이유] = 종료이유분포.get(종료이유, 0) + 1
    토큰 = m.get('토큰') or {}
    토큰합["prompt"] += 토큰.get('prompt_tokens', 0) or 0
    토큰합["completion"] += 토큰.get('completion_tokens', 0) or 0
print(f"총 {len(rows)}건 · 사고흔적 발화 {len(사고있음)}건 ({len(사고있음)/len(rows)*100:.0f}%)")
print("종료이유 분포:", 종료이유분포)
print("토큰 합계:", 토큰합)
for iid, m in rows:
    m = m or {}
    if m.get('사고흔적있음'):
        print(f"  item {iid}: 사고흔적길이={m['사고흔적길이']}자 · 종료이유={m.get('종료이유')}")
    if m.get('종료이유') == 'length':
        print(f"  🔴 item {iid}: finish_reason=length — 「모델이 모른다」가 아니라 「자리를 안 줬다」")
```

이 둘을 이으면 창이 열린 뒤 **명령 2개, 무인, ~5분**으로 처방①의 답(출력토큰 실측·
finish_reason 분포·사고흔적 발화율)이 다 나온다. 대화형 판단 지점 없음.

## 자가검토 반영

- `finish_reason=='length'` 는 별도 표시 — "모델이 모른다"(정상 판단불가)와
  "자리를 안 줬다"(잘림·실패경로)를 섞지 않는다(a70c874 가 고친 것을 다시 안 잃는다)
- 사고흔적있음/길이는 adapter.py 수정으로 이제 모든 판정 호출에 항상 실린다
  (`eval_e2e.py:584` 가 `모델` 키를 플래그 무관하게 항상 남기므로 추가 배선 불필요)
