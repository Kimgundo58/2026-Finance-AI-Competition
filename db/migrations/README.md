# DB 마이그레이션

운영 DB(NCP VM)에 적용할 스키마·데이터 변경을 날짜가 붙은 SQL 파일로 둔다.

- `db/init/` 은 빈 볼륨에서 컨테이너를 처음 만들 때만 실행된다. 이미 데이터가 있는 DB 에는 적용되지 않는다.
- 운영 DB 를 바꾸는 변경은 이 폴더에 `YYYY-MM-DD_<내용>.sql` 로 추가하고, 같은 커밋에서 `db/init/` 도 고쳐
  새로 만든 로컬 DB 가 운영과 같은 상태가 되게 한다.
- 각 파일은 멱등으로 작성한다. 다시 실행해도 같은 상태가 되어야 한다.
- 파일 상단 주석에 필수/선택 여부와 실행 명령을 적는다.

## 적용 방법 (운영, NCP VM)

테이블 소유자인 postgres 롤로 실행한다. 앱 롤 suddoe_app 은 DDL 권한이 없다.

```bash
cd /opt/suddoe/deploy/ncp
docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
  < ../../db/migrations/<파일명>.sql
```

적용 전에 `./backup.sh` 로 백업을 하나 만든다. 적용한 파일과 시각은 배포 기록에 남긴다.

## 로컬에서 검증

개발 DB 볼륨(`suddoe_pgdata`)에서 `docker compose down -v` 를 실행하지 않는다.
`db/docker-compose.yml` 은 컨테이너 이름·볼륨 이름·포트가 고정이라 `-p` 만 바꿔서는 겹친다.
init 과 마이그레이션은 이름과 포트가 다른 임시 컨테이너에서 확인한다(레포 루트에서 실행).

```bash
docker run -d --name suddoe-verify -p 55432:5432 \
  -e POSTGRES_PASSWORD=devpw -e POSTGRES_DB=suddoe -e POSTGRES_INITDB_ARGS=--locale=C.UTF-8 \
  -v "$PWD/db/init:/docker-entrypoint-initdb.d:ro" pgvector/pgvector:pg17
docker logs -f suddoe-verify          # 'PostgreSQL init process complete' 확인
docker exec -i suddoe-verify psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - < db/migrations/<파일명>.sql
docker rm -f suddoe-verify            # 익명 볼륨까지 지우려면 docker rm -fv
```

## 파일 목록

현재 `db/init/` 으로 새로 만든 DB 에는 아래 변경이 이미 반영돼 있으므로 적용하지 않는다.

