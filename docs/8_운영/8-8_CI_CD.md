# 8-8 CI/CD

PR 검사와 이미지 빌드는 GitHub Actions, 이미지 보관은 네이버클라우드 Container Registry,
배포는 운영 서버의 cron 이 한다. 서버 구성과 수동 절차는 [8-6 NCP 배포](8-6_NCP_배포.md).

## 흐름

```
PR          ─▶ ci.yml     문법 검사 · server.selftest(SUDDOE_MOCK=1) · rule_lookup self-test (DB·LLM·키 없음)
main push   ─▶ ci.yml + image.yml ─▶ suddoe.kr.ncr.ntruss.com/suddoe-api:<커밋 SHA>, :main
운영 서버   ─▶ cron 5분마다 deploy.sh --auto
               새 커밋 이미지 받기 → suddoe-api:prod 태그 → api 재생성 → healthy 확인(실패하면 되돌림)
```

이미지 빌드는 캐시가 맞으면 수 분, 캐시가 없으면 20~30분 걸린다. 병합 후 운영 반영까지 보통 10~35분이다.
문서만 바뀐 커밋은 이미지가 같아 재시작하지 않는다.

## 구성

| 항목 | 값 |
|---|---|
| 레지스트리 | `suddoe` (비공개, 저장소 유형 Ncloud Storage. Object Storage 이용 신청 없이 만들었다). 공개 주소 `suddoe.kr.ncr.ntruss.com` |
| 빌드 캐시 | 같은 레지스트리의 `suddoe-api:buildcache` (mode=max). 레이어 digest 가 유지돼 코드 레이어만 오간다 |
| push 키 | 서브 계정 `suddoe-ci-push` (NCP_CONTAINER_REGISTRY_MANAGER, 콘솔 접속 없음). GitHub secrets `NCR_PUSH_ACCESS_KEY` · `NCR_PUSH_SECRET_KEY` |
| pull 키 | 서브 계정 `suddoe-server-pull` (NCP_CONTAINER_REGISTRY_VIEWER). 서버 `/root/.docker/config.json` |
| 키 로컬 사본 | 레포 루트 `.env.ncloud` 의 `NCR_PUSH_*` · `NCR_PULL_*` (git 제외) |
| 배포 상태 | 서버 `/var/lib/suddoe/` — `deployed_sha` · `blocked_sha` · `failed_sha` · `pending` |
| 로그 | 서버 `/var/log/suddoe-deploy.log` (배포·보류·되돌림이 있을 때만 쓴다) |

`NCP_CONTAINER_REGISTRY_MANAGER` 에는 Object Storage 관리 권한도 들어 있다. Object Storage 를 쓰게 되면
push 키 권한을 레지스트리 하나로 좁힌 정책으로 바꾼다.

## 자동 배포가 멈추는 경우

배포된 커밋 이후 `db/migrations/` 나 `deploy/ncp/` 가 바뀌면 deploy.sh 는 배포하지 않고 로그에 `hold` 를 남긴다.
코드보다 스키마·설정이 먼저 바뀌어야 하기 때문이다.

1. 서버에서 `cd /opt/suddoe && git pull`
2. 마이그레이션 적용([db/migrations/README.md](../../db/migrations/README.md)), compose·`.env` 변경 반영
3. `cd deploy/ncp && ./deploy.sh --force`

디스크 여유가 8GB 보다 적으면 배포하지 않는다. 지난 `rollback-*` 이미지를 지워 공간을 만든다.

## 되돌리기

- 자동: 새 컨테이너가 420초 안에 healthy 가 되지 않으면 직전 이미지(`suddoe-api:rollback-auto`)로 되돌린다.
  그 커밋은 `failed_sha` 에 남아 다시 시도하지 않는다. 다음 커밋이 오거나 `--force` 로 배포한다.
- 손으로(직전 이미지): `docker tag suddoe-api:rollback-auto suddoe-api:prod` 후 `docker compose up -d --no-build api`
- 특정 커밋: `docker pull suddoe.kr.ncr.ntruss.com/suddoe-api:<SHA>` 후 그 이미지에 `suddoe-api:prod` 태그를 붙이고 같은 명령
- 자동 배포를 잠시 멈추기: `/etc/cron.d/suddoe-deploy` 의 마지막 줄을 주석 처리

health 200 은 판정이 정상이라는 뜻이 아니다. 배포 로그를 본 뒤 프론트에서 판정 1건을 실행한다.

## 서버 설치 (한 번)

1. pull 키로 로그인: `docker login suddoe.kr.ncr.ntruss.com -u <NCR_PULL_ACCESS_KEY> --password-stdin` (secret 은 표준 입력으로)
2. cron 설치: `install -m 644 deploy/ncp/suddoe-deploy.cron /etc/cron.d/suddoe-deploy`
3. 첫 배포: `cd /opt/suddoe/deploy/ncp && ./deploy.sh --force`

## 비용 (2026-11-03)

- GitHub Actions: 공개 레포라 무료. 레포를 비공개로 바꾸면 월 2,000분 무료 뒤 과금된다.
- Container Registry: 관리 비용은 없고 저장 용량·요청·전송량만 과금된다. 캐시 포함 약 13GB 로 예상한다.
  요금 안내의 Standard 예시 단가(GB-월 28원)를 쓰면 월 약 400원이다. 실제 단가는 콘솔 요금 안내에서 확인한다.

## 아직 CI 에 없는 검사

`scripts/ux_flow_check.py` 와 `orchestrate.py --fault` 는 DB 가 필요해 CI 에서 돌지 않는다.
레포만으로 DB 를 만들 수 있어야 한다. 지금은 사람이 검토한 룰이 운영·로컬 DB 에만 있다.
