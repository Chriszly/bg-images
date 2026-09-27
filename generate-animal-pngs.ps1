<#
.SYNOPSIS
  Render every color combination of the wild-animal SVGs to PNGs using Inkscape.

.DESCRIPTION
  Each SVG in wild-animals/ is a single animal drawn as line art inside <defs>
  and then shown twice by <use>:

      <use href="#half" stroke="#19e3ee"/>                        <- slot 1
      <use href="#half" stroke="#ff8a1c" transform="translate(400,0)
                                                 scale(-1,1)"/>   <- slot 2

  Slot 1 is the upright animal, slot 2 is the same geometry mirrored about the
  seam at x=198, so the pair reads as one symmetric two-tone image. Recoloring
  never touches the geometry: only the stroke on the two <use> tags changes,
  which is why the same code works on the compact files and on the
  pretty-printed ones (comments, indentation, an absent transform on slot 1).

  With the 20-color palette that is 20 x 20 combinations per animal, and 20
  animals in wild-animals/ makes 20 ** 3 = 8,000 PNGs, filed as

      animal-pictures/<animal>/<first-color>/<animal>_<first>_<second>.png

  so the first color is the subdirectory and the second color is the file. The
  run is resumable (existing PNGs are skipped) and shardable with -Shard i/n.

  This is the PowerShell twin of generate-animal-pngs.py. The two must agree on
  the output paths exactly, or a run split across them would scatter images and
  a resumed run could not tell what it had already done.
#>
param(
  [string]$Background = 'black',
  [string]$Inkscape = '',
  [string]$SrcDir = '',
  [string]$OutDir = '',
  [string]$OutName = 'animal-pictures',
  [string]$TmpDir = '',
  [string]$Animal = '',
  [string]$FirstColors = '',
  [string]$SecondColors = '',
  [switch]$DistinctOnly,
  [int]$Concurrency = 8,
  [int]$Width = 2160,
  [int]$Height = 2160,
  [int]$RenderTimeout = 120,
  [switch]$Overwrite,
  [switch]$DryRun,
  [int]$Limit = 0,
  [string]$Shard = '',
  [long]$StartIndex = -1,
  [long]$EndIndex = -1
)
$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$inkscape = if ($Inkscape) { $Inkscape } else { (Get-Command inkscape.exe -ErrorAction SilentlyContinue).Source }
if (-not $inkscape) { $inkscape = 'C:\Program Files\Inkscape\bin\inkscape.exe' }
if (-not (Test-Path -LiteralPath $inkscape)) { throw "Inkscape not found: $inkscape" }

$srcRoot = if ($SrcDir) { $SrcDir } else { Join-Path $scriptDir 'wild-animals' }
if (-not (Test-Path -LiteralPath $srcRoot -PathType Container)) {
  throw "Source directory not found: $srcRoot"
}

# The palette is the same 20 colors as the bear and robot generators, so the
# three sets of output folders stay directly comparable.
$colors = @(
  'ffffff','7a7a7a','ff0000','ff7a00','ff007a','ff7a7a',
  '00ff00','7aff00','00ff7a','7aff7a',
  '0000ff','7a00ff','007aff','7a7aff',
  '00ffff','7affff','ff00ff','ff7aff','ffff00','ffff7a'
)

function Select-Colors {
  param([string]$Spec, [string]$Label)
  if (-not $Spec) { return @($colors) }
  $wanted = @()
  # Split on commas or whitespace: PowerShell turns an unquoted -FirstColors
  # ff0000,00ff00 into the single string "ff0000 00ff00" before the script sees it.
  foreach ($part in ($Spec -split '[\s,]+')) {
    if (-not $part) { continue }
    $c = $part.TrimStart('#').ToLowerInvariant()
    if ($colors -notcontains $c) { throw "$Label not in palette: $c" }
    # De-duplicate while keeping the user's order, so repeating a color cannot
    # silently multiply the run.
    if ($wanted -notcontains $c) { $wanted += $c }
  }
  if ($wanted.Count -eq 0) { throw "$Label selection is empty" }
  $wanted
}

