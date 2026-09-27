#!/usr/bin/env python3
"""Render every color combination of the wild-animal SVGs to PNGs using Inkscape.

Each SVG in ``wild-animals/`` is a single animal drawn as line art inside
``<defs>`` and then shown twice by ``<use>``:

    <use href="#half" stroke="#19e3ee"/>                              <- slot 1
    <use href="#half" stroke="#ff8a1c" transform="translate(400,0)
                                                scale(-1,1)"/>         <- slot 2

Slot 1 is the upright animal, slot 2 is the same geometry mirrored about the
seam at x=198, so the pair reads as one symmetric two-tone image. Recoloring
never touches the geometry: only the ``stroke`` on the two ``<use>`` tags
changes, which is why the same code works on the compact files and on the
pretty-printed ones (comments, indentation, an absent ``transform`` on slot 1).

With the 20-color palette that is 20 x 20 combinations per animal, and 20
animals in ``wild-animals/`` makes 20 ** 3 = 8,000 PNGs, filed as

    animal-pictures/<animal>/<first-color>/<animal>_<first>_<second>.png

so the *first* color is the subdirectory and the second color is the file. The
run is resumable (existing PNGs are skipped) and shardable with ``--shard i/n``.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Same 20-color palette as the bear and robot generators, so the three sets of
# output folders stay directly comparable.
PALETTE = [
    "ffffff", "7a7a7a", "ff0000", "ff7a00", "ff007a", "ff7a7a",
    "00ff00", "7aff00", "00ff7a", "7aff7a",
    "0000ff", "7a00ff", "007aff", "7a7aff",
    "00ffff", "7affff", "ff00ff", "ff7aff", "ffff00", "ffff7a",
]

# A <use> start tag. The alternation lets the pattern skip over quoted
# attribute values so that a '>' inside one cannot end the match early.
USE_TAG_RE = re.compile(r"<use\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?/?>", re.IGNORECASE)
STROKE_ATTR_RE = re.compile(r"(\s+stroke\s*=\s*\")([^\"]*)(\")", re.IGNORECASE)

SOURCE_DIR_NAME = "wild-animals"
SOURCE_GLOB = "geo_*.svg"
SOURCE_PREFIX = "geo_"

# Each source must show its geometry exactly twice: upright, then mirrored.
EXPECTED_USES = 2

PROGRESS_INTERVAL = 10.0  # seconds between progress lines


def suffix_for(background):
    """Directory suffix that keeps different backgrounds from colliding."""
    lower = background.lower()
    if lower == "black":
        return ""
    if lower == "white":
        return "-white"
    if lower == "transparent":
        return "-transparent"
    return "-" + lower


def inkscape_args(background):
    lower = background.lower()
    if lower == "transparent":
        return []
    if lower == "black":
        return ["--export-background=black", "--export-background-opacity=1"]
    if lower == "white":
        return ["--export-background=white", "--export-background-opacity=1"]
    return ["--export-background=#" + lower, "--export-background-opacity=1"]


def animal_name(stem):
    """``geo_bison`` -> ``bison``; a name without the prefix is used as-is."""
    if stem.startswith(SOURCE_PREFIX):
        return stem[len(SOURCE_PREFIX):]
    return stem


def load_animals(src_dir, only=None):
    """Return [(name, svg_path)] sorted by name, optionally filtered by ``only``."""
    files = sorted(src_dir.glob(SOURCE_GLOB))
    animals = [(animal_name(f.stem), f) for f in files]
    if only:
        wanted = {a.strip().lower()
                  for a in only.replace(" ", "").split(",") if a.strip()}
        animals = [(name, path) for name, path in animals if name.lower() in wanted]
        missing = wanted - {name.lower() for name, _ in animals}
        if missing:
            raise SystemExit(
                "no source SVG for: {}".format(", ".join(sorted(missing)))
            )
    if not animals:
        raise SystemExit("no {} found in {}".format(SOURCE_GLOB, src_dir))
    return animals


def count_uses(source_text):
    """Number of <use> tags, which is the number of color slots."""
    return len(USE_TAG_RE.findall(source_text))


def set_stroke(tag, color):
    """Set stroke="#color" on a start tag, replacing or injecting as needed."""
    return STROKE_ATTR_RE.sub(
        lambda m: '{}{}{}'.format(m.group(1), "#" + color, m.group(3)), tag, count=1
    )


def build_svg(source_text, first, second, slots):
    """Return the source SVG with slot 1 stroked ``first`` and slot 2 ``second``.

    Colors are applied by ``<use>`` position rather than by substituting the
    placeholder hex. Both placeholders are unique in these files, but each
    source also carries a large C2PA provenance blob in ``<metadata>`` whose
    base64 payload is arbitrary text, so matching a hex string inside it is a
    real (if unlikely) way to corrupt a render. Going through the ``stroke``
    attribute cannot touch it.
    """
    if slots != EXPECTED_USES:
        raise ValueError(
            "expected {} <use> tags, found {}".format(EXPECTED_USES, slots)
        )
    parts = []
    pos = 0
    for slot, match in enumerate(USE_TAG_RE.finditer(source_text)):
        parts.append(source_text[pos:match.start()])
        parts.append(set_stroke(match.group(0), first if slot == 0 else second))
        pos = match.end()
    parts.append(source_text[pos:])
    return "".join(parts)


def png_path(out, animal, first, second):
    """Where a combination's PNG lives.

    The subdirectory carries the first color and the filename carries both, so
    a PNG is traceable to its exact pair from the path alone. The resume check
    and the render target both resolve through here, so a resumed run cannot
    disagree with a fresh one.
    """
    return out / animal / first / "{}_{}_{}.png".format(animal, first, second)


def build_combos(animals, first_colors, second_colors, distinct_only):
    """The full image list, animal-major and second-color-fastest.

    The ordering matters for a sharded or interrupted run: consecutive entries
    are the 20 files inside one first-color folder, so a shard boundary never
    splits a folder's contents across unrelated animals.
    """
    combos = []
    for animal, _path in animals:
        for first in first_colors:
            for second in second_colors:
                if distinct_only and first == second:
                    continue
                combos.append((animal, first, second))
    return combos


def kill_tree(proc):
    """Kill a render process and any children it spawned.

    Inkscape can leave helper processes behind, and a surviving child would keep
    holding the output file. ``taskkill /T`` takes the whole tree down; on other
    platforms there is nothing to walk, so the direct child is enough.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def render(svg, png, base_args, inkscape, timeout):
    """Render one SVG to PNG. False on failure, non-zero exit, or timeout.

    The timeout is not optional. Inkscape occasionally wedges on a PNG export
    and never exits; because a batch is drained with ``pool.map``, one stuck
    render would otherwise block its whole batch -- and every later batch --
    with no output and no error, which is exactly how a run appears to hang.
    With a timeout the hang becomes an ordinary failure that the retry pass
    picks up, and a genuinely wedged image is reported by name instead of
    stalling the run indefinitely.
    """
    cmd = [inkscape] + base_args + [
        "--export-filename={}".format(png), str(svg)
    ]
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # Its own process group, so kill_tree can take descendants with it.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        return False
    return proc.returncode == 0 and png.exists()


