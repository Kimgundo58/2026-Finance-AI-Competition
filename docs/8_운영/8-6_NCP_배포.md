# 8-6 NCP 배포 (GCP → 네이버클라우드 마이그레이션)

네이버클라우드 VM 1대에 API · PostgreSQL · Caddy 를 Docker Compose 로 운영한다.
배포 파일은 `deploy/ncp/` 에 있다. GCP(Cloud Run + Cloud SQL) 절차는 [[8-5_배포_레시피]].

## 구성

```
Vercel(프론트) ──HTTPS──▶ NCP VM (Ubuntu, Standard-g3 2vCPU/8GB)
                            ├─ caddy  :80/:443  리버스 프록시 + Let's Encrypt
                            ├─ api    :8080     레포 루트 Dockerfile (외부 미노출)
                            └─ db     :5432     pgvector/pgvector:pg17 (외부 미노출)
외부 서비스: Supabase(인증 JWKS), DashScope(LLM)
```

| GCP | NCP | 비고 |
|---|---|---|
| Cloud Run (min 1, 2vCPU/8Gi) | VM 위 `api` 컨테이너 | 단일 인스턴스. 오토스케일 없음 |
| Cloud SQL PG17 | VM 위 `db` 컨테이너 | NCP Cloud DB 는 pgvector 를 `cdb_admin` 스키마에만 설치 → 덤프의 `extensions.vector` 와 불일치 |
| Secret Manager | `deploy/ncp/.env` (chmod 600) | |
| Cloud Run 기본 HTTPS | Caddy + 도메인 | 프론트가 HTTPS 라 API 도 HTTPS 필수 |
| 자동 백업 (꺼져 있었음, 0개) | `backup.sh` cron 매일 04:30, 14개 보관 | |

## 예상 비용 (2026-10-27 NCP 요금표, VAT 별도)

VM 82,240원 + SSD 50GB 약 5,800원 + 공인 IP 4,032원 = **월 약 92,000원** (VAT 포함 약 10.1만 원).
4vCPU/16GB 는 VAT 포함 월 약 20만 원. 신규 가입 크레딧 10만 원(3개월, 메인 계정 결제수단 등록 후 신청).

## 1. 결제 전 (로컬)

1. 로컬 리허설: `deploy/ncp/.env` 생성(도메인 `localhost`) → `./restore.sh <dump> <rowcounts>` → `docker compose up -d --build`
2. DashScope API 키 확보 (GCP Secret Manager 는 결제 중지로 조회 불가)
3. `deploy/ncp/` 를 GitHub main 에 push (서버가 clone 한다)

## 2. NCP 콘솔

1. 메인 계정 결제수단 등록 → 서브 계정 정책 `NCP_INFRA_MANAGER` 이상
2. VPC · Public Subnet 생성
3. ACG 인바운드: TCP 22 (관리자 IP 만), TCP 80 · 443 (0.0.0.0/0)
4. 인증키(.pem) 생성 → 서버 생성: Ubuntu 22.04/24.04, Standard-g3 2vCPU/8GB, SSD 50GB, 공인 IP 할당
5. 콘솔에서 관리자 비밀번호 확인(.pem 필요) → `ssh root@<공인IP>`

## 3. 서버

```bash
curl -fsSL https://raw.githubusercontent.com/Kimgundo58/2026-Finance-AI-Competition/main/deploy/ncp/setup_server.sh | bash
vi /opt/suddoe/deploy/ncp/.env          # 시크릿: openssl rand -hex 32
# 로컬 PC 에서 덤프 업로드
scp suddoe.dump source_rowcounts.txt root@<공인IP>:/opt/suddoe/deploy/ncp/backups/
cd /opt/suddoe/deploy/ncp
./restore.sh backups/suddoe.dump backups/source_rowcounts.txt   # 모든 항목이 [ OK ] 여야 진행
docker compose up -d --build
```

`SUDDOE_DOMAIN` 은 정식 도메인 전까지 `<공인IP 점을 하이픈으로>.sslip.io` 를 쓴다.

## 4. 배포 후 확인

```bash
curl -s https://$SUDDOE_DOMAIN/api/health                     # "모드":"live"
curl -s -X POST https://$SUDDOE_DOMAIN/admin/warmup -H "X-Admin-Token: $SUDDOE_ADMIN_TOKEN"
docker compose ps                                              # 3개 모두 healthy/running
```

그다음 프론트에서 판정 1건을 실행한다. health 200 만으로는 판정 정상 여부를 알 수 없다.

## 5. 도메인 변경

1. DNS A 레코드 → 서버 공인 IP
2. `.env` 의 `SUDDOE_DOMAIN` 수정 → `docker compose up -d caddy` (인증서 자동 발급)
3. Vercel 환경변수 `NEXT_PUBLIC_API_BASE` 변경 후 재배포 (빌드 시점에 번들에 포함되는 값)

`SUDDOE_CORS` 는 프론트 origin 이라 API 도메인 변경과 무관하다.

## 6. 운영

| 작업 | 명령 |
|---|---|
| 코드 업데이트 배포 | `git pull && docker compose up -d --build api` |
| 로그 | `docker compose logs -f api` (json-file, 20MB × 5 로테이션) |
| 수동 백업 | `./backup.sh` → `backups/` |
| 백업 외부 보관 | 로컬 PC 에서 `scp root@<IP>:/opt/suddoe/deploy/ncp/backups/*.dump .` 주기적으로 |
| 재시작 | `docker compose restart api` (서버 재부팅 시 `restart: unless-stopped` 로 자동 기동) |

## 주의

- `SUDDOE_DEMO_SECRET` · `SUDDOE_SLUG_SECRET` 은 GCP 운영에서 미설정이었다(개발용 기본값 사용). 새 값으로 바꾸면 기관 slug 가 전부 바뀐다.
- `postgres` 롤은 이 구성에서 superuser 다. 앱 DSN 은 반드시 `suddoe_app` 을 쓴다(compose 에 고정).
- `db/docker-compose.yml`(개발용)은 5432 와 pgweb 을 외부에 노출한다. 서버에서 쓰지 않는다.
- Document AI(L3 스캔 OCR)는 GCP 전용이라 이관하지 않는다. 기존 운영에서도 비활성이었다.
- 덤프는 `--no-privileges` 라 권한이 복원되지 않는다. 권한은 `10b_cloudsql_grant.sql` 로 다시 설정하며,
  2026-10-27 리허설에서 운영 DB 의 GRANT/REVOKE 50줄과 일치함을 확인했다 (`extensions` 스키마 USAGE 누락을 이때 발견해 추가).
- 로컬 리허설 결과 (2026-10-27, Docker Desktop): 이미지 콜드 빌드 16분, 복원+검증 22초, API healthy 20초,
  HTTPS·CORS·관리자 토큰·데모 세션·검색 정상, DashScope 키 없이 판정 시 판단불가로 종료.
- 미검증: `setup_server.sh` 를 NCP 이미지에서 실행한 적 없음. 2vCPU/8GB 에서 동시 판정 4건 부하는 측정하지 않음.
