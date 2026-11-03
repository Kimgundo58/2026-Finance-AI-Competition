#!/usr/bin/env bash
# 운영 서버 배포: GitHub Actions(image.yml, ci 통과 뒤)가 Container Registry 에 올린 main 이미지를 받아
# api 컨테이너를 바꾼다. 절차·설치·되돌리기: docs/8_운영/8-8_CI_CD.md
#
#   ./deploy.sh --auto   cron 이 5분마다 부른다. origin/main 커밋의 이미지가 있으면 배포한다.
#                        배포된 커밋 이후 db/migrations 나 deploy/ncp 가 바뀌었으면 배포하지 않고 로그만 남긴다.
#                        (마이그레이션·설정은 손으로 반영한 뒤 --force 로 배포한다)
#   ./deploy.sh --force  위 확인 없이 origin/main 이미지를 배포한다. 처음 한 번도 이것으로 한다.
#                        SSH 가 끊기면 같이 끊기므로 nohup 으로 실행한다:
#                        nohup ./deploy.sh --force >> /var/log/suddoe-deploy.log 2>&1 &
#
# 새 컨테이너가 healthy 가 되지 않으면 직전 이미지(suddoe-api:rollback-auto)로 되돌린다.
# compose 는 그대로 suddoe-api:prod 를 쓴다. 이 스크립트는 받은 이미지에 그 태그를 붙인다.
set -euo pipefail

REPO=/opt/suddoe
COMPOSE_DIR=$REPO/deploy/ncp
IMAGE=suddoe.kr.ncr.ntruss.com/suddoe-api
STATE_DIR=/var/lib/suddoe
# cron 줄의 flock 과 다른 파일이다. 손으로 부른 --force 와 cron 실행이 겹치지 않게 한다
LOCK=/run/lock/suddoe-deploy.run.lock
MIN_FREE_GB_FULL=8     # 이미지 전체(약 6.5GB)를 받을 때: 서버에 레지스트리 이미지가 없거나 Dockerfile·의존성이 바뀜
MIN_FREE_GB_NEXT=3     # 바뀐 레이어만 받을 때(캐시가 맞으면 코드 레이어 수 MB)
HEALTH_TIMEOUT=420     # compose healthcheck start_period 180초 + 재시도 여유

mode=${1:-}
case "$mode" in --auto|--force) ;; *) echo "usage: $0 --auto|--force" >&2; exit 2 ;; esac

log() { echo "$(date '+%F %T') $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  [ "$mode" = --force ] && { log "다른 배포가 실행 중이다. 끝난 뒤 다시 실행한다"; exit 1; }
  exit 0
fi

mkdir -p "$STATE_DIR"
cd "$REPO"
git fetch -q origin main
target=$(git rev-parse origin/main)
deployed=$(cat "$STATE_DIR/deployed_sha" 2>/dev/null || true)

# 같은 커밋에 대해 같은 종류의 기록은 한 번만 남긴다 (cron 이 5분마다 같은 줄을 쓰지 않게)
once() {
  local f="$STATE_DIR/logged_$1"
  [ "$(cat "$f" 2>/dev/null || true)" = "$target" ] && return 0
  echo "$target" > "$f"
  shift
  log "$*"
}

api_cid() { (cd "$COMPOSE_DIR" && docker compose ps -a -q api); }

# api 컨테이너가 실행 중이고 healthy 면 그 이미지 ID, 아니면 빈 문자열
running_healthy_image() {
  local cid
  cid=$(api_cid)
  [ -n "$cid" ] || return 0
  docker inspect -f '{{if .State.Running}}{{if eq .State.Health.Status "healthy"}}{{.Image}}{{end}}{{end}}' "$cid" 2>/dev/null || true
}

wait_healthy() {
  local cid status waited=0
  cid=$(api_cid)
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo missing)
    case "$status" in
      healthy) return 0 ;;
      unhealthy|missing) return 1 ;;
    esac
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

start_and_check() { (cd "$COMPOSE_DIR" && docker compose up -d --no-build api) && wait_healthy; }

