#!/usr/bin/env bash
# DB, 지출계획 첨부파일, 기관 L3 원본 일일 백업 (pg_dump -Fc, 볼륨 tar).
#   30 4 * * * /opt/suddoe/deploy/ncp/backup.sh >> /var/log/suddoe-backup.log 2>&1
#
# - 덤프는 최근 KEEP 개(기본 14), 각 파일 볼륨 tar 는 최근 KEEP_ATTACH/KEEP_L3 개(각 기본 2)를 보관한다.
# - 백업이 서버 디스크에만 있으면 서버 장애 시 함께 유실된다. 주기적으로 서버 외부(로컬 PC 등)로 복사한다.
# - 덤프에는 사용자 계정·기관 데이터가 포함된다. GitHub 이나 공유 드라이브에 올리지 않는다.
set -euo pipefail
cd "$(dirname "$0")"
# 덤프 파일을 소유자만 읽을 수 있게 만든다 (기본 umask 022 이면 644 로 생성된다)
umask 077

KEEP=${KEEP:-14}
KEEP_ATTACH=${KEEP_ATTACH:-2}
KEEP_L3=${KEEP_L3:-2}
mkdir -p backups
f="backups/suddoe_$(date +%Y%m%d_%H%M).dump"

# pg_dump 나 무결성 확인이 실패하면 .part 파일이 남는다. 보관 개수 정리는 *.dump 만 보므로
# 실패가 반복되면 부분 덤프가 디스크에 쌓인다. 종료 시 항상 지운다(mv 성공 후에는 없는 파일이다).
trap 'rm -f -- "$f.part"' EXIT

docker compose exec -T db pg_dump -U postgres -Fc --no-owner --no-privileges suddoe > "$f.part"
# 덤프 무결성 확인: 목차(TOC)를 읽을 수 없으면 실패 처리하고 파일을 남기지 않는다
docker compose exec -T db pg_restore --list < "$f.part" > /dev/null
mv "$f.part" "$f"

ls -1t backups/suddoe_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm --
echo "$(date '+%F %T') $f $(du -h "$f" | cut -f1)"

# 첨부파일 볼륨. 복원: gunzip -c 백업.tar.gz | docker compose exec -T api tar -C /app/_plan_attachments -xf -
a="backups/attachments_$(date +%Y%m%d_%H%M).tar.gz"
trap 'rm -f -- "$f.part" "$a.part"' EXIT
docker compose exec -T api tar -C /app/_plan_attachments -czf - . > "$a.part"
tar -tzf "$a.part" > /dev/null
mv "$a.part" "$a"
ls -1t backups/attachments_*.tar.gz | tail -n +"$((KEEP_ATTACH + 1))" | xargs -r rm --
echo "$(date '+%F %T') $a $(du -h "$a" | cut -f1)"

# 기관 L3 업로드 원본은 지출 첨부와 별도 볼륨이다. DB의 파일 참조와 함께 보존한다.
l="backups/l3_uploads_$(date +%Y%m%d_%H%M).tar.gz"
trap 'rm -f -- "$f.part" "$a.part" "$l.part"' EXIT
docker compose exec -T api tar -C /app/_l3_업로드 -czf - . > "$l.part"
tar -tzf "$l.part" > /dev/null
mv "$l.part" "$l"
ls -1t backups/l3_uploads_*.tar.gz | tail -n +"$((KEEP_L3 + 1))" | xargs -r rm --
echo "$(date '+%F %T') $l $(du -h "$l" | cut -f1)"
