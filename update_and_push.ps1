$ErrorActionPreference = "Continue"
$base   = "C:\Users\wxk11\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\work-mode-projects\6a8c67082883811a6cb4cc6f"
$repo   = Join-Path $base "national-team-etf-flow"
$secret = "C:\Users\wxk11\.trae-cn\memory\.gh_pat.txt"
$lock   = Join-Path $base ".update.lock"
$log    = Join-Path $base "auto_update.log"

if (Test-Path $lock) { Write-Output "previous run still active, skip"; exit 0 }
Set-Content -Path $lock -Value "1" -Encoding utf8
function Log($m){ $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  " + $m; Add-Content -Path $log -Value $line -Encoding utf8; Write-Output $m }
try {
  Set-Location $base
  python refresh_national_team_etf.py
  if ($LASTEXITCODE -ne 0) { Log "refresh failed, exit=$LASTEXITCODE"; exit 0 }

  Copy-Item (Join-Path $base "国家队ETF每日净买入卖出报表.html") (Join-Path $repo "index.html") -Force
  Copy-Item (Join-Path $base "refresh_national_team_etf.py")  (Join-Path $repo "refresh.py") -Force

  Set-Location $repo
  git add index.html refresh.py
  git diff --cached --quiet
  if ($LASTEXITCODE -eq 0) {
    Log "no change, skip push"
  } else {
    if (-not (Test-Path $secret)) { Log "secret missing, skip push"; exit 0 }
    $tok = (Get-Content $secret -Raw).Trim()
    git -c user.name="wwww1998" -c user.email="wwww1998@users.noreply.github.com" commit -m ("chore: 自动刷新国家队ETF净买卖报表 " + (Get-Date -Format "yyyyMMdd HH:mm")) | Out-Null
    if ($LASTEXITCODE -ne 0) { Log "commit failed"; exit 0 }
    $env:GIT_TERMINAL_PROMPT = "0"
    $url = "https://x-access-token:$tok@github.com/wwww1998/national-team-etf-flow.git"
    git -c credential.helper= push $url main 2>$null
    if ($LASTEXITCODE -eq 0) { Log "pushed to GitHub" } else { Log "push failed (network?)" }
  }
} finally {
  Remove-Item $lock -Force -ErrorAction SilentlyContinue
}