$ErrorActionPreference = "Continue"
$proj = "C:\Users\dogun\Downloads\Desktop\Desktop\Desktop\Desktop\김건도\3-1 여름방학\금융 AI공모전"
Set-Location -LiteralPath $proj
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $proj "scripts\_work\_resume.log"
"=== relay start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Encoding utf8
$claude = "$env:APPDATA\npm\claude.cmd"
if (-not (Test-Path $claude)) { $claude = "claude" }
& $claude -p "Read the file scripts/_work/_RESUME.md and follow it exactly." --dangerously-skip-permissions 2>&1 |
    Out-File -FilePath $log -Encoding utf8 -Append
"=== relay end $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -FilePath $log -Encoding utf8 -Append
