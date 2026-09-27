<#
.SYNOPSIS
  Render color combinations of a multi-path SVG to PNGs using Inkscape.

.DESCRIPTION
  By default each <path> in the source SVG is recolored with exactly one color
  from the palette, so the total number of outputs is palette ** pathCount. For
  the 20-color palette and the 5 paths of robot_building_arm_line_art2.svg that is
  20 ** 5 = 3,200,000 images.

  -Sweep narrows that down the way generate-bear-pngs.py works: only the listed
  slots (1-based, document order) vary across the palette, and every other slot
  is given a color derived from a hash of the swept colors. A given pair always
  maps to the same full combination, so the run stays resumable and reproducible
  while the other paths still look randomly colored. -Sweep 1,4 on a 5-path SVG
  yields 20 ** 2 = 400 images.

  The full space is far too many to build up front, so this script streams the
  combination space in index order, rendering one bounded batch at a time, and
  resumes by skipping PNGs that already exist. Use -Shard i/n to split the work
  across machines or across several days.

  Path order (document order) for the robot artwork:
    1. industrial robot arm + pedestal
    2. crate the mascot stands on
    3. wrench and tightening motion marks
    4. mascot body, head, visor, arms, legs
    5. mascot face (smile and eyes)

  Output filenames always encode all slots in document order, swept and derived
  alike, so an image can be traced back to its palette entry:
    robot_<path1>_<path2>_<path3>_<path4>_<path5>.png
#>
param(
  [string]$Background = 'black',
  [string]$SwapColor = '',
  [string]$Inkscape = '',
  [string]$Source = '',
  [string]$OutDir = '',
  [string]$OutName = '',
  [string]$TmpDir = '',
  [int]$Concurrency = 8,
  [int]$RenderTimeout = 120,
  [int]$Width = 2160,
  [int]$Height = 2160,
  [switch]$Overwrite,
  [switch]$DryRun,
  [int]$Limit = 0,
  [string]$Shard = '',
  [long]$StartIndex = -1,
  [long]$EndIndex = -1,
  [string]$Sweep = '',
  [string]$StrokeWidth = '',
  [string]$GroupBy = ''
)
$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

$inkscape = if ($Inkscape) { $Inkscape } else { (Get-Command inkscape.exe -ErrorAction SilentlyContinue).Source }
if (-not $inkscape) { $inkscape = 'C:\Program Files\Inkscape\bin\inkscape.exe' }
if (-not (Test-Path -LiteralPath $inkscape)) { throw "Inkscape not found: $inkscape" }

$src = if ($Source) { $Source } else { Join-Path $scriptDir 'robot_building_arm_line_art2.svg' }
if (-not (Test-Path -LiteralPath $src)) { throw "Source SVG not found: $src" }

$outBase = if ($OutDir) { $OutDir } else { $scriptDir }

# A custom -Source gets its own output folder and filename prefix, so that two
# artworks with the same path structure (e.g. line_art2 and line_art3) cannot
# silently overwrite each other's identically named PNGs.
if ($Source) { $prefix = [System.IO.Path]::GetFileNameWithoutExtension($src) }
else          { $prefix = 'robot' }

$suffix = switch ($Background) {
  'black'       { '' }
  'white'       { '-white' }
  'transparent' { '-transparent' }
  default       { '-' + $Background.ToLowerInvariant() }
}
# -OutName pins the folder itself, e.g. to collect every robot set in one local
# directory. The background suffix is dropped in that case, so two backgrounds
# would land in the same folder; add it yourself if that matters.
if ($OutName) { $out = Join-Path $outBase $OutName }
else         { $out = Join-Path $outBase ($prefix + "-pngs" + $suffix) }

$tmpRoot = if ($TmpDir) { $TmpDir } else { [System.IO.Path]::GetTempPath() }
$tmp = Join-Path $tmpRoot "bg-images\robot-gen-$PID"

$content = Get-Content -LiteralPath $src -Raw

