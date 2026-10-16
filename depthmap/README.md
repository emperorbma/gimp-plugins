# Depth Map Plugin

Creates a depth map layer from the active layer using
[Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2),
running in-process (the `upscale.py` pattern).

## Usage

1. Open an image in GIMP
2. Go to Filters → AI → Depth Map...
3. Pick a variant (Small / Base / Large), an output (grayscale / colored /
   both), and a max side resolution
4. Click OK — the plugin resolves a `depth_anything_v2_vit*.pth` checkpoint
   from the Hugging Face cache (auto-adopting any model found on disk into
   the cache, or downloading the chosen variant via `huggingface_hub`), then
   adds the depth map as a new layer above the current layer

## 3D photo export (Facebook)

Tick **Export as 3D photo (Facebook)** to also publish the result as a
[Facebook 3D photo](https://www.facebook.com/help/www/414295416095269).
After the dialog, GIMP asks for a name and location, and once the depth map is
finished it writes two JPEGs:

- `<name>.jpg` — the source layer (alpha composited onto white)
- `<name>_depth.jpg` — the grayscale depth map (white = near, black = far)

Upload both files together to Facebook to get a 3D photo. If the parallax
looks inverted, rerun with **Invert (for lens blur)** enabled. The export uses
the grayscale depth map even when the on-canvas output is set to colored.

## Dependencies

```shell
pip install --no-deps depth-anything-v2 torch pillow huggingface_hub
pip install opencv-python matplotlib   # only if not already installed
```

`depth-anything-v2` pulls in `einops`, `numpy`, and `opencv-python`. Install
with `--no-deps` when your torch/torchvision is newer than the package's pinned
combination (it pins `torchvision<0.23`, which conflicts with recent torch
releases); the plugin needs `torch`, `torchvision`, `cv2`, `matplotlib`, and
`huggingface_hub` (for downloading checkpoints) at runtime.

## Checkpoints

The plugin locates `depth_anything_v2_vits.pth` / `_vitb.pth` / `_vitl.pth`
(a.k.a. Small ~98 MB, Base ~390 MB, Large ~1.3 GB) entirely through
`huggingface_hub`. It resolves a checkpoint in this order:

1. **The Hugging Face cache** (`~/.cache/huggingface/hub`, honoring
   `HF_HOME`), where the plugin downloads checkpoints via `huggingface_hub`
2. **On disk** — any matching checkpoint found in `~/models`, `~/Downloads`,
   `~/Downloads/src/ComfyUI/models`, `/opt/models`,
   `~/.cache/gimp-depthmap/` (legacy), or returned by
   `locate -r 'depth_anything_v2_vit[bsl]\.pth'` is **silently adopted into
   the Hugging Face cache** (linked in place where the filesystem allows),
   so the hub manages it from then on and the user never has to specify a
   location
3. **Download** — from the corresponding `depth-anything/Depth-Anything-V2-*`
   repository (if "Download if missing" is enabled)

Depth Anything V1 checkpoints (`depth_anything_vitl14.pth`) are not
supported. To download a checkpoint manually, use the Hugging Face CLI, e.g.:

```shell
hf download depth-anything/Depth-Anything-V2-Small
```

## Menu

`<Image>/Filters/AI/` — Depth Map...

## Stereo views (left/right eyes + anaglyph)

**Filters → AI → Stereo Views...** renders left and right eye views of the
scene using depth-image-based rendering (DIBR), reusing the same Depth
Anything V2 model as Depth Map:

- **Parallax (px)** — maximum horizontal shift between the eyes; larger =
  stronger 3D. 20–40 suits typical resolutions.
- **Create** — opens a **new GIMP tab** (the current image is never modified
  or resized): *Side-by-side (new tab)* builds a single layer with the left
  eye on the left half and the right eye on the right half (viewable as
  cross-eyed/parallel stereo, or as a wiggle animation), *Red-cyan anaglyph
  (new tab)* produces a single layer for red/cyan glasses, and *Both* opens
  both tabs.

The warp is a forward mapping with a z-buffer (the nearest pixel wins where
several sources collide, matching real occlusion), and exposed background is
filled directionally from the side it was uncovered on. The depth map is
blurred slightly beforehand, which shrinks disocclusion holes and removes
jaggy depth edges. Implementation lives in `dibr.py` (pure numpy + PIL, no
extra dependencies), also usable standalone:

```python
from dibr import make_stereo
left, right = make_stereo(color_pil, depth_pil, base=30.0)
```