| 파일 | 구분 | 내용 |
|---|---|---|
| `2026-11-01_decisions_강등코드_check.sql` | 필수 | `tenant.decisions` 의 검증코드 CHECK 를 당시 코드 목록과 맞춘다 (파일명은 이전 컬럼명 기준) |
| `2026-11-01_drop_ops_schema.sql` | 선택 | 사용하지 않는 `ops` 스키마(GPU 파드 주소 표)를 제거한다 |
| `2026-11-01_corpus_ingest.sql` | 필수 | 규정 적재 API 용 `corpus.ingest_jobs`·`staged` 상태·`suddoe_ingest` 롤을 만든다 (`-v ingest_password=...` 필요) |
| `2026-11-01_rules_사전승인조건_배열.sql` | 필수 | `corpus.rules.사전승인_조건` 을 `text` → `TEXT[]` 로 맞춘다(운영만 text 로 남아 판정 캐시의 데이터버전 해시 계산이 실패). 용어 정리 전 컬럼명 기준이라 `2026-11-02_용어_정리.sql` 보다 먼저 적용한다. 운영 적용 2026-11-01 23:27 |
| `2026-11-01_시퀀스_재설정.sql` | 필수 | 열 소유 시퀀스를 그 열의 최대값 이상으로 올린다(운영 `l3_articles`·`item_alias`·`chunks` 시퀀스가 뒤처져 L3 업로드가 UniqueViolation 으로 실패). 운영 적용 2026-11-01 23:36 |
| `2026-11-02_용어_정리.sql` | 필수 | 테이블·컬럼·제약·저장값·COMMENT 를 표준 용어로 바꾼다. `decisions_강등코드_check` 다음에 적용한다. 변경 전 행은 `backup_20261102` 스키마에 남으니 확인 후 지운다 |
| `2026-11-02_서버_별칭.sql` | 필수(데이터) | `item_alias` 의 '서버' → 기계장치를 지우고 '서버 임차' → 지급수수료(지침 제42조⑦ 원문)를 넣는다. 지운 행은 `backup_dq_20261102.item_alias` 에 남는다. 운영 적용 2026-11-02 02:37 (백업 `backups/suddoe_20261102_0237.dump`) |
| `2026-11-02_별칭_0906_보충.sql` | 필수(데이터) | `item_alias` 의 사업명 '초격차' 를 정식 사업명 '초격차 스타트업 프로젝트' 로 바꾼다(정확 조회 결과에 나오지 않던 행). 2026-09-06 별칭 중 운영에 빠진 26건을 원문 근거 칼럼(doc_id·조번호·원문발췌)과 함께 넣는다. 바꾸기 전 행은 `backup_dq_20261102.item_alias_초격차` 에 남는다 |
| `2026-11-02_룰473_사전심의.sql` | 보류 | 룰 473(모두의 창업 · 기계장치)의 한도 필드를 비운다. 측정에서 틀린 '가능'이 나와 적용하지 않는다. `-v apply=1` 없이 실행하면 아무것도 하지 않는다. 사유는 파일 머리말 |
| `2026-11-03_l3_단일문서.sql` | 필수 | `tenant.l3_documents."실패사유"` 컬럼을 추가하고, 기관마다 active 이고 pass/warn 인 L3 를 최신 1건만 남긴다(나머지는 `superseded`, 행·조문은 남김). 코드 배포 전에 적용한다(컬럼이 없으면 파싱 결과 기록이 실패한다) |
| `2026-11-03_l3_ocr.sql` | 필수 | `tenant.l3_documents.extraction` CHECK 에 `'ocr'` 을 더한다(스캔본을 CLOVA OCR 로 읽은 L3). 코드 배포 전에 적용한다(없으면 OCR 문서의 결과 기록이 실패한다) |
| `2026-11-03_계정별_분리.sql` | 필수 | 기업 데이터(`profile_agreements`·`expense_plans`·`plan_tasks`·`decisions`·`judge_cache`)에 `account_id` 를 넣고 RLS 를 `account_isolation`(org + 계정)으로 바꾼다. 기존 행은 기관의 가장 먼저 만든 계정(같으면 email 순)에 붙인다. **api 를 멈춘 상태에서 적용하고 새 이미지로 띄운다** — 이전 코드는 `app.account_id` 를 설정하지 않아 계정 행을 못 읽고, 새로 쓰는 행이 계정 없이 남는다. 순서는 아래 |
| `2026-11-03_계정별_분리_되돌리기.sql` | 되돌릴 때만 | 위 파일의 RLS 정책과 프로필 유일 키를 이전 정의로 되돌린다. 평소에는 실행하지 않는다 |
| `2026-11-03_주석_정정.sql` | 선택(주석만) | `corpus.refs` 의 COMMENT 두 개를 고친다. 참조 확장 깊이 "깊이 3" → "깊이 1"(실제 값은 `scripts/retrieve.py` 의 `깊이 = 1`), 해결상태의 "(판단불가 예고)" → "(판단불가가 될 수 있음)". 스키마·데이터는 바꾸지 않는다 |
| `2026-11-04_계정등록.sql` | 필수 | 가입한 계정을 기관에 등록하는 함수 `tenant.계정등록` 을 만든다(`POST /api/accounts`). 기존 행·정책은 바꾸지 않고 api 를 멈추지 않는다. 적용한 뒤 `./deploy.sh --force` 로 새 이미지를 띄운다. 프론트는 이 이미지가 뜬 뒤에 병합한다. 온보딩의 「다음」이 이 API 를 부르므로 먼저 병합하면 모든 사용자가 기관 선택 단계에서 멈춘다 |
| `2026-11-04_확인질문.sql` | 필수 | 판정 전 확인 질문용이다. `judge_cache` 종류에 `questions`, `decisions."사용자답변"`, 검증코드 `ANSWER_UNCONFIRMED` 를 더하고, `corpus.refs` 의 「지침 제40조」 5행을 고친다(4행은 14차 제43조 여비, 초격차 제10차 참고2 1행은 14차 제40조). 고칠 행이 5행도 0행도 아니면 멈춘다. 코드 배포 전에 적용하고 api 를 멈추지 않는다 |
| `2026-11-06_일정_유형.sql` | 필수 | `plan_tasks.유형` CHECK 에 사전승인·사전확인·증빙을 더한다 |
| `2026-11-06_집행줄.sql` | 필수 | `plan_tasks.집행예정일_연동` 을 더하고, 지출일이 있는 계획마다 집행 줄 1행을 채운다 |
| `2026-11-06_실제지출.sql` | 필수 | `expense_plans.실제지출일`·`실제지출액` |
| `2026-11-06_판정_피드백.sql` | 필수 | `tenant.decision_feedback`(맞았어요·틀렸어요) |
| `2026-11-06_계획_첨부.sql` | 필수 | `tenant.plan_attachments`. 새 이미지를 띄우기 전에 볼륨 `plan_attachments` 가 든 `deploy/ncp/docker-compose.yml` 도 반영한다 |
| `2026-11-06_L3_문서확인.sql` | 필수 | `파싱품질` 에 `review`, `l3_documents.검토결과`·`sha256`, `decisions.기준문서` |
| `2026-11-06_팀.sql` | 필수 | `tenant.teams`, `accounts.team_id`, `plan_tasks.작성_account_id`, 팀 함수. 로그인이 `tenant.로그인주체` 를 부르므로 새 이미지보다 먼저 적용한다. 되돌리는 SQL 은 파일 머리말에 있다 |
| `2026-11-06_온톨로지.sql` | 필수 | 룰 · 온톨로지 재설계용이다. `corpus.concepts`·`concept_labels`·`concept_relations`·`norms`·`provenance`, `tenant.l3_articles.concept_ids`, `decisions."경로"`, 검증코드 `ONTOLOGY_BAN`·`LIMIT_EXCEEDED`·`VAT_BASIS_UNKNOWN`, `suddoe_app` 읽기 · `suddoe_ingest` 쓰기 권한을 더한다. **코드 배포 전에 적용한다**(새 코드의 데이터 버전 해시가 새 테이블을 읽는다). 옛 코드는 영향이 없어 api 를 멈추지 않는다 |
| `2026-11-06_세부관리기준_시행일.sql` | 필수(데이터) | 모두의 창업 세부관리기준의 `corpus.documents.시행일` 을 부칙대로 2026-06-23 으로 채운다. 멱등이다. api 를 멈추지 않는다 |
| `2026-11-08_예비_시행일.sql` | 필수(데이터) | 예비창업패키지 세부관리기준(2025년)의 `corpus.documents.시행일` 을 부칙대로 2025-07-02 로 채운다. 멱등이다. api 를 멈추지 않는다. 로컬 `suddoe_ontology` 에만 적용했다 |
| `2026-11-08_초기_시행일.sql` | 필수(데이터) | 초기창업패키지 세부관리기준(2025년)의 `corpus.documents.시행일` 을 부칙대로 2025-07-02 로 채운다. 멱등이다. api 를 멈추지 않는다. 로컬 `suddoe_ontology` 에만 적용했다 |
| `2026-11-08_도약_시행일.sql` | 필수(데이터) | 창업도약패키지 세부관리기준(2025년)의 `corpus.documents.시행일` 을 마지막 부칙대로 2025-06-09 로 채운다. 멱등이다. api 를 멈추지 않는다. 로컬 `suddoe_ontology` 에만 적용했다 |
| `2026-11-08_창업중심대학_시행일.sql` | 필수(데이터) | 창업중심대학 세부관리기준2025년 개정의 `corpus.documents.시행일` 을 부칙 · 표지 개정 표기대로 2025-05-30 으로 채운다. 멱등이다. api 를 멈추지 않는다. 로컬 `suddoe_ontology` 에만 적용했다 |
| `2026-11-06_운영요령_참조정리.sql` | 필수(데이터) | 모두의 창업 · 지침 제14차의 운영요령 참조 41행을 superseded 복사본에서 active 문서로 옮긴다. 조번호가 active 에 없으면 멈춘다. superseded 행은 지우지 않는다. api 를 멈추지 않는다 |
| `2026-11-06_모두의창업_참조_태그_보정.sql` | 필수(데이터) | 모두의 창업의 쉼표 열거 참조 13행 추가, 제30조 재매핑 · 제41 · 43조 붙임 정정, 제46 · 50 · 52조 적용대상을 공통으로 고친다. 원문 문구와 도착 조제목이 다르면 멈춘다. refs 행 수가 바뀌어 판정 캐시가 무효화된다. api 를 멈추지 않는다 |
| `2026-11-07_적재형식_html.sql` | 필수 | `corpus.ingest_jobs` 파일형식 CHECK 에 `html` 을 더한다(법령 본문 페이지 원본). 적재 **전에** 적용한다. api 를 멈추지 않는다 |
| `2026-11-07_현행판_참조이동.sql` | 필수(데이터) | `ontology/수집/적재목록.json` 11건을 `corpus_restored_load.py --목록` 으로 적재한 **뒤에** 실행한다(일부만 active 면 멈추고, 문서가 하나도 없는 빈 DB 는 건너뛴다). 활성화가 옮기지 못한 나머지 옛 판 참조를 옮기고, 모두의 창업 제2조26 → 제3자 지침 제5 · 6조를 풀고(행을 못 찾으면 멈춘다), 운영요령 제19조 → 집행지침 문서 단위 행을 넣고, 판정 캐시를 비운다. 운영 순서는 `ontology/수집/P0-6_P0-7_기록.md`. api 를 멈추지 않는다 |
| `2026-11-07_L3연결초안.sql` | 필수 | L3 조문 연결 확인 화면용 칸: `l3_articles."연결초안"`(추천 개념 ID) · `"옛판문구"`(옛 판과 같은 문장), `l3_documents."연결초안상태"`(작성중 · 완료 · 일부실패 · 실패). 판정은 이 칸을 읽지 않는다. **코드 배포 전에 적용한다**(`GET /api/l3/current` 가 읽는다). 멱등, api 를 멈추지 않는다. 새 이미지가 뜬 뒤(그리고 비어 있던 `SUDDOE_ONTOLOGY_PROGRAMS` 를 켠 뒤) api 컨테이너에서 `python scripts/l3_link.py --backfill` 을 한 번 돌려 이미 적용 중인 문서의 추천 · 옛 판 문구를 만든다(`--목록만` 으로 대상 먼저 확인, LLM 은 문서마다 50조당 1회) |
| `2026-11-07_참조_보정.sql` | 필수(데이터) | 현행판_참조이동 **다음에** 실행한다. 가지조 참조를 가지조로 보내고, 제3자 지침 제17 · 19조의 다른 규정 붙임 · 별표를 제 문서로, 집행지침 · 사례집의 다른 지침 참조를 미해결로, 자기 문서에 없는 별표 · 참고를 실제 조나 미해결로 고친다. 안내 책자 version 을 판 표기 없음으로 고치고 판정 캐시를 비운다. api 를 멈추지 않는다 |
| `2026-11-07_용어풀이.sql` | 필수 | 용어 풀이 테이블 `corpus.terminology`(6-5) · 권한. 근거는 `provenance`(owner_kind `용어`). **코드 배포 전에 적용한다**(데이터 버전 해시가 이 테이블을 읽는다. 없으면 해시가 `unknown`). 적용 뒤 `python scripts/ontology_load.py` 로 `ontology/용어풀이.json` 을 넣는다. 멱등, api 를 멈추지 않는다 |
| `2026-11-08_자기부담_현물.sql` | 필수 | `profile_agreements."자기부담_현물"`(현물 총액, NULL = 미입력)을 되살린다. 마이페이지 협약 칸 · 판정 캐시키 · 판정 프롬프트 B5 가 읽는다. **코드 배포 전에 적용한다**(새 코드가 이 칸을 읽고 쓴다). 멱등, api 를 멈추지 않는다. 로컬 `suddoe` · `suddoe_ontology` 에 적용했다 |

