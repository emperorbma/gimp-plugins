# Depth Map Plugin — Design Spec

Date: 2026-10-14
Status: Approved (brainstorming session)

## Overview

A new GIMP 3 plugin, `depthmap/`, that creates a depth map layer from the
current layer using Depth Anything V2. It runs in-process (the `upscale.py`
pattern), auto-discovers local `depth_anything_v2_vit*.pth` checkpoints via
`locate`/glob, offers Small/Base/Large variants, auto-downloads the selected
variant when missing, and adds the depth map as a new layer above the source.
Quality and speed matter; total requirements stay well under 4 GiB.

## Decisions (from Q&A)

- **Model:** Depth Anything V2 only, via the official `depth_anything_v2`
  PyPI package (not `transformers`). This is the only route that natively loads
  the raw `depth_anything_v2_vit*.pth` checkpoints. V1 checkpoints
  (`depth_anything_vitl14.pth`) are out of scope — the discovery pattern
  excludes them by name.
- **Variants:** Small (`vits`), Base (`vitb`), Large (`vitl`). Giant (`vitg`,
  ~4 GB) excluded to honor the 4 GiB budget.
- **Architecture:** In-process like `upscale.py` (model cached in a global
  dict). No separate server, no subprocess CLI.
- **Model source:** Smart discovery — `locate` regex plus glob-walk of a
  curated directory list, a manual-path escape hatch, and auto-download into a
  cache dir when the chosen variant is missing.
- **Output:** Dialog toggle — Grayscale (default) / Colored (inferno) / Both.
- **Scope:** Processes the active layer only, at its canvas position; original
  layer stays visible with the depth layer inserted above it.

## Package layout

```
depthmap/
├── depthmap.py      # single-file plugin (in-process)
├── README.md        # pip install + usage
└── locale/          # empty i18n dir (repo convention)
```

- Procedure `python-fu-depthmap`, menu `<Image>/Filters/AI/_Depth Map...`,
  `set_sensitivity_mask(DRAWABLE | DRAWABLES)`, image types `"RGB*, GRAY*"`.
- Single file mirrors `upscale.py` structure: i18n block, model helpers,
  `MODEL_CACHE`, handler function, `Gimp.PlugIn` subclass, entry point.
- Temp files: `gimp_depthmap_input_<pid>.png`, `gimp_depthmap_output_gray_<pid>.png`,
  `gimp_depthmap_output_color_<pid>.png`, cleaned up in a `finally` block.

## Model variants & discovery

Variant map (encoder config → checkpoint → HF repo → size):

| Variant | Encoder | File | Repo | Size |
|---|---|---|---|---|
| Small | `vits` | `depth_anything_v2_vits.pth` | `depth-anything/Depth-Anything-V2-Small` | ~98 MB |
| Base | `vitb` | `depth_anything_v2_vitb.pth` | `depth-anything/Depth-Anything-V2-Base` | ~390 MB |
| Large | `vitl` | `depth_anything_v2_vitl.pth` | `depth-anything/Depth-Anything-V2-Large` | ~1.3 GB |

The official package constructs the model from an encoder config dict:

```python
MODEL_CONFIGS = {
    'vits': {'encoder': 'vits', 'features': 64,   'out_channels': [48, 96, 192, 384]},
    'vitb': {'encoder': 'vitb', 'features': 128,  'out_channels': [96, 192, 384, 768]},
    'vitl': {'encoder': 'vitl', 'features': 256,  'out_channels': [256, 512, 1024, 1024]},
}
CHECKPOINTS = {
    'vits': 'depth_anything_v2_vits.pth',
    'vitb': 'depth_anything_v2_vitb.pth',
    'vitl': 'depth_anything_v2_vitl.pth',
}
```

**Discovery order at dialog time:**
1. If the `locate` binary exists, run
   `locate -r 'depth_anything_v2_vit[bsl]\.pth'` (suppress stderr).
2. Glob-walk a fixed candidate directory list (depth 3):
   - Linux/macOS: `~/models`, `~/Downloads`, `~/Downloads/src/ComfyUI/models`,
     `/opt/models`, `~/.cache/gimp-depthmap`
   - Windows: `%USERPROFILE%\models`, `%USERPROFILE%\Downloads`,
     `%USERPROFILE%\Downloads\src\ComfyUI\models`