# A <path> start tag. The alternation skips over quoted attribute values so a
# '>' inside one cannot end the match early.
$pathRe = New-Object System.Text.RegularExpressions.Regex(
  '<path\b(?:[^>"'']|"[^"]*"|''[^'']*'')*?/?>',
  [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
$strokeRe = New-Object System.Text.RegularExpressions.Regex(
  '\s+stroke\s*=\s*("[^"]*"|''[^'']*'')',
  [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
$strokeWidthRe = New-Object System.Text.RegularExpressions.Regex(
  '\s+stroke-width\s*=\s*("[^"]*"|''[^'']*'')',
  [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)

$slots = $pathRe.Matches($content).Count
if ($slots -eq 0) { throw "No <path> elements in $src" }

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
  $colors = @($colors | ForEach-Object { if ($_.ToLowerInvariant() -eq $swap) { '000000' } else { $_ } })
}

# -GroupBy files each PNG into a subdirectory named after the color of that
# 1-based slot, e.g. 4 to group the mascot variants by mascot color.
$groupSlot = 0
if ($GroupBy) {
  if (-not [int]::TryParse($GroupBy.Trim(), [ref]$groupSlot)) {
    throw "GroupBy must be a single slot number, e.g. 4 (got '$GroupBy')"
  }
  if ($groupSlot -lt 1 -or $groupSlot -gt $slots) {
    throw "GroupBy slot $groupSlot out of range; the source has $slots paths"
  }
}

# Swept slots are 1-based and ascending. Ascending order matters: it fixes which
# slot the low index digits land on, so the same index means the same image no
# matter how the slots were listed on the command line.
$sweepSlots = @()
if ($Sweep) {
  $sweepSlots = @()
  # Split on commas or whitespace: PowerShell turns an unquoted -Sweep 1,4 into
  # the single string "1 4" before the script ever sees it.
  foreach ($part in ($Sweep -split '[\s,]+')) {
    if (-not $part) { continue }
    $n = 0
    if (-not [int]::TryParse($part, [ref]$n)) {
      throw "Sweep must be a comma-separated list of slot numbers, e.g. 1,4 (got '$Sweep')"
    }
    if ($n -lt 1 -or $n -gt $slots) {
      throw "Sweep slot $n out of range; the source has $slots paths"
    }
    if ($sweepSlots -notcontains $n) { $sweepSlots += $n }
  }
  $sweepSlots = @($sweepSlots | Sort-Object)
} else {
  $sweepSlots = @(1..$slots)
}

$base = $colors.Count
$total = [long][Math]::Pow($base, $sweepSlots.Count)

# 32-bit polynomial rolling hash with a multiply/xorshift finalizer, kept in
# int64 range throughout. This must stay bit-for-bit identical to poly_hash in
# generate-robot-pngs.py: a run split across the two scripts has to derive the
# same colors, or one filename would mean two different images. GetStringHashCode
# is unusable for that because it is salted per process.
#
# The finalizer is not optional. A rolling hash is linear in its last character,
# so keying the derived slots as "<swept>|<slot>" made slot 3 always land one
# palette step after slot 2 and slot 5 three steps after it; the three "random"
# paths came out as a single fixed offset triple.
function Get-PolyHash {
  param([string]$Text)
  $h = [long]0
  foreach ($c in $Text.ToCharArray()) {
    $h = ($h * 131 + [long][int]$c) % 2147483647
  }
  $h = (($h -bxor ($h -shr 15)) * 0x2C1B3C6D) % 2147483647
  $h = (($h -bxor ($h -shr 12)) * 0x297A2D39) % 2147483647
  [long]($h -bxor ($h -shr 15))
}

# Pick a stable pseudo-random palette entry for a non-swept slot. The swept
# colors are excluded from the candidate set, so a derived path can never come
# out the same color as one of the two main paths.
#
# The key is the swept colors in slot order plus the 1-based number of the slot
# being filled, so the result depends only on the swept colors -- not on the
# iteration order, the shard, or the process. That keeps a resumed or sharded run
# producing exactly the images the earlier run would have produced, and it is
# also what makes the two-color filename in Get-PngPath sufficient: the main
# colors identify the image, because everything else follows from them.
function Get-DerivedColor {
  param([string[]]$Colors, [string[]]$Swept, [int]$Slot)
  $candidates = @($Colors | Where-Object { $Swept -notcontains $_ })
  if ($candidates.Count -eq 0) { $candidates = $Colors }
  $key = (($Swept -join '|') + '|' + $Slot)
  $candidates[[int]((Get-PolyHash -Text $key) % $candidates.Count)]
}

# Map a combination index to one color per slot, most significant first, so
# consecutive indices vary the *last* swept slot (finest granularity for shards).
# Only the swept slots consume index digits; the rest are derived from those.
function Get-Combo {
  param([long]$Index, [string[]]$Colors, [int]$Slots, [int[]]$SweepSlots)
  $res = New-Object string[] $Slots
  $v = $Index
  for ($p = $SweepSlots.Count - 1; $p -ge 0; $p--) {
    $res[$SweepSlots[$p] - 1] = $Colors[[int]($v % $Colors.Count)]
    $v = [long][Math]::Floor($v / $Colors.Count)
  }
  if ($SweepSlots.Count -lt $Slots) {
    $swept = @($SweepSlots | ForEach-Object { $res[$_ - 1] })
    for ($slot = 1; $slot -le $Slots; $slot++) {
      if ($null -eq $res[$slot - 1]) {
        $res[$slot - 1] = Get-DerivedColor -Colors $Colors -Swept $swept -Slot $slot
      }
    }
  }
  $res
}

# Where a combination's PNG lives.
#
# The name carries only the swept colors, in slot order, because the derived
# colors are a pure function of those: the main colors identify the image
# completely, so a five-color filename would just be noise. With no -Sweep every
# slot is swept and this is unchanged.
#
# With -GroupBy the image goes into a subdirectory named after that slot's color,
# which is how the 400 mascot variants get filed under their mascot color. The
# resume check and the render target must agree, so both go through here.
function Get-PngPath {
  param([string]$Out, [string]$Prefix, [string[]]$Combo, [int[]]$SweepSlots, [int]$GroupSlot)
  $key = (@($SweepSlots | ForEach-Object { $Combo[$_ - 1] }) -join '_')
  $name = '{0}_{1}.png' -f $Prefix, $key
  if ($GroupSlot) { return (Join-Path (Join-Path $Out $Combo[$GroupSlot - 1]) $name) }
  return (Join-Path $Out $name)
}

# Set an explicit stroke (and optionally stroke-width) on every <path>. Some
# paths in the source carry no stroke of their own and inherit #000 from the
# enclosing group, so the attribute is injected when missing rather than only
# substituted when present.
function Set-Attr {
  param([string]$Tag, $AttrRe, [string]$Name, [string]$Value)
  $rendered = ' ' + $Name + '="' + $Value + '"'
  if ($AttrRe.IsMatch($Tag)) {
    return $AttrRe.Replace($Tag, $rendered, 1)
  }
  if ($Tag.EndsWith('/>')) { return $Tag.Substring(0, $Tag.Length - 2) + $rendered + '/>' }
  return $Tag.Substring(0, $Tag.Length - 1) + $rendered + '>'
}

function New-ColoredSvg {
  param([string]$Text, [string[]]$Combo, [string]$Width)
  $sb = New-Object System.Text.StringBuilder
  $pos = 0
  $slot = 0
  foreach ($m in $pathRe.Matches($Text)) {
    [void]$sb.Append($Text.Substring($pos, $m.Index - $pos))
    $tag = Set-Attr -Tag $m.Value -AttrRe $strokeRe -Name 'stroke' -Value ('#' + $Combo[$slot])
    if ($Width) { $tag = Set-Attr -Tag $tag -AttrRe $strokeWidthRe -Name 'stroke-width' -Value $Width }
    [void]$sb.Append($tag)
    $pos = $m.Index + $m.Length
    $slot++
  }
  [void]$sb.Append($Text.Substring($pos))
  $sb.ToString()
}

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

$quote = [char]34
Write-Host "Source:   $src"
Write-Host "Paths:    $slots color slots"
$slotNo = 0
foreach ($m in $pathRe.Matches($content)) {
  $slotNo++
  $role = if ($sweepSlots -contains $slotNo) { ' [swept]' } else { ' [derived]' }
  $d = ''
  $dm = [regex]::Match($m.Value, '\sd\s*=\s*"([^"]{0,48})')
  if ($dm.Success) { $d = $dm.Groups[1].Value.Trim() }
  Write-Host ('  slot {0}{1}: d={2}{3}...{2}' -f $slotNo, $role, $quote, $d)
}
Write-Host "Palette:  $base colors"
$sweptLabel = $sweepSlots -join ','
if ($sweepSlots.Count -eq $slots) { $sweptLabel += ' (all paths)' }
Write-Host "Swept:    $sweptLabel"
Write-Host "Total:    $total combinations ($base ** $($sweepSlots.Count))"
Write-Host ("Range:    [{0}, {1}) -> {2} images" -f $start, $end, ($end - $start))
$widthLabel = if ($StrokeWidth) { $StrokeWidth } else { '(from source)' }
Write-Host "Width:    stroke-width $widthLabel"
$groupLabel = if ($groupSlot) { "subdirectory per slot-$groupSlot color" } else { 'flat (no subdirectories)' }
Write-Host "Grouping: $groupLabel"
$namePattern = (@($sweepSlots | ForEach-Object { "<slot$_>" }) -join '_')
Write-Host ('Output:   {0}\{1}_{2}.png' -f $out, $prefix, $namePattern)
if ($total -gt 10000) {
  Write-Host "Note:     this is a large run; consider -Shard i/n across machines, and -DryRun to preview."
}

if ($DryRun) {
  if ($end -gt $start) {
    foreach ($i in @($start, [long][Math]::Min($start + 1, $end - 1), ($end - 1))) {
      $c = @(Get-Combo -Index $i -Colors $colors -Slots $slots -SweepSlots $sweepSlots)
      Write-Host ("  index {0} -> {1}" -f $i, (Get-PngPath -Out $out -Prefix $prefix -Combo $c -SweepSlots $sweepSlots -GroupSlot $groupSlot))
    }
  }
  Write-Host 'Dry run: nothing rendered.'
  return
}

New-Item -ItemType Directory -Force -Path $out | Out-Null
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$baseArgs = @(
  '--export-type=png',
  "--export-width=$Width",
  "--export-height=$Height"
) + $bg

# The timeout is not optional. Inkscape occasionally wedges on a PNG export and
# never exits; because a batch is drained as a unit, one stuck render would
# otherwise block its whole batch -- and every later batch -- with no output and
# no error, which is exactly how a run appears to hang. With a timeout the hang
# becomes an ordinary failure that the retry pass picks up, and a genuinely
# wedged image gets reported by name instead of stalling the run indefinitely.
# This matters more here than elsewhere: the full space is millions of images,
# so "blocked forever" is a real possibility rather than a thought experiment.
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
    $p = Start-Process -FilePath $inkscape -ArgumentList ($baseArgs + @("--export-filename=$($j.png)", $j.svg)) -PassThru
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
    $c = @(Get-Combo -Index $k -Colors $colors -Slots $slots -SweepSlots $sweepSlots)
    $png = Get-PngPath -Out $out -Prefix $prefix -Combo $c -SweepSlots $sweepSlots -GroupSlot $groupSlot
    if ((Test-Path -LiteralPath $png) -and -not $Overwrite) { $skipped++; continue }
    $svgTmp = Join-Path $tmp ("work_{0}.svg" -f ($c -join '_'))
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $png)) | Out-Null
    [System.IO.File]::WriteAllText($svgTmp, (New-ColoredSvg -Text $content -Combo $c -Width $StrokeWidth))
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
    # A render that already timed out once gets longer on the retry, since some
    # wedges are only a slow first run rather than a true deadlock.
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
