#!/usr/bin/env bash
# 합성 Docker 응답으로 세 백업과 L3 오류 처리를 확인한다. 운영·실제 DB 접근 없음.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/suddoe-backup-check-XXXXXXXX")
mkdir -p "$work/bin" "$work/input/attachments" "$work/input/l3" "$work/ok" "$work/fail"
printf 'synthetic attachment\n' > "$work/input/attachments/attachment.txt"
printf 'synthetic L3\n' > "$work/input/l3/l3.txt"
cp "$here/backup.sh" "$work/ok/backup.sh"
cp "$here/backup.sh" "$work/fail/backup.sh"
cat > "$work/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *pg_dump*) printf 'synthetic database dump' ;;
  *pg_restore*) cat >/dev/null ;;
  *"tar -C /app/_plan_attachments"*) tar -C "$MOCK_INPUT/attachments" -czf - . ;;
  *"tar -C /app/_l3_업로드"*)
    if [[ "${MOCK_L3_FAIL:-0}" == 1 ]]; then exit 1; fi
    tar -C "$MOCK_INPUT/l3" -czf - . ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH" MOCK_INPUT="$work/input"
bash "$work/ok/backup.sh" > /dev/null
[[ $(find "$work/ok/backups" -name '*.dump' | wc -l) -eq 1 ]]
[[ $(find "$work/ok/backups" -name 'attachments_*.tar.gz' | wc -l) -eq 1 ]]
[[ $(find "$work/ok/backups" -name 'l3_uploads_*.tar.gz' | wc -l) -eq 1 ]]
tar -tzf "$work/ok/backups/"l3_uploads_*.tar.gz | grep -Fx './l3.txt' > /dev/null
tar -tzf "$work/ok/backups/"attachments_*.tar.gz | grep -Fx './attachment.txt' > /dev/null
if MOCK_L3_FAIL=1 bash "$work/fail/backup.sh" > /dev/null 2>&1; then
  echo 'L3 백업 실패를 성공으로 처리함' >&2; exit 1
fi
[[ $(find "$work/fail/backups" -name 'l3_uploads_*.tar.gz' -o -name '*.part' | wc -l) -eq 0 ]]
echo '합성 DB·첨부·L3 백업 및 L3 실패 검사 통과'
