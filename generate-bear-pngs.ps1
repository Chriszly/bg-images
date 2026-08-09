param(
  [string]$Background = 'black',
  [string]$SwapColor = '',
  [string]$Inkscape = '',
  [string]$Source = '',
  [string]$OutDir = ''
)
$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$inkscape = if ($Inkscape) { $Inkscape } else { (Get-Command inkscape.exe -ErrorAction SilentlyContinue).Source }
if (-not $inkscape) { $inkscape = 'C:\Program Files\Inkscape\bin\inkscape.exe' }
$src      = if ($Source) { $Source } else { Join-Path $scriptDir 'bear -split.svg' }
$outBase  = if ($OutDir) { $OutDir } else { $scriptDir }
$suffix = switch ($Background) {
  'black'       { '' }
  'white'       { '-white' }
  'transparent' { '-transparent' }
  default       { '-' + $Background.ToLower() }
}
$out = Join-Path $outBase ("bear-pngs" + $suffix)
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) 'bg-images\bear-gen'

New-Item -ItemType Directory -Force -Path $out | Out-Null
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

switch ($Background) {
  'black'       { $bg = @('--export-background=black','--export-background-opacity=1') }
  'white'       { $bg = @('--export-background=white','--export-background-opacity=1') }
  'transparent' { $bg = @() }
  default       { $bg = @("--export-background=#$($Background.ToLowerInvariant())",'--export-background-opacity=1') }
}

$colors = @(
  'ffffff','7a7a7a','ff0000','ff7a00','ff007a','ff7a7a',
  '00ff00','7aff00','00ff7a','7aff7a',
  '0000ff','7a00ff','007aff','7a7aff',
  '00ffff','7affff','ff00ff','ff7aff','ffff00','ffff7a'
)
if ($SwapColor) {
  $swap = $SwapColor.ToLowerInvariant()
  if ($colors -notcontains $swap) { Write-Warning ("SwapColor {0} not present in palette" -f $swap) }
  $colors = $colors | ForEach-Object { if ($_.ToLowerInvariant() -eq $swap) { '000000' } else { $_ } }
}

$jobs = New-Object System.Collections.Generic.List[object]
$idx = 0
foreach ($left in $colors) {
  foreach ($right in $colors) {
    $svgTmp = Join-Path $tmp ("work_{0}.svg" -f $idx)
    $content = Get-Content -LiteralPath $src -Raw
    $content = $content.Replace('#00ffff', '#' + $left).Replace('#ff7a00', '#' + $right)
    Set-Content -LiteralPath $svgTmp -Value $content -Encoding UTF8
    $png = Join-Path $out ("bear_{0}_{1}.png" -f $left, $right)
    $jobs.Add([pscustomobject]@{ svg = $svgTmp; png = $png })
    $idx++
  }
}

$baseArgs = @(
  '--export-type=png',
  '--export-width=2160',
  '--export-height=2160'
) + $bg
$concurrency = 8
$total = $jobs.Count
$done = 0
$failures = @()

function Invoke-Render {
  param([array]$Items, [int]$Parallel)
  $procs = @()
  foreach ($j in $Items) {
    $procs += Start-Process -FilePath $inkscape -ArgumentList ($baseArgs + @("--export-filename=$($j.png)", $j.svg)) -PassThru
    if ($procs.Count -ge $Parallel) {
      $procs | Wait-Process
      $procs = @()
    }
  }
  $procs | Wait-Process
}

for ($i = 0; $i -lt $total; $i += $concurrency) {
  $hi = [Math]::Min($i + $concurrency - 1, $total - 1)
  $batch = @()
  for ($k = $i; $k -le $hi; $k++) { $batch += $jobs[$k] }
  Invoke-Render $batch $concurrency
  $missing = $batch | Where-Object { -not (Test-Path -LiteralPath $_.png) }
  if ($missing) { $failures += $missing }
  $done += $batch.Count
  Write-Host ("{0}/{1}" -f $done, $total)
}

if ($failures.Count -gt 0) {
  Write-Host ("Retrying {0} failed renders..." -f $failures.Count)
  Invoke-Render $failures 2
  $failures = $failures | Where-Object { -not (Test-Path -LiteralPath $_.png) }
}

Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
if ($failures.Count -gt 0) {
  Write-Warning ("{0} PNGs still missing: {1}" -f $failures.Count, ($failures.png -join ', '))
  Write-Host ("Done. {0}/{1} PNGs in {2}" -f ($done - $failures.Count), $total, $out)
} else {
  Write-Host ("Done. {0} PNGs in {1}" -f $done, $out)
}