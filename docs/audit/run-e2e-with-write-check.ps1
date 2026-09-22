#!/usr/bin/env pwsh
# Run browser E2E scripts and independently verify "zero writes" via backend logs.
#
# Background (real incident): diag118 registered multiple page.route handlers,
# which caused the interception to fail -- a real work order (#63) was deleted.
# At the time I only compared the work-order total before/after and claimed
# "data safe". The total was unchanged only because the test never successfully
# created anything; what it deleted belonged to the user.
# Lesson: the criterion must be independent of the thing under test.
#         Here we use the backend access log.
#
# NOTE: This file is intentionally ASCII-only. Windows PowerShell 5.1 reads
#       .ps1 without BOM as ANSI/GBK, which corrupts non-ASCII text and can
#       break string terminators. (Third encoding trap in this project.)
#
# Usage: powershell -File docs/audit/run-e2e-with-write-check.ps1

param(
  [string[]]$Scripts = @('diag93.mjs','diag94.mjs','diag95.mjs','diag97.mjs',
                         'diag113.mjs','diag118.mjs','diag109.mjs','diag124.mjs'),
  [string]$ProbeDir = "$env:TEMP\pw-probe"
)

$ErrorActionPreference = 'Continue'

function Get-WriteCounts {
  $log = docker logs churn-backend 2>&1 | Out-String
  @{
    create  = ([regex]::Matches($log, 'POST /api/work-orders')).Count
    confirm = ([regex]::Matches($log, 'POST /api/agent/confirm')).Count
    batch   = ([regex]::Matches($log, 'POST /api/agent/confirm-batch')).Count
    delete  = ([regex]::Matches($log, 'DELETE /api/work-orders')).Count
  }
}

function Show-Counts($c, $label) {
  Write-Host ("  {0,-8} create={1} confirm={2} batch={3} delete={4}" -f `
    $label, $c.create, $c.confirm, $c.batch, $c.delete)
}

Write-Host "=============================================================="
Write-Host "Sync helpers to probe dir"
Write-Host "=============================================================="
foreach ($f in @('_guard.mjs','_write_counter.mjs')) {
  Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $ProbeDir $f) -Force `
    -ErrorAction SilentlyContinue
}
foreach ($s in $Scripts) {
  # Map the short probe name back to the real source file.
  # e.g. 'diag93.mjs' -> base 'diag93' -> source 'diag93-*.mjs'
  #
  # WARNING (real bug, caused a stale-copy false FAIL): do NOT use
  # $s.Split('-')[0] -- for 'diag93.mjs' there is no dash, so it yields
  # 'diag93.mjs' including the extension, and the ^diag\d+$ test then fails.
  # Result: nothing was ever copied and the probe dir kept an OLD copy.
  $base = $s -replace '\.mjs$', ''
  if ($base -match '^diag\d+$') {
    $src = Get-ChildItem (Join-Path $PSScriptRoot "$base-*.mjs") `
      -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($src) {
      Copy-Item $src.FullName (Join-Path $ProbeDir $s) -Force
    } else {
      Write-Host "  (source for $s not found)"
    }
  }
}

$before = Get-WriteCounts
Write-Host ""
Write-Host "=============================================================="
Write-Host "Baseline before run"
Write-Host "=============================================================="
Show-Counts $before 'before'

$results = @()
foreach ($s in $Scripts) {
  $path = Join-Path $ProbeDir $s
  if (-not (Test-Path $path)) {
    Write-Host ""
    Write-Host "--- $s (not found, skipped) ---"
    continue
  }
  Write-Host ""
  Write-Host "=============================================================="
  Write-Host "--- $s ---"
  Write-Host "=============================================================="
  Push-Location $ProbeDir
  $out = & node $s 2>&1 | Out-String
  Pop-Location
  ($out -split "`n") | Where-Object {
    $_ -match 'PASS|FAIL|Timeout|Error'
  } | Select-Object -First 10 | ForEach-Object { Write-Host "  $($_.TrimEnd())" }
  # Verdict: take the LAST line containing PASS or FAIL.
  # Scripts print a final summary line with exactly one of them, but may also
  # print diagnostics mentioning FAIL earlier (e.g. "non-expected"), so a
  # whole-output substring match would be wrong. ASCII-only (see header).
  #
  # WARNING (PowerShell gotcha, cost me a wrong report): wrap in @(...) to
  # force an array. If only ONE line matches, Where-Object returns a scalar
  # string, and `$x[-1]` then yields the last CHARACTER instead of the last
  # element -- so the comparison silently fails and everything looks FAIL.
  $verdict = 'FAIL'
  $marked = @($out -split "`n" | Where-Object { $_ -match 'PASS|FAIL' })
  if ($marked.Count -gt 0) {
    $line = $marked[$marked.Count - 1]
    if ($line -match 'FAIL') { $verdict = 'FAIL' }
    elseif ($line -match 'PASS') { $verdict = 'PASS' }
  }
  if ($out -match 'TimeoutError') { $verdict = 'FAIL' }
  $results += [pscustomobject]@{ Script = $s; Verdict = $verdict }
}

Start-Sleep -Seconds 3
$after = Get-WriteCounts
Write-Host ""
Write-Host "=============================================================="
Write-Host "Counts after run (writes must NOT increase)"
Write-Host "=============================================================="
Show-Counts $after 'after'

$leak = @()
foreach ($k in @('create','confirm','batch','delete')) {
  if ($after[$k] -gt $before[$k]) { $leak += "$k +$($after[$k] - $before[$k])" }
}

Write-Host ""
Write-Host "=============================================================="
Write-Host "Script verdicts"
Write-Host "=============================================================="
$results | ForEach-Object { Write-Host ("  [{0}] {1}" -f $_.Verdict, $_.Script) }

Write-Host ""
Write-Host "=============================================================="
Write-Host "Interpretation"
Write-Host "=============================================================="
# diag118 creates ONE fixture order (real write, via page.request) and deletes
# it in finally -- so create/delete each +1 with net zero. That is intended.
# Any OTHER increment means a script leaked a write.
$createDelta = $after.create - $before.create
$deleteDelta = $after.delete - $before.delete
$otherDelta = @()
foreach ($k in @('confirm','batch')) {
  if ($after[$k] -gt $before[$k]) { $otherDelta += "$k +$($after[$k] - $before[$k])" }
}

if ($otherDelta.Count -gt 0) {
  Write-Host "LEAK: confirm/batch writes reached backend -> $($otherDelta -join ', ')" -ForegroundColor Red
  exit 2
}
if ($createDelta -eq $deleteDelta) {
  if ($createDelta -gt 0) {
    Write-Host "OK: create/delete each +$createDelta -- expected (diag118 fixture, net zero)" -ForegroundColor Green
  } else {
    Write-Host "OK: zero writes across all scripts (verified via backend log)" -ForegroundColor Green
  }
  Write-Host "    confirm/batch unchanged: no unconfirmed write leaked."
} else {
  Write-Host "SUSPICIOUS: create +$createDelta but delete +$deleteDelta (fixture not cleaned?)" -ForegroundColor Yellow
  Write-Host "    Please check for leftover test work orders."
  exit 3
}