`2026-11-06_일정_유형` 부터 `2026-11-06_온톨로지` 까지 8개는 위 순서대로 코드 배포 전에 적용한다(새 코드가 `decisions` 의 `기준문서` · `경로` · `온톨로지결과` 를 읽고 쓴다). api 를 멈추지 않는다. 적용한 뒤 `./deploy.sh --force`. 그 아래 파일은 각 행에 적은 때에 적용한다.

### 참조를 다시 만든 뒤

`scripts/build_refs.py` 로 `corpus.refs` 를 스캔해 다시 만들면(`_articles.json` 필요) 마이그레이션이 원문 대조로 고친 행이 사라진다. 코드로 옮기지 못한 행이 남아 있다. 다시 만든 뒤 아래 순서로 다시 돌린다. 모두 멱등이다.

`2026-11-04_확인질문.sql`(지침 제40조 5행) → `2026-11-06_운영요령_참조정리.sql` → `2026-11-06_모두의창업_참조_태그_보정.sql` → `2026-11-07_현행판_참조이동.sql` → `2026-11-07_참조_보정.sql`

`_refs.json` 은 그 뒤 `python scripts/build_refs.py --DB내보내기` 로 맞춘다. 적재 원천이 아니라 기록이다.

### `2026-11-03_계정별_분리.sql` 적용 순서 (운영)

