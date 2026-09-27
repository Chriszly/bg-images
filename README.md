# bg-images

Collection of SVG background images and the tooling used to render them into PNGs at scale.

## What happens here

This repo takes a layered SVG (`bear -split.svg`) and renders it into hundreds of color variants as 2160×2160 PNGs using [Inkscape](https://inkscape.org). A second generator does the same for the robot line art, recoloring each of its 5 paths independently — and with `--sweep` can hold most of those paths fixed to render a bear-style 400-image set instead of 3.2 million — see [Robot line art](#robot-line-art).

`generate-bear-pngs.py` (or its equivalent `generate-bear-pngs.ps1`):

1. Reads the source SVG and a 20-color palette.
2. Builds every 20×20 color combination by swapping the two placeholder colors in the SVG (`#00ffff` and `#ff7a00`) for each palette pair.
3. Renders each combination with Inkscape, parallelized (8 concurrent processes), with automatic retry of failed renders.
4. Writes results as `bear_<left-color>_<right-color>.png`.

### Usage

```bash
python generate-bear-pngs.py                        # black background
python generate-bear-pngs.py --background white
python generate-bear-pngs.py --background transparent
python generate-bear-pngs.py --background 123456
python generate-bear-pngs.py --swap-color ff0000    # replace ff0000 with black
```

### Parameters

| Parameter       | Description                                                                 |
|-----------------|-----------------------------------------------------------------------------|
| `--background`  | Export background: `black` (default), `white`, `transparent`, or any hex color. |
| `--swap-color`  | A palette color to replace with black (`000000`) before rendering.          |
| `--inkscape`    | Path to `inkscape.exe`; defaults to PATH lookup, then the standard install path. |
| `--source`      | Path to the source SVG; defaults to `bear -split.svg` next to the script.   |
| `--out-dir`     | Output base directory; defaults to the script's directory.                  |
| `--concurrency` | Number of parallel Inkscape processes (default 8).                         |
| `--render-timeout` | Seconds before a single Inkscape render is killed and counted as a failure (default 120). |
| `--width`/`--height` | Output PNG dimensions in pixels (default 2160).                       |

### Output

Each run produces 400 PNGs (20 left × 20 right colors) in a folder named by background:

| Folder                 | Invocation                                                     |
|------------------------|----------------------------------------------------------------|
| `bear-pngs`            | `python generate-bear-pngs.py`                                 |
| `bear-pngs-white`      | `python generate-bear-pngs.py --background white`              |
| `bear-pngs-transparent`| `python generate-bear-pngs.py --background transparent`        |
| `bear-pngs-<hex>`      | `python generate-bear-pngs.py --background <hex>`              |
| `bear-pngs-<hex>` (swap) | e.g. `python generate-bear-pngs.py --swap-color ff0000`      |

## Robot line art

`generate-robot-pngs.py` (or `generate-robot-pngs.ps1`) applies the same idea to
`robot_building_arm_line_art2.svg`, with one important difference: instead of
swapping two fixed placeholder colors, it gives **every `<path>` its own color**
from the palette. The artwork has 5 paths, so by default the combination space is
20⁵ = **3,200,000 images**.

Path slots, in document order (this is also the filename order):

| Slot | Element           | `stroke-width` |
|------|-------------------|----------------|
| 1    | robot arm + pedestal | 3            |
| 2    | crate               | 7            |
| 3    | wrench + motion marks | 3          |
| 4    | mascot body, head, limbs | 4        |
| 5    | mascot face         | 4            |

Because the space is far too large to materialize up front, the generator
*streams* combinations in index order, rendering one bounded batch at a time.
It is also **resumable** — existing PNGs are skipped — and **shardable** so the
work can be spread over several machines or days.

### Sweeping only some paths

`--sweep` reduces the space to the bear-style 20×20 grid. Only the slots you list
vary across the palette; every other slot is given a color **derived** from a
hash of the swept colors. So `--sweep 1,4` gives 20 × 20 = **400 images** with the
robot arm and the mascot in all combinations and the other three paths
differently colored in each one.

The derived colors are a pure function of the swept pair, which is what keeps the
run resumable, shardable, and identical between the Python and PowerShell
scripts — the same filename always means the same image. It is a
*pseudorandom* derivation, not a fresh `random` call: a true random pick would not
be reproducible, so a resumed run could not tell which combinations it had
already rendered.

Two properties fall out of that determinism:

- **A derived path never comes out the same color as one of the two main
  paths.** The swept colors are removed from the candidate set before the hash
  is reduced, so a derived path draws from the remaining 18 (19 if the two main
  paths happen to share a color).
- **The filename needs only the two main colors.** Because everything else is
  derived from them, the pair identifies the image completely — a five-color
  filename would only be noise. So `--sweep 1,4` writes
  `robot_building_arm_line_art3_<arm>_<mascot>.png`.

The hash is a polynomial rolling hash plus a finalizer, spelled out by hand in
both scripts so they agree bit for bit. Two details matter if you touch it:

- It is **not** Python's `hash()`, which is salted per process, nor
  `GetStringHashCode`, which is salted the same way. Either would make every run
  disagree with the last.
- The finalizer is **not optional**. A rolling hash is linear in its last
  character, so keying the derived slots as `"<swept>|<slot>"` alone made slot 3
  always land one palette step after slot 2, and slot 5 three steps after it —
  the three "random" paths collapsed into a single fixed offset triple.

```bash
# mascot + robot arm over the full 20x20 grid, everything else derived, 400 PNGs
python generate-robot-pngs.py --source robot_building_arm_line_art3.svg --sweep 1,4

# ... at a uniform stroke weight instead of the source's mixed 3/7/4
python generate-robot-pngs.py --source robot_building_arm_line_art3.svg --sweep 1,4 --stroke-width 3
```

PowerShell uses the same options with `-` prefixes, e.g.
`.\generate-robot-pngs.ps1 -Sweep 1,4 -StrokeWidth 3`. Quote the sweep list if
your shell splits on the comma: `-Sweep '1,4'`.

### Usage

```bash
python generate-robot-pngs.py --dry-run                      # preview the plan
python generate-robot-pngs.py --shard 0/8                    # render 1 of 8 chunks
python generate-robot-pngs.py --limit 50                      # just the first 50
python generate-robot-pngs.py                                # all 3,200,000
python generate-robot-pngs.py --background white
python generate-robot-pngs.py --swap-color ff0000
```

### Parameters

| Parameter          | Description                                                                    |
|--------------------|--------------------------------------------------------------------------------|
| `--background`     | Export background: `black` (default), `white`, `transparent`, or a hex color.  |
| `--swap-color`     | A palette color to replace with black before rendering.                       |
| `--sweep`          | Comma-separated 1-based path numbers to sweep, e.g. `1,4`. Paths not listed get a derived color. Default: sweep every path. |
| `--stroke-width`   | Override the `stroke-width` of every path, e.g. `3`. Default: keep the source values. |
| `--group-by`       | File each PNG into a subdirectory named after that 1-based slot's color, e.g. `4` to group by mascot color. Default: flat. |
| `--out-name`       | Name of the output directory itself, e.g. `robot-combo`. Default: `<prefix>-pngs` plus a background suffix. |
| `--shard i/n`      | Render only shard `i` of `n`. Shards tile the space with no gaps or overlap.  |
| `--start-index` / `--end-index` | Explicit half-open range of combination indices.                  |
| `--limit`          | Stop after this many images in the current run.                               |
| `--dry-run`        | Print the plan and sample filenames without rendering.                        |
| `--overwrite`      | Re-render PNGs that already exist (default is to skip them).                   |
| `--inkscape`       | Path to `inkscape.exe`; defaults to PATH lookup, then the standard install path. |
| `--source`         | Path to the source SVG; defaults to `robot_building_arm_line_art2.svg`.        |
| `--out-dir`        | Output base directory; defaults to the script's directory.                     |
| `--tmp-dir`        | Scratch directory for intermediate SVGs.                                       |
| `--concurrency`    | Number of parallel Inkscape processes (default 8).                             |
| `--render-timeout` | Seconds before a single Inkscape render is killed and counted as a failure (default 120). |
| `--width`/`--height` | Output PNG dimensions in pixels (default 2160).                             |

### Render timeouts

All four generators bound every individual Inkscape render. Inkscape
occasionally wedges on a PNG export and never exits, and because a batch is
drained as a unit, one stuck render used to block its whole batch — and every
batch after it — with no output and no error, so the run simply looked hung.
`--render-timeout` (PowerShell: `-RenderTimeout`, default 120s) turns that into
an ordinary failure the retry pass picks up, and a genuinely wedged image is
reported by name. The retry gets twice the budget, since some wedges are only a
slow first run rather than a true deadlock.

On Windows each render gets its own process group and is killed with
`taskkill /T`, so no Inkscape helper process is left holding the output file. In
the PowerShell scripts the wait covers the whole batch with a single timeout
budget, so a batch of N hung renders costs one timeout rather than N.

### Output

By default, with no `--sweep`, every slot is swept and the filename lists them
all in document order: `robot_<path1>_..._<pathN>.png` in `robot-pngs` (or
`robot-pngs-<background>`), for example
`robot_ff0000_00ff00_0000ff_ffff00_ff00ff.png`. The output needs no sidecar
manifest, and a resumed run can recompute exactly where it left off.

With `--sweep` only the swept colors appear, in slot order — see
[Sweeping only some paths](#sweeping-only-some-paths) for why that is still a
complete identifier.

Pointing `--source` at a different SVG derives both the folder and the filename
prefix from that file's name, so two artworks that happen to share a path
structure cannot overwrite each other:

```bash
python generate-robot-pngs.py --source robot_building_arm_line_art3.svg
# -> robot_building_arm_line_art3-pngs/robot_building_arm_line_art3_<...>.png
```

`--out-name` and `--group-by` then let you collect and file the results:

```bash
python generate-robot-pngs.py --source robot_building_arm_line_art3.svg \
    --sweep 1,4 --stroke-width 3 --out-name robot-combo --group-by 4
# -> robot-combo/<mascot-color>/robot_building_arm_line_art3_<arm>_<mascot>.png
#    20 folders x 20 PNGs = 400
```

With `--group-by 4` every one of the 20 mascot colors gets its own folder, and
within a folder the robot arm cycles through all 20. Note that `--out-name`
drops the background suffix, so two backgrounds would land in the same folder —
add the suffix yourself (`--out-name robot-combo-white`) if you render more
than one.

The resume check and the render target both resolve through the same path
helper, so a grouped run is as resumable as a flat one: re-running the command
above reports `400 already present` and re-renders nothing.

The number of color slots is read from the file, so a source with a different
path count produces `palette ** path_count` images automatically.

> **Note on scale:** at roughly 200 KB per PNG, a full run is on the order of
> **600 GB** and several days of rendering. Plan disk space and use `--shard` to
> divide the work; the default single-machine invocation will try to render all
> 3,200,000 images.

## Requirements

- Windows
- Python 3.7+ (uses only the standard library)
- [Inkscape](https://inkscape.org) (found on PATH, or pass `--inkscape` with the full path).

## License

See [LICENSE](LICENSE).
