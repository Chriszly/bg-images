#!/usr/bin/env python3
"""Render every color combination of a multi-path SVG to PNGs using Inkscape.

By default every ``<path>`` in the source SVG is recolored with one color from
the palette, so the total number of outputs is ``len(palette) ** path_count``.
For the 20-color palette and the 5 paths of ``robot_building_arm_line_art2.svg``
that is 20 ** 5 = 3,200,000 images.

``--sweep`` narrows that down the way ``generate-bear-pngs.py`` works: only the
listed slots (1-based, document order) vary across the palette, and every other
slot is given a color derived from a hash of the swept colors. A given pair
therefore always maps to the same full combination, so the run stays resumable
and reproducible while the other paths still look randomly colored.
``--sweep 1,4`` on a 5-path SVG yields 20 ** 2 = 400 images.

Because the full space is far too many to build up front, this script streams
the combination space in index order, rendering one bounded batch at a time, and
resumes by skipping PNGs that already exist. Use ``--shard i/n`` to split the
work across machines or across several days.

Path order (document order) for the robot artwork:
    1. industrial robot arm + pedestal
    2. crate the mascot stands on
    3. wrench and tightening motion marks
    4. mascot body, head, visor, arms, legs
    5. mascot face (smile and eyes)

Output filenames always encode all slots in document order, swept and derived
alike, so an image can be traced back to its palette entry:
    robot_<path1>_<path2>_<path3>_<path4>_<path5>.png
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

PALETTE = [
    "ffffff", "7a7a7a", "ff0000", "ff7a00", "ff007a", "ff7a7a",
    "00ff00", "7aff00", "00ff7a", "7aff7a",
    "0000ff", "7a00ff", "007aff", "7a7aff",
    "00ffff", "7affff", "ff00ff", "ff7aff", "ffff00", "ffff7a",
]

BLACK = "000000"

# A <path> start tag. The alternation lets the pattern skip over quoted
# attribute values so that a '>' inside one cannot end the match early.
PATH_TAG_RE = re.compile(r"<path\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*?/?>", re.IGNORECASE)
STROKE_ATTR_RE = re.compile(r"\s+stroke\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
STROKE_WIDTH_ATTR_RE = re.compile(
    r"\s+stroke-width\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE
)
D_ATTR_RE = re.compile(r"\sd\s*=\s*\"([^\"]{0,48})", re.IGNORECASE)

PROGRESS_INTERVAL = 10.0  # seconds between progress lines


def suffix_for(background):
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
    if lower == "black":
        return ["--export-background=black", "--export-background-opacity=1"]
    if lower == "white":
        return ["--export-background=white", "--export-background-opacity=1"]
    if lower == "transparent":
        return []
    return ["--export-background=#" + lower, "--export-background-opacity=1"]


def swap_palette(colors, swap_color):
    if not swap_color:
        return colors
    swap = swap_color.lower()
    if swap not in colors:
        print(
            "Warning: swap color {!r} not present in palette".format(swap),
            file=sys.stderr,
        )
    return [BLACK if c == swap else c for c in colors]


def count_paths(svg_text):
    """Number of <path> elements, which is the number of color slots."""
    return len(PATH_TAG_RE.findall(svg_text))


HASH_MOD = 2147483647  # Mersenne prime
HASH_BASE = 131
MIX_1 = 0x2C1B3C6D
MIX_2 = 0x297A2D39


def poly_hash(text):
    """A polynomial rolling hash over the code points of ``text``.

    The trailing ``mix`` is not optional. A rolling hash is linear in its last
    character, so keying the derived slots as ``"<swept>|<slot>"`` made slot 3
    always land exactly one palette step after slot 2, and slot 5 three steps
    after it -- the three "random" paths came out as one fixed offset triple.
    The multiply/xorshift finalizer destroys that linearity.

    Written out by hand, and kept in int64 range at every step, so that
    generate-robot-pngs.ps1 can reproduce it exactly. That agreement matters: a
    run split across the two scripts has to derive the same colors, or the same
    filename would mean two different images. hash() cannot be used because it
    is salted per process, so every run would disagree with the last.
    """
    h = 0
    for char in text:
        h = (h * HASH_BASE + ord(char)) % HASH_MOD
    h = ((h ^ (h >> 15)) * MIX_1) % HASH_MOD
    h = ((h ^ (h >> 12)) * MIX_2) % HASH_MOD
    return h ^ (h >> 15)


def derive_color(colors, swept, slot):
    """Pick a stable pseudo-random palette entry for a non-swept slot.

    The swept colors are excluded from the candidate set, so a derived path can
    never come out the same color as one of the two main paths. That leaves
    ``len(colors) - 2`` candidates (19 if the two main paths share a color).

    The key is the swept colors in slot order plus the 1-based number of the slot
    being filled, so the result depends only on the swept colors -- not on the
    iteration order, the shard, or the process. That is what keeps a resumed or
    sharded run producing exactly the images the earlier run would have produced,
    and it is also what makes the two-color filename in ``png_path`` sufficient:
    the main colors identify the image, because everything else follows from them.
    """
    banned = set(swept)
    candidates = [c for c in colors if c not in banned]
    if not candidates:
        candidates = list(colors)
    key = "{}|{}".format("|".join(swept), slot)
    return candidates[poly_hash(key) % len(candidates)]


def combo_for(index, colors, slots, sweep_slots):
    """Map a combination index to one color per slot, most significant first.

    ``sweep_slots`` are 1-based ascending (see ``parse_sweep``) and consume
    index digits; the remaining slots are derived from those. Consecutive
    indices therefore vary the *last* swept slot, which gives the finest
    granularity when a run is split into shards or interrupted. Ascending order
    matters: it fixes which slot the low index digits land on, so the same index
    means the same image no matter how the slots were listed on the command line.
    """
    base = len(colors)
    out = [None] * slots
    value = index
    for pos in range(len(sweep_slots) - 1, -1, -1):
        slot = sweep_slots[pos]
        out[slot - 1] = colors[value % base]
        value //= base
    if len(sweep_slots) == slots:
        return out
    swept = [out[slot - 1] for slot in sweep_slots]
    for slot in range(slots):
        if out[slot] is None:
            out[slot] = derive_color(colors, swept, slot + 1)
    return out


def set_attr(tag, attr_re, name, value):
    """Set ``name="value"`` on a start tag, replacing or injecting as needed."""
    rendered = ' {}="{}"'.format(name, value)
    if attr_re.search(tag):
        return attr_re.sub(lambda _m, r=rendered: r, tag, count=1)
    if tag.endswith("/>"):
        return tag[:-2] + rendered + "/>"
    return tag[:-1] + rendered + ">"


def parse_slot(spec, slots):
    """Parse a single 1-based slot number."""
    try:
        number = int(spec.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            "slot must be a single number, e.g. 4"
        )
    if not 1 <= number <= slots:
        raise argparse.ArgumentTypeError(
            "slot {} out of range; the source has {} paths".format(number, slots)
        )
    return number


def png_path(out, prefix, combo, sweep_slots, group_slot=None):
    """Where a combination's PNG lives.

    The name carries only the swept colors, in slot order, because the derived
    colors are a pure function of those: the main colors identify the image
    completely, so a five-color filename would just be noise. With no ``--sweep``
    every slot is swept and this is unchanged.

    With ``group_slot`` the image goes into a subdirectory named after that
    slot's color, which is how the 400 mascot variants get filed under their
    mascot color. The resume check and the render target must agree, so both go
    through here.
    """
    key = "_".join(combo[slot - 1] for slot in sweep_slots)
    name = "{}_{}.png".format(prefix, key)
    if group_slot:
        return out / combo[group_slot - 1] / name
    return out / name


def build_svg(source_text, combo, stroke_width=None):
    """Return the source SVG with slot N's stroke recolored to combo[N].

    Paths are recolored by setting an explicit stroke on every <path>. Some
    paths in the source carry no stroke attribute of their own and inherit
    #000 from the enclosing group, so the attribute is injected when missing
    rather than only substituted when present.

    ``stroke_width`` overrides the stroke-width of every path, for artwork that
    should be rendered at one consistent weight regardless of the source values.
    """
    parts = []
    pos = 0
    for slot, match in enumerate(PATH_TAG_RE.finditer(source_text)):
        tag = set_attr(match.group(0), STROKE_ATTR_RE, "stroke", "#" + combo[slot])
        if stroke_width is not None:
            tag = set_attr(tag, STROKE_WIDTH_ATTR_RE, "stroke-width", stroke_width)
        parts.append(source_text[pos:match.start()])
        parts.append(tag)
        pos = match.end()
    parts.append(source_text[pos:])
    return "".join(parts)


def render(svg_path, png_path, base_args, inkscape):
    result = subprocess.run(
        [inkscape]
        + base_args
        + ["--export-filename={}".format(png_path), str(svg_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and png_path.exists()


def process_batch(batch, source_text, out, tmp, base_args, inkscape,
                  concurrency, overwrite, prefix, stroke_width=None,
                  group_slot=None, sweep_slots=None):
    """Render one batch. Returns (rendered, skipped, failures)."""
    def task(combo):
        png = png_path(out, prefix, combo, sweep_slots, group_slot)
        if png.exists() and not overwrite:
            return (combo, None, True)
        svg = tmp / ("work_{}.svg".format("_".join(combo)))
        try:
            png.parent.mkdir(parents=True, exist_ok=True)
            svg.write_text(
                build_svg(source_text, combo, stroke_width), encoding="utf-8"
            )
        except OSError as exc:
            return (combo, str(exc), False)
        if render(svg, png, base_args, inkscape):
            return (combo, None, False)
        return (combo, "render failed", False)

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
            failures.append(combo)
    return rendered, skipped, failures


def describe_paths(svg_text, sweep_slots=None):
    for i, match in enumerate(PATH_TAG_RE.finditer(svg_text)):
        slot = i + 1
        d = D_ATTR_RE.search(match.group(0))
        snippet = d.group(1).strip() if d else "(no d attribute)"
        role = ""
        if sweep_slots is not None:
            role = " [swept]" if slot in sweep_slots else " [derived]"
        print("  slot {}{}: d=\"{}{}\"".format(slot, role, snippet, "..."))


def parse_shard(spec, total):
    """Parse 'i/n' into a contiguous [start, end) range of the index space."""
    try:
        part, _, count = spec.partition("/")
        index = int(part)
        parts = int(count)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "shard must look like i/n, e.g. 0/8"
        )
    if parts <= 0 or index < 0 or index >= parts:
        raise argparse.ArgumentTypeError(
            "shard index must be 0..n-1 with n > 0"
        )
    size = -(-total // parts)  # ceiling division
    start = index * size
    return start, min(start + size, total)


def parse_sweep(spec, slots):
    """Parse a comma-separated slot list into sorted 1-based slot numbers."""
    parts = spec.replace(" ", "").split(",")
    if not all(parts):
        raise argparse.ArgumentTypeError(
            "sweep must be a comma-separated list of slot numbers, e.g. 1,4"
        )
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(
            "sweep slot {!r} is not a number".format(
                next(p for p in parts if not p.lstrip("-").isdigit())
            )
        )
    out = []
    for number in numbers:
        if not 1 <= number <= slots:
            raise argparse.ArgumentTypeError(
                "sweep slot {} out of range; the source has {} paths".format(
                    number, slots
                )
            )
        if number not in out:
            out.append(number)
    return sorted(out)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--background", default="black",
                        help="black, white, transparent, or a hex color")
    parser.add_argument("--swap-color", dest="swap_color", default="",
                        help="palette color to replace with black")
    parser.add_argument("--inkscape", default="",
                        help="path to inkscape.exe")
    parser.add_argument("--source", default="",
                        help="source SVG (default: robot_building_arm_line_art2.svg)")
    parser.add_argument("--out-dir", dest="out_dir", default="",
                        help="output base directory (default: script dir)")
    parser.add_argument("--out-name", dest="out_name", default="",
                        help="name of the output directory itself, e.g. robot-combo "
                             "(default: <prefix>-pngs, plus a background suffix)")
    parser.add_argument("--tmp-dir", dest="tmp_dir", default="",
                        help="scratch directory (default: temp dir)")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--width", type=int, default=2160)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--overwrite", action="store_true",
                        help="re-render PNGs that already exist")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="report the plan and sample filenames, render nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many images in this run (0 = no limit)")
    parser.add_argument("--shard", default="",
                        help="render only shard i/n of the combination space")
    parser.add_argument("--start-index", dest="start_index", type=int, default=None,
                        help="first combination index (default 0, or set by --shard)")
    parser.add_argument("--end-index", dest="end_index", type=int, default=None,
                        help="stop before this combination index (default: all)")
    parser.add_argument("--sweep", default="",
                        help="comma-separated 1-based path numbers to sweep over the "
                             "palette, e.g. '1,4'; remaining paths are given a "
                             "deterministic derived color. Default: sweep every path.")
    parser.add_argument("--stroke-width", dest="stroke_width", default="",
                        help="override the stroke-width of every path, e.g. 3 "
                             "(default: keep the source stroke-width)")
    parser.add_argument("--group-by", dest="group_by", default="",
                        help="file each PNG into a subdirectory named after the "
                             "color of this 1-based slot, e.g. 4 to group the "
                             "mascot variants by mascot color (default: flat)")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    inkscape = args.inkscape or shutil.which("inkscape") or shutil.which("inkscape.exe")
    if not inkscape:
        inkscape = r"C:\Program Files\Inkscape\bin\inkscape.exe"
    if not Path(inkscape).exists():
        print("Inkscape not found: {}".format(inkscape), file=sys.stderr)
        return 1

    src = Path(args.source) if args.source else script_dir / "robot_building_arm_line_art2.svg"
    if not src.exists():
        print("Source SVG not found: {}".format(src), file=sys.stderr)
        return 1

    source_text = src.read_text(encoding="utf-8")
    slots = count_paths(source_text)
    if slots == 0:
        print("No <path> elements in {}".format(src), file=sys.stderr)
        return 1

    colors = swap_palette(PALETTE, args.swap_color)
    base = len(colors)

    sweep_slots = list(range(1, slots + 1))
    if args.sweep:
        try:
            sweep_slots = parse_sweep(args.sweep, slots)
        except argparse.ArgumentTypeError as exc:
            print("Error: {}".format(exc), file=sys.stderr)
            return 1

    stroke_width = args.stroke_width.strip() or None

    group_slot = None
    if args.group_by:
        try:
            group_slot = parse_slot(args.group_by, slots)
        except argparse.ArgumentTypeError as exc:
            print("Error: {}".format(exc), file=sys.stderr)
            return 1

    total = base ** len(sweep_slots)

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

    out_base = Path(args.out_dir) if args.out_dir else script_dir
    # A custom --source gets its own output folder and filename prefix, so that
    # two artworks with the same path structure (e.g. line_art2 and line_art3)
    # cannot silently overwrite each other's identically named PNGs.
    prefix = "robot" if not args.source else src.stem
    # --out-name pins the folder itself, e.g. to collect every robot set in one
    # local directory. The background suffix is dropped in that case, so two
    # backgrounds would land in the same folder; add it yourself if that matters.
    out_name = args.out_name.strip() or (
        prefix + "-pngs" + suffix_for(args.background))
    out = out_base / out_name

    print("Source:   {}".format(src))
    print("Paths:    {} color slots".format(slots))
    describe_paths(source_text, sweep_slots)
    print("Palette:  {} colors".format(base))
    print("Swept:    {}{}".format(
        ",".join(str(s) for s in sweep_slots),
        " (all paths)" if len(sweep_slots) == slots else "",
    ))
    print("Total:    {} combinations ({} ** {})".format(
        total, base, len(sweep_slots)))
    print("Range:    [{}, {}) -> {} images".format(start, end, end - start))
    print("Width:    stroke-width {}".format(
        stroke_width if stroke_width is not None else "(from source)"))
    print("Grouping: {}".format(
        "subdirectory per slot-{} color".format(group_slot) if group_slot
        else "flat (no subdirectories)"))
    print("Output:   {}_{}.png".format(
        prefix, "_".join("<slot{}>".format(s) for s in sweep_slots)))
    if total > 10000:
        print("Note:     this is a large run; consider --shard i/n across "
              "machines, and --dry-run to preview.")

    if args.dry_run:
        if end > start:
            for index in (start, min(start + 1, end - 1), end - 1):
                combo = combo_for(index, colors, slots, sweep_slots)
                print("  index {} -> {}".format(
                    index, png_path(out, prefix, combo, sweep_slots, group_slot)))
        print("Dry run: nothing rendered.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(args.tmp_dir) if args.tmp_dir else Path(tempfile.gettempdir())
    tmp = tmp_root / "bg-images" / "robot-gen-{}".format(os.getpid())
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    base_args = (
        ["--export-type=png",
         "--export-width={}".format(args.width),
         "--export-height={}".format(args.height)]
        + inkscape_args(args.background)
    )

    planned = end - start
    batch_size = max(1, args.concurrency) * 4
    done = rendered = skipped = 0
    failures = []
    started = time.time()
    last_report = started

    index = start
    while index < end:
        batch = []
        for _ in range(batch_size):
            if index >= end:
                break
            batch.append(combo_for(index, colors, slots, sweep_slots))
            index += 1

        b_rendered, b_skipped, b_failures = process_batch(
            batch, source_text, out, tmp, base_args, inkscape,
            args.concurrency, args.overwrite, prefix, stroke_width, group_slot,
            sweep_slots,
        )
        rendered += b_rendered
        skipped += b_skipped
        failures.extend(b_failures)
        done += len(batch)

        for svg in tmp.glob("work_*.svg"):
            try:
                svg.unlink()
            except OSError:
                pass

        now = time.time()
        if now - last_report >= PROGRESS_INTERVAL:
            last_report = now
            elapsed = now - started or 1.0
            rate = done / elapsed
            remaining = planned - done
            eta = remaining / rate if rate > 0 else float("inf")
            print("{}/{} rendered  {:.1f}/s  eta {}".format(
                done, planned, rate,
                time.strftime("%H:%M:%S", time.gmtime(eta)) if rate > 0 else "?",
            ))

    if failures:
        print("Retrying {} failed renders...".format(len(failures)))
        retry_failures = []
        chunk = max(1, args.concurrency) * 4
        for i in range(0, len(failures), chunk):
            _r, _s, missed = process_batch(
                failures[i:i + chunk], source_text, out, tmp, base_args,
                inkscape, 2, args.overwrite, prefix, stroke_width, group_slot,
                sweep_slots,
            )
            retry_failures.extend(missed)
        failures = retry_failures
        for svg in tmp.glob("work_*.svg"):
            try:
                svg.unlink()
            except OSError:
                pass

    shutil.rmtree(tmp, ignore_errors=True)

    elapsed = time.time() - started
    if failures:
        print("Warning: {} PNGs still missing after retry".format(len(failures)),
              file=sys.stderr)
        for combo in failures[:20]:
            print("  {}".format(
                png_path(out, prefix, combo, sweep_slots, group_slot)),
                file=sys.stderr)
        if len(failures) > 20:
            print("  ... and {} more".format(len(failures) - 20), file=sys.stderr)
    print("Done. {} rendered, {} already present, {} failed in {:.0f}s -> {}".format(
        rendered, skipped, len(failures), elapsed, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
