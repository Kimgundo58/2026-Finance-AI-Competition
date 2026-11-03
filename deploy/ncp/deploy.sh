#!/usr/bin/env bash
# 운영 서버 배포: GitHub Actions(image.yml)가 Container Registry 에 올린 main 이미지를 받아 api 컨테이너를 바꾼다.
# 절차·설치·되돌리기: docs/8_운영/8-8_CI_CD.md
#
#   ./deploy.sh --auto   cron 이 5분마다 부른다. origin/main 커밋의 이미지가 있으면 배포한다.
#                        배포된 커밋 이후 db/migrations 나 deploy/ncp 가 바뀌었으면 배포하지 않고 로그만 남긴다.
#                        (마이그레이션·설정은 손으로 반영한 뒤 --force 로 배포한다)
#   ./deploy.sh --force  위 확인 없이 origin/main 이미지를 배포한다.
#
# 새 컨테이너가 healthy 가 되지 않으면 직전 이미지(suddoe-api:rollback-auto)로 되돌린다.
# compose 는 그대로 suddoe-api:prod 를 쓴다. 이 스크립트는 받은 이미지에 그 태그를 붙인다.
set -euo pipefail

REPO=/opt/suddoe
COMPOSE_DIR=$REPO/deploy/ncp
IMAGE=suddoe.kr.ncr.ntruss.com/suddoe-api
STATE_DIR=/var/lib/suddoe
MIN_FREE_GB=8          # 처음 받을 때 이미지 전체(약 6.5GB)를 받는다
HEALTH_TIMEOUT=420     # compose healthcheck start_period 180초 + 재시도 여유

mode=${1:-}
case "$mode" in --auto|--force) ;; *) echo "usage: $0 --auto|--force" >&2; exit 2 ;; esac

log() { echo "$(date '+%F %T') $*"; }

mkdir -p "$STATE_DIR"
cd "$REPO"
git fetch -q origin main
target=$(git rev-parse origin/main)
[ -s "$STATE_DIR/deployed_sha" ] || git rev-parse HEAD > "$STATE_DIR/deployed_sha"
deployed=$(cat "$STATE_DIR/deployed_sha")

if [ "$mode" = --auto ]; then
  [ "$target" = "$deployed" ] && exit 0
  # 배포했다가 되돌린 커밋은 다시 시도하지 않는다. 다음 커밋이 오거나 --force 로 배포한다
  [ "$target" = "$(cat "$STATE_DIR/failed_sha" 2>/dev/null || true)" ] && exit 0
  manual=$(git diff --name-only "$deployed" "$target" -- db/migrations deploy/ncp)
  if [ -n "$manual" ]; then
    if [ "$target" != "$(cat "$STATE_DIR/blocked_sha" 2>/dev/null || true)" ]; then
      log "hold $target: 손으로 반영할 변경이 있다. 반영 후 ./deploy.sh --force"
      echo "$manual" | sed 's/^/  /'
      echo "$target" > "$STATE_DIR/blocked_sha"
    fi
    exit 0
  fi
fi

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
  log "skip $target: 디스크 여유 ${free_gb}G < ${MIN_FREE_GB}G"
  exit 1
fi

# 이미지가 아직 빌드 중이면 다음 실행에서 다시 본다
if ! docker pull -q "$IMAGE:$target" >/dev/null 2>&1; then
  [ "$mode" = --force ] && { log "image $IMAGE:$target 없음 (Actions image 워크플로 확인)"; exit 1; }
  # 빌드는 보통 30분 안에 끝난다. 60분이 지나도 받지 못하면 한 번만 기록한다
  p_sha=; p_ts=
  [ -f "$STATE_DIR/pending" ] && read -r p_sha p_ts < "$STATE_DIR/pending"
  if [ "$p_sha" != "$target" ]; then
    echo "$target $(date +%s)" > "$STATE_DIR/pending"
  elif [ $(( $(date +%s) - p_ts )) -gt 3600 ]; then
    log "image $IMAGE:$target 를 60분째 받지 못했다 (Actions image 워크플로·레지스트리 로그인 확인)"
    echo "$target 9999999999" > "$STATE_DIR/pending"
  fi
  exit 0
fi

new_id=$(docker image inspect -f '{{.Id}}' "$IMAGE:$target")
cur_id=$(docker image inspect -f '{{.Id}}' suddoe-api:prod 2>/dev/null || true)
if [ "$new_id" = "$cur_id" ]; then
  echo "$target" > "$STATE_DIR/deployed_sha"
  git merge -q --ff-only origin/main 2>/dev/null || true
  [ "$mode" = --force ] && log "$target: 실행 중인 이미지와 같아 재시작하지 않았다"
  exit 0   # 문서만 바뀐 커밋 등
fi

wait_healthy() {
  local cid status waited=0
  cid=$(cd "$COMPOSE_DIR" && docker compose ps -q api)
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

log "deploy $deployed -> $target"
[ -n "$cur_id" ] && docker tag suddoe-api:prod suddoe-api:rollback-auto
docker tag "$IMAGE:$target" suddoe-api:prod
(cd "$COMPOSE_DIR" && docker compose up -d --no-build api)

if wait_healthy; then
  echo "$target" > "$STATE_DIR/deployed_sha"
  git merge -q --ff-only origin/main || log "warn: 서버 checkout 을 fast-forward 하지 못했다(로컬 변경 확인)"
  # 레지스트리에서 받은 지난 커밋 태그를 지운다. 같은 이미지에 다른 태그가 있으면 태그만 떨어진다
  docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" | grep -v ":$target\$" | xargs -r docker rmi >/dev/null 2>&1 || true
  docker image prune -f >/dev/null
  log "deployed $target"
else
  log "unhealthy: $target 되돌림"
  echo "$target" > "$STATE_DIR/failed_sha"
  if [ -n "$cur_id" ]; then
    docker tag suddoe-api:rollback-auto suddoe-api:prod
    (cd "$COMPOSE_DIR" && docker compose up -d --no-build api)
    if wait_healthy; then log "rolled back"; else log "되돌린 이미지도 unhealthy — 바로 확인 필요"; fi
  fi
  exit 1
fi