병합하면 Actions 가 새 이미지를 올리고, 자동 배포는 이 파일 때문에 보류(hold)된다([8-8 CI/CD](../../docs/8_운영/8-8_CI_CD.md)).
Actions image 가 끝난 뒤 `tmux new -s migrate` 안에서 아래 블록을 통째로 실행한다(SSH 가 끊기면 `tmux attach -t migrate`). 어느 줄이든 실패하면 거기서 멈추고 다음 줄을 실행하지 않는다.
api 가 멈춘 동안은 요청이 실패한다. 절차가 끝날 때까지 다른 PR 을 병합하지 않는다(`deploy.sh` 는 `origin/main` 을 배포한다).

먼저 기존 행이 붙을 계정을 본다. 기관별 첫 계정이 의도와 다르면 실행하지 않는다.
`cd /opt/suddoe/deploy/ncp && docker compose exec -T db psql -U postgres -d suddoe -c "SELECT DISTINCT ON (org_id) org_id, email FROM tenant.accounts ORDER BY org_id, created_at, email"`

```bash
(
set -euo pipefail
cd /opt/suddoe
git pull --ff-only
IMG=suddoe.kr.ncr.ntruss.com/suddoe-api:$(git rev-parse HEAD)
docker pull -q "$IMG"                                                  # 이미지가 없으면 여기서 멈춘다
[ "$(df -BG --output=avail / | tail -1 | tr -dc 0-9)" -ge 3 ] || { echo "중단: 디스크 여유 3GB 미만"; exit 1; }   # deploy.sh 기준. 모자라면 api 를 띄우지 못한다
[ "$(docker image inspect -f '{{.Id}}' "$IMG")" != "$(docker image inspect -f '{{.Id}}' suddoe-api:prod)" ] || { echo "중단: 새 이미지가 실행 중인 이미지와 같다"; exit 1; }   # 같으면 deploy.sh 가 재시작하지 않는다
rm -f /etc/cron.d/suddoe-deploy                                        # 자동 배포를 끈다. 되돌린 뒤 다시 적용할 때는 이미 없다
flock /run/lock/suddoe-deploy.lock true                                # 실행 중인 --auto 가 끝나기를 기다린다
cd deploy/ncp
docker tag suddoe-api:prod suddoe-api:before-account-split             # 되돌릴 이미지
docker compose stop api
./backup.sh < /dev/null
docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
  < ../../db/migrations/2026-11-03_계정별_분리.sql                       # NOTICE: cfeba091 → prototype@ssudo.kr 확인
echo "--- 계정별 분리 적용 후 배포 $(date '+%F %T') ---" >> /var/log/suddoe-deploy.log || true
nohup ./deploy.sh --force >> /var/log/suddoe-deploy.log 2>&1 < /dev/null &
)
[ $? -eq 0 ] && tail -n 20 -f /var/log/suddoe-deploy.log               # 위 구분선 아래에 deployed <SHA> 가 나올 때까지
```

