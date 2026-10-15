# Depth Map Plugin

Creates a depth map layer from the active layer using
[Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2),
running in-process (the `upscale.py` pattern).

## Usage

1. Open an image in GIMP
2. Go to Filters → AI → Depth Map...
3. Pick a variant (Small / Base / Large), an output (grayscale / colored /
   both), and a max side resolution
4. Click OK — the plugin finds an existing `depth_anything_v2_vit*.pth`
   checkpoint (via `locate` and common model directories), or downloads the
   chosen variant into `~/.cache/gimp-depthmap/`, then adds the depth map as a
   new layer above the current layer

## Dependencies

```shell
pip install --no-deps depth-anything-v2 torch pillow
pip install opencv-python matplotlib   # only if not already installed
```

`depth-anything-v2` pulls in `einops`, `numpy`, and `opencv-python`. Install
with `--no-deps` when your torch/torchvision is newer than the package's pinned
combination (it pins `torchvision<0.23`, which conflicts with recent torch
releases); the plugin needs `torch`, `torchvision`, `cv2`, and `matplotlib` at
runtime.

## Checkpoints

The plugin discovers `depth_anything_v2_vits.pth` / `_vitb.pth` / `_vitl.pth`
(a.k.a. Small ~98 MB, Base ~390 MB, Large ~1.3 GB) in:

- `~/models`, `~/Downloads`, `~/Downloads/src/ComfyUI/models`, `/opt/models`
- `~/.cache/gimp-depthmap/` (its own download cache)
- Any path returned by `locate -r 'depth_anything_v2_vit[bsl]\.pth'`

Explicit paths entered in the dialog always win. Depth Anything V1 checkpoints
(`depth_anything_vitl14.pth`) are not supported. To download a checkpoint
manually, use the Hugging Face CLI, e.g.:

```shell
hf download depth-anything/Depth-Anything-V2-Small \
  --local-dir ~/.cache/gimp-depthmap
```

## Menu

`<Image>/Filters/AI/` — Depth Map...