# A <use> start tag. The alternation skips over quoted attribute values so a '>'
# inside one cannot end the match early.
$useRe = New-Object System.Text.RegularExpressions.Regex(
  '<use\b(?:[^>"'']|"[^"]*"|''[^'']*'')*?/?>',
  [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
$strokeRe = New-Object System.Text.RegularExpressions.Regex(
  '(\s+stroke\s*=")([^"]*)(")',
  [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

# Each source must show its geometry exactly twice: upright, then mirrored.
$expectedUses = 2

# Set stroke="#color" on a start tag, replacing or injecting as needed.
function Set-Stroke {
  param([string]$Tag, [string]$Color)
  if ($strokeRe.IsMatch($Tag)) {
    return $strokeRe.Replace($Tag, ('$1' + '#' + $Color + '$3'), 1)
  }
  $rendered = ' stroke="#' + $Color + '"'
  if ($Tag.EndsWith('/>')) { return $Tag.Substring(0, $Tag.Length - 2) + $rendered + '/>' }
  return $Tag.Substring(0, $Tag.Length - 1) + $rendered + '>'
}

# Colors are applied by <use> position rather than by substituting the
# placeholder hex. Both placeholders are unique in these files, but each source
# also carries a large C2PA provenance blob in <metadata> whose base64 payload is
# arbitrary text, so matching a hex string inside it is a real (if unlikely) way
# to corrupt a render. Going through the stroke attribute cannot touch it.
function New-ColoredSvg {
  param([string]$Text, [string]$First, [string]$Second, [int]$SlotCount)
  if ($SlotCount -ne $expectedUses) {
    throw "expected $expectedUses <use> tags, found $SlotCount"
  }
  $sb = New-Object System.Text.StringBuilder
  $pos = 0
  $slot = 0
  foreach ($m in $useRe.Matches($Text)) {
    [void]$sb.Append($Text.Substring($pos, $m.Index - $pos))
    $color = if ($slot -eq 0) { $First } else { $Second }
    [void]$sb.Append((Set-Stroke -Tag $m.Value -Color $color))
    $pos = $m.Index + $m.Length
    $slot++
  }
  [void]$sb.Append($Text.Substring($pos))
  $sb.ToString()
}

# Where a combination's PNG lives. The subdirectory carries the first color and
# the filename carries both, so a PNG is traceable to its exact pair from the
# path alone. The resume check and the render target both resolve through here,
# so a resumed run cannot disagree with a fresh one.
function Get-PngPath {
  param([string]$Out, [string]$AnimalName, [string]$First, [string]$Second)
  $name = '{0}_{1}_{2}.png' -f $AnimalName, $First, $Second
  Join-Path (Join-Path (Join-Path $Out $AnimalName) $First) $name
}

# --- collect the animals -------------------------------------------------
$files = @(Get-ChildItem -LiteralPath $srcRoot -Filter 'geo_*.svg' -File | Sort-Object Name)
$allAnimals = @($files | ForEach-Object { $_.BaseName -replace '^geo_', '' })
$selected = $allAnimals
if ($Animal) {
  $wantedNames = @()
  foreach ($part in ($Animal -split '[\s,]+')) {
    if (-not $part) { continue }
    $wantedNames += $part.ToLowerInvariant()
  }
  $selected = @($allAnimals | Where-Object { $wantedNames -contains $_.ToLowerInvariant() })
  $missing = @($wantedNames | Where-Object { $allAnimals.ToLowerInvariant() -notcontains $_ })
  if ($missing.Count -gt 0) {
    throw ("no source SVG for: " + (($missing | Sort-Object -Unique) -join ', '))
  }
}
if ($selected.Count -eq 0) { throw "no geo_*.svg found in $srcRoot" }

# Cache each source's text and <use> count once. The same file is recolored once
# per color pair, and re-reading it 400 times per animal is pure waste.
$sources = @{}
foreach ($f in $files) {
  $name = $f.BaseName -replace '^geo_', ''
  if ($selected -notcontains $name) { continue }
  $text = [System.IO.File]::ReadAllText($f.FullName)
  $count = $useRe.Matches($text).Count
  if ($count -ne $expectedUses) {
    Write-Warning ("Skipping {0}: expected {1} <use> tags, found {2}" -f $f.Name, $expectedUses, $count)
    continue
  }
  $sources[$name] = $text
}
if ($sources.Count -eq 0) { throw "No usable sources in $srcRoot" }
$animals = @($selected | Where-Object { $sources.ContainsKey($_) })
if ($animals.Count -eq 0) { throw "No usable sources in $srcRoot" }

# The result variables are deliberately named differently from the -FirstColors
# and -SecondColors parameters. PowerShell variable names are case-insensitive,
# so `$firstColors = ...Select-Colors -Spec $FirstColors` would assign the
# palette into the very parameter being read, collapsing 20 colors into one.
$firstPalette  = @(Select-Colors -Spec $FirstColors  -Label 'first color')
$secondPalette = @(Select-Colors -Spec $SecondColors -Label 'second color')

# The image list, animal-major and second-color-fastest. The ordering matters for
# a sharded or interrupted run: consecutive entries are the 20 files inside one
# first-color folder, so a shard boundary never splits a folder's contents.
$combos = New-Object System.Collections.Generic.List[object]
foreach ($a in $animals) {
  foreach ($first in $firstPalette) {
    foreach ($second in $secondPalette) {
      if ($DistinctOnly -and $first -eq $second) { continue }
      $combos.Add([pscustomobject]@{ animal = $a; first = $first; second = $second })
    }
  }
}
$total = [long]$combos.Count

$start = [long]0
$end   = $total
if ($Shard) {
  $parts = $Shard.Split('/')
  if ($parts.Count -ne 2) { throw "Shard must look like i/n, e.g. 0/8 (got '$Shard')" }
  $si = 0; $sn = 0
  if (-not [int]::TryParse($parts[0], [ref]$si)) { throw "Shard index must be an integer" }
  if (-not [int]::TryParse($parts[1], [ref]$sn)) { throw "Shard count must be an integer" }
  if ($sn -le 0 -or $si -lt 0 -or $si -ge $sn) { throw "Shard index must be 0..n-1 with n > 0" }
  $size = [long][Math]::Ceiling($total / [double]$sn)
  $start = $si * $size
  $end   = [Math]::Min($start + $size, $total)
}
if ($StartIndex -ge 0) { $start = [Math]::Max([long]0, $StartIndex) }
if ($EndIndex -ge 0)   { $end   = [Math]::Min($total, $EndIndex) }
if ($Limit -gt 0)      { $end   = [Math]::Min($end, $start + $Limit) }
if ($end -lt $start)   { $end = $start }

$outBase  = if ($OutDir) { $OutDir } else { $scriptDir }
$suffix = switch ($Background) {
  'black'       { '' }
  'white'       { '-white' }
  'transparent' { '-transparent' }
  default       { '-' + $Background.ToLower() }
}
# Likewise $folderName, not $outName, which would collide with the -OutName
# parameter for the same reason.
$folderName = if ($OutName) { $OutName } else { 'animal-pictures' }
$out = Join-Path $outBase ($folderName + $suffix)

$tmpRoot = if ($TmpDir) { $TmpDir } else { [System.IO.Path]::GetTempPath() }
$tmp = Join-Path $tmpRoot "bg-images\animal-gen-$PID"

Write-Host "Source:   $($animals.Count) animals from $srcRoot"
Write-Host 'Slots:    2 per animal (upright, mirrored)'
Write-Host "Palette:  $($firstPalette.Count) first x $($secondPalette.Count) second"
Write-Host "Total:    $total combinations ($($animals.Count) animals x $($firstPalette.Count) x $($secondPalette.Count))"
Write-Host ("Range:    [{0}, {1}) -> {2} images" -f $start, $end, ($end - $start))
Write-Host "Size:     ${Width}x${Height} on $Background"
Write-Host "Output:   $folderName/<animal>/<first>/<animal>_<first>_<second>.png"
if ($DistinctOnly) { Write-Host 'Note:     -DistinctOnly skips matching-color combinations.' }

if ($DryRun) {
  if ($end -gt $start) {
    foreach ($i in @($start, [long][Math]::Min($start + 1, $end - 1), ($end - 1))) {
      $c = $combos[$i]
      Write-Host ("  " + (Get-PngPath -Out $out -AnimalName $c.animal -First $c.first -Second $c.second))
    }
  }
  Write-Host 'Dry run: nothing rendered.'
  return
}

New-Item -ItemType Directory -Force -Path $out | Out-Null
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$bg = switch ($Background) {
  'black'       { @('--export-background=black','--export-background-opacity=1') }
  'white'       { @('--export-background=white','--export-background-opacity=1') }
  'transparent' { @() }
  default       { @("--export-background=#$($Background.ToLowerInvariant())",'--export-background-opacity=1') }
}
$baseArgs = @(
  '--export-type=png',
  "--export-width=$Width",
  "--export-height=$Height"
) + $bg

# The timeout is not optional. Inkscape occasionally wedges on a PNG export and
# never exits; because a batch is drained as a unit, one stuck render would
# otherwise block its whole batch -- and every later batch -- with no output and
# no error, which is exactly how a run appears to hang. With a timeout the hang
# becomes an ordinary failure that the retry pass picks up.
function Wait-Render {
  param([array]$Running, [int]$TimeoutSec)
  # Wait on the whole batch with a single timeout budget. Waiting on each
  # process in turn would make a batch of N hung renders cost N timeouts, so the
  # bound would scale with concurrency instead of being a real bound.
  $ids = @($Running | ForEach-Object { $_.proc.Id })
  try {
    Wait-Process -Id $ids -Timeout $TimeoutSec -ErrorAction Stop
    return
  } catch { }
  # At least one blew the budget. Give the rest a brief grace in case they were
  # only just finishing, then kill whatever is left so no helper process keeps
  # holding the output file.
  try {
    Wait-Process -Id $ids -Timeout ([Math]::Min(5, $TimeoutSec)) -ErrorAction SilentlyContinue
  } catch { }
  foreach ($r in $Running) {
    $alive = $true
    try { $alive = -not $r.proc.HasExited } catch { $alive = $false }
    if (-not $alive) { continue }
    # taskkill /T takes the whole tree down.
    & taskkill /F /T /PID $r.proc.Id 2>$null | Out-Null
    Stop-Process -Id $r.proc.Id -Force -ErrorAction SilentlyContinue
  }
}

function Invoke-Render {
  param([array]$Items, [int]$Parallel, [int]$TimeoutSec)
  $running = @()
  foreach ($j in $Items) {
    $p = Start-Process -FilePath $inkscape `
      -ArgumentList ($baseArgs + @("--export-filename=$($j.png)", $j.svg)) -PassThru
    $running += [pscustomobject]@{ proc = $p; png = $j.png }
    if ($running.Count -ge $Parallel) {
      Wait-Render -Running $running -TimeoutSec $TimeoutSec
      $running = @()
    }
  }
  if ($running.Count -gt 0) { Wait-Render -Running $running -TimeoutSec $TimeoutSec }
}

$planned   = $end - $start
$batchSize = [Math]::Max(1, $Concurrency) * 4
$done = 0; $rendered = 0; $skipped = 0
$failures = @()
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$lastReport = 0.0

for ($i = $start; $i -lt $end; $i += $batchSize) {
  $batch = @()
  $hi = [Math]::Min($i + $batchSize, $end)
  for ($k = $i; $k -lt $hi; $k++) {
    $c = $combos[$k]
    $png = Get-PngPath -Out $out -AnimalName $c.animal -First $c.first -Second $c.second
    if ((Test-Path -LiteralPath $png) -and -not $Overwrite) { $skipped++; continue }
    $svgTmp = Join-Path $tmp ("work_{0}_{1}_{2}.svg" -f $c.animal, $c.first, $c.second)
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $png)) | Out-Null
    [System.IO.File]::WriteAllText($svgTmp, (New-ColoredSvg -Text $sources[$c.animal] -First $c.first -Second $c.second -SlotCount $expectedUses))
    $batch += [pscustomobject]@{ svg = $svgTmp; png = $png }
  }
  if ($batch.Count -gt 0) {
    Invoke-Render $batch $Concurrency $RenderTimeout
    $missing = @($batch | Where-Object { -not (Test-Path -LiteralPath $_.png) })
    $rendered += ($batch.Count - $missing.Count)
    foreach ($m in $missing) { $failures += $m }
  }
  $done += ($hi - $i)
  Remove-Item -Path (Join-Path $tmp 'work_*.svg') -Force -ErrorAction SilentlyContinue

  $elapsed = $sw.Elapsed.TotalSeconds
  if ($elapsed - $lastReport -ge 10) {
    $lastReport = $elapsed
    $rate = $done / [Math]::Max($elapsed, 0.001)
    $eta = ($planned - $done) / [Math]::Max($rate, 0.0001)
    Write-Host ("{0}/{1} rendered  {2:N1}/s  eta {3}" -f $done, $planned, $rate, ([TimeSpan]::FromSeconds($eta)))
  }
}

if ($failures.Count -gt 0) {
  Write-Host ("Retrying {0} failed renders..." -f $failures.Count)
  $retry = @()
  $step = [Math]::Max(1, $Concurrency) * 4
  for ($i = 0; $i -lt $failures.Count; $i += $step) {
    $slice = @()
    $hiRetry = [Math]::Min($i + $step, $failures.Count)
    for ($k = $i; $k -lt $hiRetry; $k++) { $slice += $failures[$k] }
    if ($slice.Count -eq 0) { continue }
    Invoke-Render $slice 2 ($RenderTimeout * 2)
    foreach ($j in $slice) {
      if (Test-Path -LiteralPath $j.png) { $rendered++ } else { $retry += $j }
    }
  }
  $failures = $retry
}

Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
if ($failures.Count -gt 0) {
  Write-Warning ("{0} PNGs still missing after retry" -f $failures.Count)
  $failures | Select-Object -First 20 | ForEach-Object { Write-Warning ("  " + (Split-Path -Leaf $_.png)) }
  if ($failures.Count -gt 20) { Write-Warning ("  ... and {0} more" -f ($failures.Count - 20)) }
}
Write-Host ("Done. {0} rendered, {1} already present, {2} failed in {3:N0}s -> {4}" -f `
  $rendered, $skipped, $failures.Count, $sw.Elapsed.TotalSeconds, $out)
