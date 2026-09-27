#!/usr/bin/env python3
"""Render all 20x20 color combinations of a layered SVG to PNGs using Inkscape."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PALETTE = [
    "ffffff", "7a7a7a", "ff0000", "ff7a00", "ff007a", "ff7a7a",
    "00ff00", "7aff00", "00ff7a", "7aff7a",
    "0000ff", "7a00ff", "007aff", "7a7aff",
    "00ffff", "7affff", "ff00ff", "ff7aff", "ffff00", "ffff7a",
]

BLACK = "000000"


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


def build_jobs(source_svg, colors, tmp_dir):
    jobs = []
    idx = 0
    for left in colors:
        for right in colors:
            svg_tmp = tmp_dir / ("work_{}.svg".format(idx))
            content = source_svg.read_text(encoding="utf-8")
            content = content.replace("#00ffff", "#" + left).replace(
                "#ff7a00", "#" + right
            )
            svg_tmp.write_text(content, encoding="utf-8")
            jobs.append((svg_tmp, left, right))
            idx += 1
    return jobs


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


def render(job, timeout):
    """Render one job. False on failure, non-zero exit, or timeout.

    The timeout is not optional. Inkscape occasionally wedges on a PNG export
    and never exits; because a batch is drained as a unit, one stuck render
    would otherwise block its whole batch -- and every later batch -- with no
    output and no error, which is exactly how a run appears to hang. With a
    timeout the hang becomes an ordinary failure that the retry pass picks up,
    and a genuinely wedged image is reported by name instead of stalling the
    run indefinitely.
    """
    svg, left, right, png, base_args, inkscape = job
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


def run_batch(jobs, out, concurrency, base_args, inkscape, timeout):
    def task(job):
        svg, left, right = job
        png = out / "bear_{}_{}.png".format(left, right)
        return render((svg, left, right, png, base_args, inkscape), timeout)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(task, jobs))
    failures = [job for job, ok in zip(jobs, results) if not ok]
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", default="black",
                        help="black, white, transparent, or a hex color")
    parser.add_argument("--swap-color", dest="swap_color", default="",
                        help="palette color to replace with black")
    parser.add_argument("--inkscape", default="",
                        help="path to inkscape.exe")
    parser.add_argument("--source", default="",
                        help="path to the source SVG (default: script dir)")
    parser.add_argument("--out-dir", dest="out_dir", default="",
                        help="output base directory (default: script dir)")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--render-timeout", dest="render_timeout", type=float,
                        default=120.0,
                        help="seconds before a single Inkscape render is "
                             "killed and treated as a failure (default: 120)")
    parser.add_argument("--width", type=int, default=2160)
    parser.add_argument("--height", type=int, default=2160)
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    inkscape = args.inkscape or shutil.which("inkscape") or shutil.which("inkscape.exe")
    if not inkscape:
        inkscape = r"C:\Program Files\Inkscape\bin\inkscape.exe"
    if not Path(inkscape).exists():
        print("Inkscape not found: {}".format(inkscape), file=sys.stderr)
        return 1

    src = Path(args.source) if args.source else script_dir / "bear -split.svg"
    if not src.exists():
        print("Source SVG not found: {}".format(src), file=sys.stderr)
        return 1

    out_base = Path(args.out_dir) if args.out_dir else script_dir
    out = out_base / ("bear-pngs" + suffix_for(args.background))
    out.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.gettempdir()) / "bg-images" / "bear-gen"
    tmp.mkdir(parents=True, exist_ok=True)

    colors = swap_palette(PALETTE, args.swap_color)

    base_args = (
        ["--export-type=png",
         "--export-width={}".format(args.width),
         "--export-height={}".format(args.height)]
        + inkscape_args(args.background)
    )

    jobs = build_jobs(src, colors, tmp)
    total = len(jobs)
    done = 0
    failures = []

    for i in range(0, total, args.concurrency):
        batch = jobs[i:i + args.concurrency]
        missing = run_batch(batch, out, args.concurrency, base_args, inkscape,
                            args.render_timeout)
        failures.extend(missing)
        done += len(batch)
        print("{}/{}".format(done, total))

    if failures:
        print("Retrying {} failed renders...".format(len(failures)))
        # A render that already timed out once gets longer on the retry, since
        # some wedges are only a slow first run rather than a true deadlock.
        missing = run_batch(failures, out, 2, base_args, inkscape,
                            args.render_timeout * 2)
        failures = missing

    shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("Warning: {} PNGs still missing:".format(len(failures)),
              file=sys.stderr)
        for _svg, _left, _right in failures:
            print("  work_{}_{}.png".format(_left, _right), file=sys.stderr)
        print("Done. {}/{} PNGs in {}".format(done - len(failures), total, out))
    else:
        print("Done. {} PNGs in {}".format(done, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())