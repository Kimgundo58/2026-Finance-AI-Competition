# scripts/archive/ — 서빙에 안 쓰는 스크립트

여기 있는 파일은 **판정 서빙 경로(`server/main.py` 가 최상위·정적 top-level import
와 함수 내부 지연 import 를 전부 추적한 결과)에서 실행되지 않는다.** 죽은 코드가
아니라 오프라인 도구다 — 색인 구축, 크롤링, 평가, 시드, 일회성 CLI 가 원래 이 모양이다.

**지우지 않고 옮긴 이유**: 이 프로젝트는 "초록이 가린다" 사고가 반복돼서
되돌릴 수 있는 형태를 우선한다. `git mv` 로 옮겨 이력이 살아 있다.

## 분류

| 폴더 | 내용 |
|---|---|
| `indexing/` | Stage0·Stage2 색인 파이프라인, build_*, 별표/조문 후처리(`retag_*`, `tag_apply_target`, `extract_tables`, `backfill_doc_meta`) |
| `extraction/` | `convert_hwp.py` (HWP→PDF 수동 변환 산출 관리) |
| `crawling/` | 국가법령정보센터·행정규칙 크롤러 |
| `eval/` | 정답셋 채점, 검증, 감사 도구 (`eval_retrieval`·`eval_store` 는 여기 없다 — 서빙이 지연 import 로 쓴다) |
| `seed/` | DB 시드·픽스처 적재 |
| `cli/` | 오프라인 CLI 진입점(`judge_cli`·`judge_run`·`rehearsal` 등) |
| `agents/` | 일회성 작업 에이전트 스크립트 |
| `work/` | `scripts/_work/` 에 섞여 있던 진단용 1회성 스크립트 4개. **데이터 파일(`scripts/_work/*.json`·`*.jsonl`)은 그대로 `scripts/_work/`에 남아 있다** — 옮긴 건 `.py` 뿐이다 |

## 이관하며 한 것 (2026-09-05)

- 전부 `scripts/` 바로 밑에 있다고 가정한 `sys.path`/`ROOT` 계산이 깊어진 경로에서도
  살도록, 각 파일 맨 위에 `scripts/_lib` 을 찾을 때까지 위로 걸어 올라가는 부트스트랩을
  심었다. `archive/` 내부에서 카테고리를 넘나드는 import(예: `seed/load_db.py` →
  `eval/index_guard.py`)도 같이 해결한다
- 파일 안팎에서 옛 경로(`scripts/<이름>.py`)를 문자열로 참조하던 곳(문서 40여 곳,
  `.claude/hooks/*`, `.claude/_lanes.json`, `db/init/*.sql`, `server/*.py` 주석,
  일부 테스트의 에러 메시지)을 새 경로로 같이 고쳤다
- `pytest -q` 306 passed · 3 xfailed · 실패 0 (이관 전과 동일) 로 확인

## 이번 판에서 뺀 것

- `scripts/eval_e2e.py` — 다른 세션(ai-35)이 같은 시점에 병렬화 작업 중이라 충돌을
  피하려고 이번 판은 그대로 뒀다
- `l3_parse·l3_load·stage0_extract·stage0_articles·table_splice·hwpx_extract·
  docx_extract·hwp_extract·build_refs·pdftext·scope·eval_retrieval·eval_store` —
  처음엔 "안 쓰는 것"으로 분류했다가, `server/routes_l3.py:272` 의 함수 내부 지연
  import(`from l3_parse import 파싱`)를 따라가 보니 L3 업로드 파싱 경로에서 실제로
  쓰고 있어서 재분류했다. `scripts/` 에 그대로 있다

## 🔴 알려진 문제 — `.claude/hooks/guard_readonly_paths.py`

이 훅이 `archive/` 를 부분 문자열로 막는다(`"archive/" in norm`). 원래 의도는 저장소
최상위 `archive/`(이력)·`_골든셋/`·`_테스트_L3/`·`_범위밖_보류/` 보호인데, 시작 위치를
안 따져서 `scripts/archive/` 에 대한 Write/Edit 툴 호출도 같이 막힌다(Bash 로 옮기는
`git mv`는 안 걸린다 — 훅이 PreToolUse(Write|Edit|NotebookEdit) 전용이라). 패턴을
`norm.startswith("archive/")` 로 좁히는 게 맞아 보인다.