3. Dedupe, keep files that exist and are non-trivial in size.
4. Match each file to its variant via filename suffix (`_vits/_vitb/_vitl`).

The v1 checkpoint `depth_anything_vitl14.pth` is naturally excluded (the regex
requires `depth_anything_v2_vit`).

## Dialog options (`GimpUi.ProcedureDialog`)

Static `Gimp.Choice`/argument entries (no dynamic repopulation):

- **Variant** (choice, default Small): `Small (vits)` / `Base (vitb)` /
  `Large (vitl)`.
- **Model file** (text): optional explicit `.pth` path; overrides discovery
  when non-empty.
- **Download if missing** (toggle, default on): fetch the variant's `.pth` from
  Hugging Face (`https://huggingface.co/<repo>/resolve/main/<file>`) into
  `~/.cache/gimp-depthmap/` using stdlib `urllib` + `ssl`, with progress via
  `Gimp.progress_set_text`.
- **Output** (choice, default Grayscale): `Grayscale` / `Colored (inferno)` /
  `Both`.
- **Max side** (spinner, default 1024, range 256–4096): inference resolution;
  the `height` argument passed to `infer_image` (aspect-preserving). Higher =
  more detail, slower, more RAM.

## Inference pipeline

1. `export_layer_png(image, layer, path)` — the helper from `sd-server.py`
   that renders only the active layer at its canvas position (clears and
   restores an active selection), saved via `Gimp.file_save`.
2. Resolve the model path:
   - explicit dialog path, else
   - discovery match for the chosen variant, else
   - if "Download if missing" is on, download it, else
   - EXECUTION_ERROR naming the exact path the plugin looked for.
3. `MODEL_CACHE` lookup by path; on miss build
   `DepthAnythingV2(**MODEL_CONFIGS[variant])`, `torch.load(path, map_location='cpu')`,
   `.to(device).eval()` where `device = "cuda" if torch.cuda.is_available() else "cpu"`.
   Cache the instance.
4. `depth = model.infer_image(pil_image, height)` where `height` derives from
   the Max side spinner (aspect-preserving). Returns H×W float in 0–255.
   `depth = depth.astype(np.uint8)` for grayscale;
   `apply_colormap(depth)` (from `depth_anything_v2.util.colormap`) for inferno.

## Layer flow

1. Save grayscale result (and colored result, if requested) as temp PNGs via
   Pillow.
2. `Gimp.file_load` each output PNG; re-fetch `drawable = drawables[0]`
   (stale-drawables gotcha).
3. Create canvas-sized `Gimp.Layer.new` layers named
   `Depth (grayscale)` / `Depth (colored)`, mode NORMAL.
4. Copy pixels with `paste_layer_aligned` (the upscale/sd-server helper,
   copied into this file since each plugin is self-contained).
5. Insert above the source layer; keep the source visible;
   `image.active_layer = new depth layer`.
6. `undo_group_start/end`, `context_push/pop`, `Gimp.displays_flush`.

## Error handling

- `torch`/`depth_anything_v2` import missing → `Gimp.message` with the exact
  `pip install` line.
- Download failure (offline, HTTP error) → message naming the URL and the cache
  path, with a manual-download hint.
- Empty drawables, missing output file, failed load → EXECUTION_ERROR with
  clear text (upscale pattern).

## Dependencies & install

```shell
pip install torch depth-anything-v2 pillow
```

`depth-anything-v2` pulls in einops, numpy, opencv-python. Model + torch +
deps stay well under 4 GiB (largest optional model is Large at ~1.3 GB fp32;
Small at ~98 MB is the auto-download default). The root `install.py` will be
updated to include these deps if it enumerates plugin dependencies.

## Verification

- No automated tests in this repo (repo convention).
- Verify: clean `pylsp` diagnostics for `depthmap.py` (only pre-existing-style
  E501/E302 warnings are acceptable), plus a manual GIMP pass: run on a layer,
  confirm grayscale/colored/both outputs, confirm discovery finds
  `depth_anything_v2_vits.pth` on this machine, confirm download fallback.

## Out of scope

- Giant (`vitg`) variant (~4 GB).
- Depth Anything V1 / MiDaS / ControlNet backends.
- Server architecture, CLI wrapper.
- Selection-masked depth (whole layer is processed).
- Normal-map or other derived outputs.
