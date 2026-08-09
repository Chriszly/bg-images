# bg-images

Collection of SVG background images and the tooling used to render them into PNGs at scale.

## What happens here

This repo takes a layered SVG (`bear -split.svg`) and renders it into hundreds of color variants as 2160×2160 PNGs using [Inkscape](https://inkscape.org).

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

## Requirements

- Windows
- Python 3.7+ (uses only the standard library)
- [Inkscape](https://inkscape.org) (found on PATH, or pass `--inkscape` with the full path).

## License

See [LICENSE](LICENSE).
