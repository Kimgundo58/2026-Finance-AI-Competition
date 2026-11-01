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
| `2026-11-01_rules_사전승인조건_배열.sql` | 필수 | `corpus.rules.사전승인_조건` 을 `text` → `TEXT[]` 로 맞춘다(운영만 text 로 남아 판정 캐시 설정 해시가 실패). 용어 정리 전 컬럼명 기준이라 `2026-11-02_용어_정리.sql` 보다 먼저 적용한다. 운영 적용 2026-11-01 23:27 |
| `2026-11-01_시퀀스_재설정.sql` | 필수 | 열 소유 시퀀스를 그 열의 최대값 이상으로 올린다(운영 `l3_articles`·`item_alias`·`chunks` 시퀀스가 뒤처져 L3 업로드가 UniqueViolation 으로 실패). 운영 적용 2026-11-01 23:36 |
| `2026-11-02_용어_정리.sql` | 필수 | 테이블·컬럼·제약·저장값·COMMENT 를 표준 용어로 바꾼다. `decisions_강등코드_check` 다음에 적용한다. 변경 전 행은 `backup_20261102` 스키마에 남으니 확인 후 지운다 |
| `2026-11-02_서버_별칭.sql` | 필수(데이터) | `item_alias` 의 '서버' → 기계장치를 지우고 '서버 임차' → 지급수수료(지침 제42조⑦ 원문)를 넣는다. 지운 행은 `backup_dq_20261102.item_alias` 에 남는다. 운영 적용 2026-11-02 02:37 (백업 `backups/suddoe_20261102_0237.dump`) |
| `2026-11-02_룰473_사전심의.sql` | 보류 | 룰 473(모두의 창업 · 기계장치)의 한도 필드를 비운다. 측정에서 틀린 '가능'이 나와 적용하지 않는다. `-v apply=1` 없이 실행하면 아무것도 하지 않는다. 사유는 파일 머리말 |

`item_alias`·`rules` 데이터는 `db/init` 시드가 없고 덤프로 옮긴다. 그래서 데이터 수정 마이그레이션은 `db/init` 을 고치지 않는다.

## seed

`db/seed/` 는 init 에서 실행하지 않는 데이터 보정 SQL 이다. 기관 목록(`tenant.orgs`)을 적재한 뒤 수동으로 실행한다.