주의: 닫는 괄호 뒤에 `&&` 를 바로 붙이지 않는다. 그러면 bash 가 괄호 안의 `set -e` 를 무시해 실패해도 다음 줄로 넘어간다.

확인(health 200 은 DB 를 보지 않으므로 아래를 전부 본다). 끝나면 자동 배포를 다시 켠다.
- `cd /opt/suddoe/deploy/ncp && docker compose exec -T db psql -U postgres -d suddoe -c "SELECT account_id IS NULL, count(*) FROM tenant.expense_plans WHERE org_id='cfeba091-251a-5ae4-8cc9-88c6e6679440' GROUP BY 1"` 에 NULL 행이 없다.
- 실제 판정 1건과 협약 정보 저장 1건이 성공하고, prototype@ssudo.kr 로 로그인해 계획 목록이 그대로 보인다.
- `install -m 644 /opt/suddoe/deploy/ncp/suddoe-deploy.cron /etc/cron.d/suddoe-deploy`

실패했을 때(명령은 `cd /opt/suddoe/deploy/ncp` 에서 실행한다):
- SSH 가 끊겼다: `tmux attach -t migrate` 로 블록이 아직 도는지 먼저 본다. `--force` 는 잠금을 잡지 않아 두 번 돌면 안 된다.
- 블록이 `psql` 또는 그 앞에서 멈췄다: `docker compose exec -T db psql -U postgres -d suddoe -Atc "SELECT count(*) FROM pg_policies WHERE policyname='account_isolation'"` 로 적용 여부를 본다(`psql` 이 실패로 끝나도 커밋됐을 수 있다). 0 이면 적용되지 않았으니 `docker compose up -d --no-build api` 로 이전 이미지를 띄우고 자동 배포를 다시 켠 뒤 원인을 본다. 0 이 아니면 적용된 것이니 `./deploy.sh --force` 를 실행한다.
- 로그가 `skip`·`image ... 없음` 또는 `git fetch`·`docker tag` 오류로 끝났다: api 가 멈춰 있고 스키마는 이미 바뀌었다. 원인을 풀고 `./deploy.sh --force` 를 다시 실행한다. 풀 수 없으면 되돌린다.
- 로그가 `재시작하지 않았다` 또는 `docker compose up` 오류로 끝났다: `suddoe-api:prod` 는 이미 새 이미지라 `--force` 를 다시 실행해도 api 를 띄우지 않는다. `docker compose up -d --no-build api` 로 띄우고, healthy 가 되면 `./deploy.sh --force` 를 한 번 더 실행해 배포 기록(`deployed_sha`)을 남긴다. 이미지가 같아 재시작하지 않는다. 기록이 없으면 자동 배포를 켜도 뒤 커밋이 계속 hold 된다.
- 로그가 `unhealthy` 다: `deploy.sh` 가 이전 코드로 되돌려 이전 코드가 새 정책 위에서 돈다. 바로 되돌린다.

