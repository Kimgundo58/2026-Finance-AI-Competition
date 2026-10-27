#!/usr/bin/env bash
# 신규 Ubuntu 서버(네이버클라우드 VM) 초기 설정. root 로 1회 실행한다. 재실행해도 결과는 같다(idempotent).
#   - Docker / Docker Compose 설치
#   - 스왑 4GB
#   - 레포 clone, .env 생성
#   - 일일 백업 cron 등록
#
# 전제: deploy/ncp 가 GitHub 원격 저장소(main)에 push 되어 있어야 한다.
# 주의: NCP 이미지에서 아직 실행해 보지 않았다. Ubuntu 22.04/24.04 패키지 이름 기준이다.
set -euo pipefail

REPO=${REPO:-https://github.com/Kimgundo58/2026-Finance-AI-Competition.git}
DIR=${DIR:-/opt/suddoe}

timedatectl set-timezone Asia/Seoul

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 docker-buildx git
systemctl enable --now docker
docker compose version

# 스왑: API(임베딩 peak RSS 약 2.4GB)와 DB 가 8GB 메모리를 공유하므로 OOM kill 대신 swap 으로 버티게 한다
if ! swapon --show | grep -q /swapfile; then
    fallocate -l 4G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

[ -d "$DIR/.git" ] || git clone --depth 1 "$REPO" "$DIR"

# .env 에는 시크릿이 들어가므로 root 만 읽을 수 있게 한다
[ -f "$DIR/deploy/ncp/.env" ] || cp "$DIR/deploy/ncp/.env.example" "$DIR/deploy/ncp/.env"
chmod 600 "$DIR/deploy/ncp/.env"
chmod +x "$DIR/deploy/ncp/"*.sh

# 매일 04:30 백업. 재실행 시 중복 등록되지 않도록 태그 주석으로 기존 항목을 제거하고 다시 넣는다
( crontab -l 2>/dev/null | grep -v '# suddoe-backup' || true
  echo "30 4 * * * $DIR/deploy/ncp/backup.sh >> /var/log/suddoe-backup.log 2>&1 # suddoe-backup" ) | crontab -

cat <<EOF
setup complete. next steps:
  1. $DIR/deploy/ncp/.env 값 입력 (시크릿 생성: openssl rand -hex 32)
  2. 로컬 PC 에서 덤프 업로드:
     scp suddoe.dump source_rowcounts.txt root@<PUBLIC_IP>:$DIR/deploy/ncp/backups/
  3. cd $DIR/deploy/ncp && ./restore.sh backups/suddoe.dump backups/source_rowcounts.txt
  4. docker compose up -d --build
EOF