def process_batch(batch, sources, out, tmp, base_args, inkscape, concurrency,
                  overwrite, timeout):
    """Render one batch. Returns (rendered, skipped, failures)."""
    def task(combo):
        animal, first, second = combo
        png = png_path(out, animal, first, second)
        if png.exists() and not overwrite:
            return (combo, None, True)
        source_text, slots = sources[animal]
        svg = tmp / "work_{}_{}_{}.svg".format(animal, first, second)
        try:
            png.parent.mkdir(parents=True, exist_ok=True)
            svg.write_text(
                build_svg(source_text, first, second, slots), encoding="utf-8"
            )
        except (OSError, ValueError) as exc:
            return (combo, str(exc), False)
        if render(svg, png, base_args, inkscape, timeout):
            return (combo, None, False)
        return (combo, "render failed or timed out", False)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(task, batch))

    rendered = skipped = 0
    failures = []
    for combo, error, was_skipped in results:
        if was_skipped:
            skipped += 1
        elif error is None:
            rendered += 1
        else:
            failures.append((combo, error))
    return rendered, skipped, failures


def clean_tmp(tmp):
    for svg in tmp.glob("work_*.svg"):
        try:
            svg.unlink()
        except OSError:
            pass


def parse_shard(spec, total):
    """Parse 'i/n' into a contiguous [start, end) range of the index space."""
    try:
        part, _, count = spec.partition("/")
        index = int(part)
        parts = int(count)
    except ValueError:
        raise argparse.ArgumentTypeError("shard must look like i/n, e.g. 0/8")
    if parts <= 0 or index < 0 or index >= parts:
        raise argparse.ArgumentTypeError("shard index must be 0..n-1 with n > 0")
    size = -(-total // parts)  # ceiling division
    start = index * size
    return start, min(start + size, total)


def parse_color_list(spec, label):
    """Resolve a comma-separated palette selection to palette entries."""
    if not spec:
        return list(PALETTE)
    wanted = [c.strip().lower().lstrip("#")
              for c in spec.replace(" ", "").split(",") if c.strip()]
    unknown = [c for c in wanted if c not in PALETTE]
    if unknown:
        raise SystemExit(
            "{} not in palette: {}".format(label, ", ".join(unknown))
        )
    # De-duplicate while keeping the user's order, so that repeating a color
    # cannot silently multiply the run.
    seen = set()
    out = []
    for color in wanted:
        if color not in seen:
            seen.add(color)
            out.append(color)
    return out


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--background", default="black",
                        help="black, white, transparent, or a hex color")
    parser.add_argument("--inkscape", default="",
                        help="path to inkscape.exe")
    parser.add_argument("--src-dir", dest="src_dir", default="",
                        help="directory of source SVGs (default: wild-animals "
                             "next to the script)")
    parser.add_argument("--out-dir", dest="out_dir", default="",
                        help="output base directory (default: script dir)")
    parser.add_argument("--out-name", dest="out_name", default="animal-pictures",
                        help="name of the output directory itself, plus a "
                             "background suffix when not black "
                             "(default: animal-pictures)")
    parser.add_argument("--tmp-dir", dest="tmp_dir", default="",
                        help="scratch directory (default: temp dir)")
    parser.add_argument("--animal", default="",
                        help="comma-separated animal names to restrict the run "
                             "to, e.g. 'fox,wolf' (default: every source)")
    parser.add_argument("--first-colors", dest="first_colors", default="",
                        help="comma-separated palette entries for the first "
                             "color (default: the whole palette)")
    parser.add_argument("--second-colors", dest="second_colors", default="",
                        help="comma-separated palette entries for the second "
                             "color (default: the whole palette)")
    parser.add_argument("--distinct-only", dest="distinct_only",
                        action="store_true",
                        help="skip combinations where both colors are the same")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--render-timeout", dest="render_timeout", type=float,
                        default=120.0,
                        help="seconds before a single Inkscape render is "
                             "killed and treated as a failure (default: 120)")
    parser.add_argument("--width", type=int, default=2160)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--overwrite", action="store_true",
                        help="re-render PNGs that already exist")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="report the plan and sample paths, render nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many images in this run (0 = no limit)")
    parser.add_argument("--shard", default="",
                        help="render only shard i/n of the combination space")
    parser.add_argument("--start-index", dest="start_index", type=int, default=None,
                        help="first combination index (default 0, or set by --shard)")
    parser.add_argument("--end-index", dest="end_index", type=int, default=None,
                        help="stop before this combination index (default: all)")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    inkscape = args.inkscape or shutil.which("inkscape") or shutil.which("inkscape.exe")
    if not inkscape:
        inkscape = r"C:\Program Files\Inkscape\bin\inkscape.exe"
    if not Path(inkscape).exists():
        print("Inkscape not found: {}".format(inkscape), file=sys.stderr)
        return 1

    src_dir = Path(args.src_dir) if args.src_dir else script_dir / SOURCE_DIR_NAME
    if not src_dir.is_dir():
        print("Source directory not found: {}".format(src_dir), file=sys.stderr)
        return 1

    try:
        animals = load_animals(src_dir, args.animal)
        first_colors = parse_color_list(args.first_colors, "first color")
        second_colors = parse_color_list(args.second_colors, "second color")
    except SystemExit as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    # Cache each source's text and <use> count once. The same file is recolored
    # once per color pair, and re-reading it 400 times per animal is pure waste.
    sources = {}
    for name, path in animals:
        text = path.read_text(encoding="utf-8")
        slots = count_uses(text)
        if slots != EXPECTED_USES:
            print("Skipping {}: expected {} <use> tags, found {}".format(
                path.name, EXPECTED_USES, slots), file=sys.stderr)
            continue
        sources[name] = (text, slots)
    animals = [(name, path) for name, path in animals if name in sources]
    if not animals:
        print("No usable sources in {}".format(src_dir), file=sys.stderr)
        return 1

    combos = build_combos(animals, first_colors, second_colors,
                          args.distinct_only)
    total = len(combos)

    start = 0
    end = total
    if args.shard:
        try:
            start, end = parse_shard(args.shard, total)
        except argparse.ArgumentTypeError as exc:
            print("Error: {}".format(exc), file=sys.stderr)
            return 1
    if args.start_index is not None:
        start = max(0, args.start_index)
    if args.end_index is not None:
        end = min(total, args.end_index)
    if args.limit:
        end = min(end, start + args.limit)
    end = max(start, end)
    planned = combos[start:end]

    out_base = Path(args.out_dir) if args.out_dir else script_dir
    out_name = args.out_name.strip() or "animal-pictures"
    out = out_base / (out_name + suffix_for(args.background))

    print("Source:   {} animals from {}".format(len(animals), src_dir))
    print("Slots:    2 per animal (upright, mirrored)")
    print("Palette:  {} first x {} second".format(
        len(first_colors), len(second_colors)))
    print("Total:    {} combinations ({} animals x {} x {})".format(
        total, len(animals), len(first_colors), len(second_colors)))
    print("Range:    [{}, {}) -> {} images".format(start, end, len(planned)))
    print("Size:     {}x{} on {}".format(
        args.width, args.height, args.background))
    print("Output:   {}".format(
        "{}/<animal>/<first>/<animal>_<first>_<second>.png".format(out.name)))
    if args.distinct_only:
        print("Note:     --distinct-only skips matching-color combinations.")

    if args.dry_run:
        if planned:
            picks = sorted({planned[0],
                            planned[len(planned) // 2],
                            planned[-1]})
            for combo in picks:
                print("  {}".format(png_path(out, *combo)))
        print("Dry run: nothing rendered.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(args.tmp_dir) if args.tmp_dir else Path(tempfile.gettempdir())
    tmp = tmp_root / "bg-images" / "animal-gen-{}".format(os.getpid())
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    base_args = (
        ["--export-type=png",
         "--export-width={}".format(args.width),
         "--export-height={}".format(args.height)]
        + inkscape_args(args.background)
    )

    done = rendered = skipped = 0
    failures = []
    started = time.time()
    last_report = started
    batch_size = max(1, args.concurrency) * 4

    for i in range(0, len(planned), batch_size):
        batch = planned[i:i + batch_size]
        b_rendered, b_skipped, b_failures = process_batch(
            batch, sources, out, tmp, base_args, inkscape,
            args.concurrency, args.overwrite, args.render_timeout,
        )
        rendered += b_rendered
        skipped += b_skipped
        failures.extend(b_failures)
        done += len(batch)
        clean_tmp(tmp)

        now = time.time()
        if now - last_report >= PROGRESS_INTERVAL:
            last_report = now
            elapsed = now - started or 1.0
            rate = done / elapsed
            remaining = len(planned) - done
            eta = remaining / rate if rate > 0 else float("inf")
            print("{}/{} rendered  {:.1f}/s  eta {}".format(
                done, len(planned), rate,
                time.strftime("%H:%M:%S", time.gmtime(eta)) if rate > 0 else "?",
            ), flush=True)

    if failures:
        print("Retrying {} failed renders...".format(len(failures)), flush=True)
        still_missing = []
        chunk = max(1, args.concurrency) * 4
        for i in range(0, len(failures), chunk):
            _r, _s, missed = process_batch(
                [combo for combo, _e in failures[i:i + chunk]], sources, out, tmp,
                base_args, inkscape, 2, args.overwrite,
                args.render_timeout * 2,
            )
            still_missing.extend(missed)
        failures = still_missing
        clean_tmp(tmp)

    shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.time() - started
    if failures:
        print("Warning: {} PNGs still missing after retry".format(len(failures)),
              file=sys.stderr)
        for combo, error in failures[:20]:
            print("  {} ({})".format(png_path(out, *combo), error), file=sys.stderr)
        if len(failures) > 20:
            print("  ... and {} more".format(len(failures) - 20), file=sys.stderr)
    print("Done. {} rendered, {} already present, {} failed in {:.0f}s -> {}".format(
        rendered, skipped, len(failures), elapsed, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
