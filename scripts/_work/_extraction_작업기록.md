# extraction 재태깅 작업기록 (세션 A · 2026-08-31)

`corpus.documents.extraction` 283건 전부 `native` 였던 것을 실제 추출 경로에 맞게 재태깅했다.
산출물은 `scripts/retag_extraction.py` (멱등, dry-run 기본). 커밋하지 않았다.

## 결과

```
             전      후     index_target=true
native      283  →  255           228
dedupe        0  →    2             1
hancom        0  →   23             5
vlm           0  →    3             0
```

변경 28건. 손대지 않은 것: `chunks` 20,055 / `embedding` 20,055 /
`chunks.parse_quality='high'` 20,044 **(변화 0)** / `documents.parse_quality` high 260·low 23
**(변화 0)** / `retrieval_scope` NULL 0건 / `rules` 54건.

## 판정 근거 — 어떻게 세웠나

### dedupe (2건) — 실측. 하드코딩 아님

`pdftext.extract_meta()` 의 dedupe 플래그를 **매 실행 다시 잰다**.
판정식은 `dup_ratio(pages[min(4,n-1)]) > 0.35` — probe 가 한 쪽뿐이라
`max_pages=5` 로 잘라 불러도 **같은 쪽을 본다**. 4건 교차검증으로 확인했다
(max5 == full, 15.8s → 1.5s). 그래서 `extract()` 경유 규칙을 지키면서 10배 빠르다.

PDF 55건 전수 실측 결과가 **깨끗하게 갈렸다** — 중간값이 없다:

| doc | dup_ratio | index_target |
|---|---|---|
| `L1_중소기업창업_지원사업_통합관리지침_제14차개정_20251223` | **0.500** | true |
| `창업도약패키지 지원사업 세부관리기준(2022년)` | **0.507** | false |
| 나머지 53건 | ≤ 0.017 | |

🔴 `CLAUDE.md` 는 중복 3건이라 적어뒀지만 **DB 기준으로는 2건이 맞다.**
세 번째(`L3_2025초기창업패키지_주요질의응답집_별첨4`)는 골든셋이라 인덱스 투입 금지 대상이고
`corpus.documents` 에 애초에 없다. 문서가 틀린 게 아니라 세는 모집단이 다르다.

전수 실측값은 `scripts/_work/_extraction_probe.json` (프로브: `_probe_extraction.py`).

### hancom (23건) — 경로 + 원본 존재 이중 확인

`_hwp변환/` 아래 = 한컴오피스 1회 수동 변환분 (`convert_hwp.py` 가 원본 트리를 미러링).
**경로 접두사만 믿지 않고** `_hwp변환/<원본경로>` 옆에 같은 이름의 HWP 가 실제로 있는지 봤다.
**23/23 발견** (.hwp 22 + .hwpx 1 — 초격차 제10차). 미발견 0건.

### vlm (3건) — 스캔 대장 등재

`_scan_inventory.json` 45건 중 `corpus.documents` 에 있는 것은 3건뿐이다
(나머지 42건은 PMS 매뉴얼·별표 등 미적재분).

| doc | 등급 | 쪽 | 표본 자/쪽 | index_target |
|---|---|---|---|---|
| `2026년 재도전성공패키지 세부관리기준(11차 개정)` | A | 9 | 5.0 | false |
| `창업사업화 지원사업 부정행위 방지 사례집` | B | 62 | 0.0 | false |
| `[주관기관]전자협약~사업비집행_v.1.2` | 기타 | 78 | 46.0 | false |

## 🔴 남는 것 — 이 3건에 VLM 판독본은 아직 없다

저장된 본문과 native 재추출을 대조했다 (2026-08-31 실측):

```
[주관기관]전자협약~사업비집행_v.1.2        저장 3,370자 == native 재추출 3,370자 (앞 200자 동일)
창업사업화 지원사업 부정행위 방지 사례집    저장 0자 / native 추출은 개행 61자뿐
2026년 재도전성공패키지 세부관리기준(11차)  저장 0자 / native 추출은 쪽번호("- 1 -"…) 53자뿐
```

즉 `extraction='vlm'` 은 **"판독했다"는 기록이 아니라 "판독 없이는 인용 금지"라는 가드**다.
그래도 vlm 으로 간 이유: `native` 는 스키마상 **A등급 인용 가능**을 뜻해서, 9쪽 스캔본에서
쪽번호만 긁히는 문서에 붙기에는 명백히 위험한 값이다. vlm 은 신뢰를 내리는 방향으로만
틀린다. 셋 다 `index_target=false` · 청크 0개라 실동작 변화는 없다.

**실제 판독을 하게 되면** `retag_extraction.py` 판정 규칙 1번 주석과
`_scan_inventory.json` 의 A등급 방침을 같이 갱신해야 한다.

## parse_quality — 게이트는 걸었고, 결과는 0

vlm 문서에 `parse_quality='low'` 를 거는 작업인데, 판정 검색 필터가 `parse_quality='high'`
라 잘못 내리면 청크가 검색에서 조용히 빠진다. 그래서 쓰기 전에
"지금 high 인데 low 로 내려갈 청크" 를 세고 **0이 아니면 아무것도 쓰지 않고 중단**하도록
스크립트에 게이트를 박았다 (`SystemExit(2)`).

실측 **0개** — 3건 모두 이미 `documents.parse_quality='low'` 였고 청크가 0개다.
그래서 `documents` 0건 / `chunks` 0건 갱신, `chunks high` 20,044 그대로.

`chunks` 쪽 UPDATE 문은 no-op 이지만 남겨뒀다. 나중에 vlm 문서가 인덱싱되면
사본도 같이 내려가야 하고 그때 이 스크립트가 유일한 갱신 경로가 된다.

## 확인한 전제

- `corpus.chunks` 에 `extraction` 컬럼 없음 → `documents` 만 갱신 (확인)
- `documents.extraction` CHECK 는 4종 `('native','dedupe','hancom','vlm')`.
  `hwpx`/`hwp` 는 `01_schema.sql:369` 쪽(L3 업로드 전용)이라 여기서는 나올 수 없다
- 우선순위 vlm > dedupe > hancom > native 로 짰지만 **실제 충돌 0건**
  (스캔 3건은 `_hwp변환/` 밖, dedupe 2건도 밖). 순서는 앞으로를 위한 규칙일 뿐이다
- 적재기 `load_db.py` 는 `extraction` 을 계산하지 않고 넘긴다 →
  **재적재하면 다시 전부 `native` 로 돌아간다.** 근본 수정은 `load_db.py`/`stage0_ingest.py`
  가 이 판정을 태우는 것인데, 이번 세션 범위 밖이라 손대지 않았다 (미결)

## 건드리지 않은 것

`corpus.rules` · `seed_rules.py` · `retrieval_scope`(documents/chunks) · `chunks.text` ·
`embedding` · `corpus.refs` · `doc_articles` · `db/init/*.sql` · `check_items` ·
`evidence_sources` · `tenant.*` · 설계 문서 전반. 커밋 안 함.

## 산출물

| 파일 | 무엇 |
|---|---|
| `scripts/retag_extraction.py` | 재태깅 본체. dry-run 기본, `--apply` 로 쓰기. 멱등 |
| `scripts/_work/_probe_extraction.py` | dedupe 전수 실측 프로브 (1회용) |
| `scripts/_work/_extraction_probe.json` | PDF 57건 dup_ratio·쪽수 실측값 |