되돌리기(`restore.sh` 는 빈 DB 에만 복원하므로 덤프는 최후 수단이다). 자동 배포는 끈 채로 둔다. 켜 두면 다음 커밋이 hold 없이 새 코드를 되돌린 스키마 위에 올린다.

```bash
(
set -euo pipefail
rm -f /etc/cron.d/suddoe-deploy
flock /run/lock/suddoe-deploy.lock true
cd /opt/suddoe/deploy/ncp
# 두 계정이 같은 사업명으로 저장했으면 되돌리기 SQL 이 멈추므로 api 를 멈추기 전에 본다
dup=$(docker compose exec -T db psql -U postgres -d suddoe -Atc 'SELECT count(*) FROM (SELECT 1 FROM tenant.profile_agreements GROUP BY org_id, "사업명" HAVING count(*) > 1) d')
[ "$dup" = 0 ] || { echo "중단: 같은 사업명의 협약 행이 계정별로 나뉜 조합 ${dup}건. 남길 행을 정한 뒤 다시 실행한다"; exit 1; }
docker compose stop api
docker compose exec -T db psql -U postgres -d suddoe -v ON_ERROR_STOP=1 -f - \
  < ../../db/migrations/2026-11-03_계정별_분리_되돌리기.sql
docker tag suddoe-api:before-account-split suddoe-api:prod
docker compose up -d --no-build api
)
```

되돌린 뒤 `cd /opt/suddoe/deploy/ncp && docker compose ps api` 가 healthy 이고 prototype@ssudo.kr 의 계획 목록이 보이는지 확인한다.

되돌리기 블록이 `psql` 에서 멈추면 스키마는 새 정책 그대로다. `unhealthy` 뒤가 아니면 `suddoe-api:prod` 가 새 이미지이므로 `cd /opt/suddoe/deploy/ncp && docker compose up -d --no-build api` 로 다시 띄우고 원인을 본다. `unhealthy` 뒤에는 이전 이미지라 띄우면 이전 코드가 새 정책 위에서 돈다. 띄우지 말고 원인을 풀어 되돌리기를 다시 실행한다.

다시 적용할 때는 첫 블록을 처음부터 실행한다. 되돌린 동안 이전 코드가 쓴 행은 기관의 첫 계정에 배정된다. 두 번째 계정이 실제로 쓰고 있었다면 그 계정의 계획·판정이 첫 계정으로 옮겨지므로 먼저 행을 확인한다.

`item_alias`·`rules` 데이터는 `db/init` 시드가 없고 덤프로 옮긴다. 그래서 데이터 수정 마이그레이션은 `db/init` 을 고치지 않는다.

## seed

`db/seed/` 는 init 에서 실행하지 않는 데이터 보정 SQL 이다. 기관 목록(`tenant.orgs`)을 적재한 뒤 수동으로 실행한다.
