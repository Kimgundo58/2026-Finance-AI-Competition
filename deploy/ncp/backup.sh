#!/usr/bin/env bash
# DB 일일 백업 (pg_dump -Fc). setup_server.sh 가 cron 에 등록한다.
#   30 4 * * * /opt/suddoe/deploy/ncp/backup.sh >> /var/log/suddoe-backup.log 2>&1
#
# - 최근 KEEP 개(기본 14)만 보관하고 나머지는 삭제한다.
# - 백업이 서버 디스크에만 있으면 서버 장애 시 함께 유실된다. 주기적으로 서버 외부(로컬 PC 등)로 복사한다.
# - 덤프에는 사용자 계정·기관 데이터가 포함된다. GitHub 이나 공유 드라이브에 올리지 않는다.
set -euo pipefail
cd "$(dirname "$0")"
# 덤프 파일을 소유자만 읽을 수 있게 만든다 (기본 umask 022 이면 644 로 생성된다)
umask 077

KEEP=${KEEP:-14}
mkdir -p backups
f="backups/suddoe_$(date +%Y%m%d_%H%M).dump"

docker compose exec -T db pg_dump -U postgres -Fc --no-owner --no-privileges suddoe > "$f.part"
# 덤프 무결성 확인: 목차(TOC)를 읽을 수 없으면 실패 처리하고 파일을 남기지 않는다
docker compose exec -T db pg_restore --list < "$f.part" > /dev/null
mv "$f.part" "$f"

ls -1t backups/suddoe_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm --
echo "$(date '+%F %T') $f $(du -h "$f" | cut -f1)"