if [ "$mode" = --auto ]; then
  if [ -z "$deployed" ]; then
    once nostate "배포 기록($STATE_DIR/deployed_sha)이 없다. 처음 한 번은 ./deploy.sh --force 로 배포한다"
    exit 0
  fi
  [ "$target" = "$deployed" ] && exit 0
  # 배포했다가 되돌린 커밋은 다시 시도하지 않는다. 다음 커밋이 오거나 --force 로 배포한다
  [ "$target" = "$(cat "$STATE_DIR/failed_sha" 2>/dev/null || true)" ] && exit 0
  manual=$(git diff --name-only "$deployed" "$target" -- db/migrations deploy/ncp)
  if [ -n "$manual" ]; then
    once hold "hold $target: 손으로 반영할 변경이 있다. 반영 후 ./deploy.sh --force"$'\n'"$(echo "$manual" | sed 's/^/  /')"
    exit 0
  fi
fi

need_gb=$MIN_FREE_GB_NEXT
if [ -z "$(docker images -q "$IMAGE")" ] || [ -z "$deployed" ] \
   || ! git diff --quiet "$deployed" "$target" -- Dockerfile requirements-api.txt; then
  need_gb=$MIN_FREE_GB_FULL
fi
free_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
if [ "$free_gb" -lt "$need_gb" ]; then
  once disk "skip $target: 디스크 여유 ${free_gb}G < ${need_gb}G (docker builder prune·지난 rollback-* 정리)"
  exit 1
fi

if ! err=$(docker pull -q "$IMAGE:$target" 2>&1 >/dev/null); then
  if ! echo "$err" | grep -qiE 'not found|manifest unknown'; then
    once pullerr "pull 실패 $target: $(echo "$err" | tail -1)"
    exit 1
  fi
  [ "$mode" = --force ] && { log "image $IMAGE:$target 없음. Actions 의 ci·image 워크플로를 확인한다"; exit 1; }
  # 아직 빌드 중이거나 ci 가 실패해 빌드하지 않았다. 60분이 지나도 없으면 한 번 기록한다
  p_sha=; p_ts=
  [ -f "$STATE_DIR/pending" ] && read -r p_sha p_ts < "$STATE_DIR/pending"
  if [ "$p_sha" != "$target" ]; then
    echo "$target $(date +%s)" > "$STATE_DIR/pending"
  elif [ $(( $(date +%s) - p_ts )) -gt 3600 ]; then
    once pending "image $IMAGE:$target 가 60분째 없다 (ci 실패 또는 image 워크플로 확인)"
  fi
  exit 0
fi

new_id=$(docker image inspect -f '{{.Id}}' "$IMAGE:$target")
# 태그가 아니라 실제로 실행 중이고 healthy 인 컨테이너와 비교한다. 중간에 끊긴 배포를 '배포됨'으로 기록하지 않게
if [ "$new_id" = "$(running_healthy_image)" ]; then
  echo "$target" > "$STATE_DIR/deployed_sha"
  git merge -q --ff-only origin/main 2>/dev/null || true
  [ "$mode" = --force ] && log "$target: 실행 중인 이미지와 같고 healthy 라 재시작하지 않았다"
  exit 0   # 문서만 바뀐 커밋 등
fi

# 되돌릴 이미지: 지금 컨테이너가 쓰는 이미지. 새 이미지와 같으면(지난 배포가 중간에 끊김) 기존 rollback-auto 를 둔다
cid=$(api_cid)
old_id=
[ -n "$cid" ] && old_id=$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)
if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
  docker tag "$old_id" suddoe-api:rollback-auto
fi

log "deploy ${deployed:-기록없음} -> $target"
docker tag "$IMAGE:$target" suddoe-api:prod

if start_and_check; then
  echo "$target" > "$STATE_DIR/deployed_sha"
  git merge -q --ff-only origin/main || log "warn: 서버 checkout 을 fast-forward 하지 못했다(로컬 변경 확인)"
  # 레지스트리에서 받은 지난 커밋 태그를 지운다. 같은 이미지에 다른 태그가 있으면 태그만 떨어진다
  docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" | grep -v ":$target\$" | xargs -r docker rmi >/dev/null 2>&1 || true
  docker image prune -f >/dev/null || true
  log "deployed $target"
else
  log "unhealthy: $target 되돌림"
  echo "$target" > "$STATE_DIR/failed_sha"
  if [ -n "$(docker images -q suddoe-api:rollback-auto)" ]; then
    docker tag suddoe-api:rollback-auto suddoe-api:prod
    if start_and_check; then log "rolled back"; else log "되돌린 이미지도 unhealthy — 바로 확인 필요"; fi
  fi
  exit 1
fi
