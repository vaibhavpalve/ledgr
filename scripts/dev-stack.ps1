<#
.SYNOPSIS
  Start, stop or check LEDGR's local dev stack on a machine without Docker.

.DESCRIPTION
  docker-compose.yml is the canonical local infrastructure. This script is the
  Windows-native equivalent for machines where Docker is absent: PostgreSQL and
  Azurite installed under .devtools/ (gitignored), the FastAPI app under uvicorn
  with --reload, and the Vite dev server. See docs/founder-review-2026-09-14.md
  and the "local dev stack" notes for how .devtools/ was populated.

.EXAMPLE
  .\scripts\dev-stack.ps1 status
  .\scripts\dev-stack.ps1 start          # everything
  .\scripts\dev-stack.ps1 start api      # one component
  .\scripts\dev-stack.ps1 restart api
  .\scripts\dev-stack.ps1 stop
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet('start', 'stop', 'restart', 'status')][string]$Action = 'status',
  [Parameter(Position = 1)][ValidateSet('all', 'postgres', 'azurite', 'api', 'web')][string]$Component = 'all'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$devtools = Join-Path $root '.devtools'
$pg = Join-Path $devtools 'pgsql\bin'
$pgdata = Join-Path $devtools 'pgdata'
$pgPort = 55432

function Test-Port([int]$port) {
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $async = $client.BeginConnect('127.0.0.1', $port, $null, $null)
    $ok = $async.AsyncWaitHandle.WaitOne(500)
    if ($ok) { $client.EndConnect($async) }
    $client.Close()
    return $ok
  } catch { return $false }
}

function Get-ProcessesMatching([string]$pattern) {
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match $pattern }
}

function Start-Postgres {
  if (Test-Port $pgPort) { Write-Host "postgres: already listening on $pgPort"; return }
  & (Join-Path $pg 'pg_ctl.exe') -D $pgdata -l (Join-Path $devtools 'pg.log') -w -t 20 start | Out-Null
  Write-Host "postgres: started on $pgPort"
}

function Stop-Postgres {
  if (-not (Test-Port $pgPort)) { Write-Host 'postgres: not running'; return }
  & (Join-Path $pg 'pg_ctl.exe') -D $pgdata -m fast stop | Out-Null
  Write-Host 'postgres: stopped'
}

function Start-Azurite {
  if (Test-Port 10000) { Write-Host 'azurite: already listening on 10000'; return }
  $azDir = Join-Path $devtools 'azurite'
  $entry = Join-Path $azDir 'node_modules\azurite\dist\src\azurite.js'
  Start-Process -FilePath 'node' -WorkingDirectory $azDir -WindowStyle Hidden -ArgumentList @(
    "`"$entry`"", '--blobHost', '127.0.0.1', '--blobPort', '10000',
    '--queueHost', '127.0.0.1', '--queuePort', '10001',
    '--location', "`"$(Join-Path $devtools 'azurite-data')`"", '--silent'
  )
  Write-Host 'azurite: started on 10000/10001'
}

function Stop-Azurite {
  Get-ProcessesMatching 'azurite\.js' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Write-Host 'azurite: stopped'
}

function Start-Api {
  if (Test-Port 8000) { Write-Host 'api: already listening on 8000'; return }
  $apiDir = Join-Path $root 'apps\api'
  $uvicorn = Join-Path $apiDir '.venv-check\Scripts\uvicorn.exe'
  # Settings read apps/api/.env (mirrored from the repo root .env); the two
  # values below are the only ones that must be present even when .env is not.
  $env:PYTHONPATH = Join-Path $apiDir 'src'
  if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = "postgresql+asyncpg://ledgr_app:ledgr-app-dev-password@localhost:$pgPort/ledgr"
  }
  Start-Process -FilePath $uvicorn -WorkingDirectory $apiDir -WindowStyle Hidden -ArgumentList @(
    'api.main:app', '--host', '127.0.0.1', '--port', '8000', '--reload', '--reload-dir', 'src'
  ) -RedirectStandardOutput (Join-Path $devtools 'api.out.log') -RedirectStandardError (Join-Path $devtools 'api.err.log')
  Write-Host 'api: started on 8000 (--reload; logs in .devtools/api.*.log)'
}

function Stop-Api {
  Get-ProcessesMatching 'uvicorn' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-ProcessesMatching 'multiprocessing\.spawn|api\.main:app' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Host 'api: stopped'
}

function Start-Web {
  if (Test-Port 5173) { Write-Host 'web: already listening on 5173'; return }
  Start-Process -FilePath 'cmd.exe' -WorkingDirectory $root -WindowStyle Hidden -ArgumentList @(
    '/d', '/s', '/c', 'corepack pnpm --filter @ledgr/web run dev'
  ) -RedirectStandardOutput (Join-Path $devtools 'web.out.log') -RedirectStandardError (Join-Path $devtools 'web.err.log')
  Write-Host 'web: started on 5173 (logs in .devtools/web.*.log)'
}

function Stop-Web {
  Get-ProcessesMatching 'vite(\.js|")' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Host 'web: stopped'
}

function Show-Status {
  $rows = @(
    @{ name = 'postgres'; port = $pgPort },
    @{ name = 'azurite';  port = 10000 },
    @{ name = 'api';      port = 8000 },
    @{ name = 'web';      port = 5173 }
  )
  foreach ($r in $rows) {
    $state = if (Test-Port $r.port) { 'up' } else { 'down' }
    Write-Host ("{0,-9} {1,-5} :{2}" -f $r.name, $state, $r.port)
  }
}

$order = @('postgres', 'azurite', 'api', 'web')
$targets = if ($Component -eq 'all') { $order } else { @($Component) }

switch ($Action) {
  'status'  { Show-Status }
  'start'   { foreach ($t in $targets) { & "Start-$((Get-Culture).TextInfo.ToTitleCase($t))" } }
  'stop'    { foreach ($t in ($targets | Sort-Object { $order.IndexOf($_) } -Descending)) { & "Stop-$((Get-Culture).TextInfo.ToTitleCase($t))" } }
  'restart' {
    foreach ($t in ($targets | Sort-Object { $order.IndexOf($_) } -Descending)) { & "Stop-$((Get-Culture).TextInfo.ToTitleCase($t))" }
    Start-Sleep -Seconds 1
    foreach ($t in $targets) { & "Start-$((Get-Culture).TextInfo.ToTitleCase($t))" }
  }
}
