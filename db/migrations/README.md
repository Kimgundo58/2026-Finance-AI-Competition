# DB 마이그레이션

운영 DB(NCP VM)에 적용할 스키마·데이터 변경을 날짜가 붙은 SQL 파일로 둔다.

- `db/init/` 은 빈 볼륨에서 컨테이너를 처음 만들 때만 실행된다. 이미 데이터가 있는 DB 에는 적용되지 않는다.
- 운영 DB 를 바꾸는 변경은 이 폴더에 `YYYY-MM-DD_<내용>.sql` 로 추가하고, 같은 커밋에서 `db/init/` 도 고쳐
  새로 만든 로컬 DB 가 운영과 같은 상태가 되게 한다.
- 각 파일은 멱등으로 작성한다. 다시 실행해도 같은 상태가 되어야 한다.
- 파일 머리 주석에 필수/선택 여부와 실행 명령을 적는다.

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

| 파일 | 구분 | 내용 |
|---|---|---|
| `2026-11-01_decisions_강등코드_check.sql` | 필수 | `tenant.decisions` 강등코드 CHECK 를 `llm_validate.강등코드_전체` 와 맞춘다 |
| `2026-11-01_drop_ops_schema.sql` | 선택 | 사용하지 않는 `ops` 스키마(GPU 팟 주소 표)를 제거한다 |

## seed

`db/seed/` 는 init 에서 실행하지 않는 데이터 보정 SQL 이다. 기관 명부(`tenant.orgs`)를 적재한 뒤 수동으로 실행한다.
