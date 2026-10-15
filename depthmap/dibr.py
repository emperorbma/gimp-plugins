#!/usr/bin/env python3
"""Depth-Image-Based Rendering (DIBR): generate left/right stereo views and
red-cyan anaglyphs from a single color image and its depth map.

Approach
--------
* Forward mapping (splat): every source pixel is moved horizontally by its
  parallax  (disparity ∝ nearness), and a z-buffer keeps the nearest pixel
  when several land on the same target.  This reproduces real parallax:
  near objects shift most, far objects barely move.
* Disocclusion holes — background exposed where a foreground object has
  been slid away — are filled directionally from the side it was exposed
  on (left edge for the left-eye view, right edge for the right-eye
  view), then smoothed with a short diffusion pass.
* The disparity map is blurred first, which shrinks the holes and removes
  jagged depth edges.

Depth convention
----------------
0 = far, 255 = near (nearer pixels shift the most, like a real camera).

Dependencies
------------
* numpy, PIL (Pillow) — already required by the depthmap plugin.
"""

import numpy as np


# ---------------------------------------------------------------------------
# Core DIBR warp
# ---------------------------------------------------------------------------

def _disparity_map(depth_gray, radius):
    """Return a float32 (H, W) disparity map in [0, 1] (near = 1), with the
    depth smoothed by a Gaussian of `radius` pixels when radius > 0."""
    from PIL import Image, ImageFilter
    depth = Image.fromarray(np.asarray(depth_gray, dtype=np.uint8))
    if radius > 0:
        depth = depth.filter(ImageFilter.GaussianBlur(radius))
    arr = np.asarray(depth, dtype=np.float32)
    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1.0:
        return np.zeros(arr.shape, dtype=np.float32)
    return (arr - lo) / (hi - lo)


def _warp_forward(color_rgb, disp, base, sign):
    """Splat each source pixel to x + disp * base * sign; the nearest source
    wins via a z-buffer (disp = 1 is closest). Returns (warped RGB, holes)."""
    H, W = color_rgb.shape[:2]
    shift = np.rint(disp * base * sign).astype(np.int32)
    ys, xs = np.mgrid[0:H, 0:W].astype(np.int32)
    dst_x = xs + shift
    inside = (dst_x >= 0) & (dst_x < W)
    sy, sx, dx = ys[inside], xs[inside], dst_x[inside]
    vals = color_rgb[sy, sx]
    # Draw nearest first: sort by disparity descending, then keep the first
    # occurrence of each destination pixel.
    order = np.argsort(disp[sy, sx], kind="stable")[::-1]
    dst_flat = sy[order] * W + dx[order]
    uniq, first = np.unique(dst_flat, return_index=True)
    out = np.zeros((H, W, 3), dtype=np.uint8)
    seen = np.zeros(H * W, dtype=bool)
    out.reshape(H * W, 3)[uniq] = vals[order][first]
    seen[uniq] = True
    return out, ~seen.reshape(H, W)


def _hfill_direction(out, holes, use_left):
    """Per-row fill: for each hole pixel take the nearest valid pixel on the
    preferred side, falling back to the other side. Returns remaining holes
    (rows with no valid pixel at all stay unfilled)."""
    H, W = out.shape[:2]
    remaining = holes.copy()
    cols = np.broadcast_to(np.arange(W, dtype=np.int32), (H, W))
    valid = ~holes
    # Nearest valid index at or before each column (-1 where none)
    left_idx = np.where(valid, cols, -1)
    left_idx = np.maximum.accumulate(left_idx, axis=1)
    # Nearest valid index at or after each column (W where none)
    right_idx = np.where(valid, cols, W)
    right_idx = np.minimum.accumulate(right_idx[:, ::-1], axis=1)[:, ::-1]

    pick_left = left_idx >= 0
    pick_right = right_idx < W
    use_left = pick_left if use_left else ~pick_right
    chosen = np.where(use_left, left_idx, right_idx)
    fillable = holes & (pick_left | pick_right)
    if not fillable.any():
        return remaining
    y_rows = np.broadcast_to(np.arange(H, dtype=np.int32)[:, None], (H, W))
    src_flat = y_rows[fillable] * W + chosen[fillable]
    rgb = out.reshape(H * W, 3)
    out[fillable] = rgb[src_flat]
    remaining &= ~fillable
    return remaining


def _diffuse(out, holes, iterations=16):
    """Grow valid colours into holes by averaging valid 4-neighbours."""
    H, W = out.shape[:2]
    work = out.astype(np.float32)
    remaining = holes.copy()
    for _ in range(iterations):
        if not remaining.any():
            break
        acc = np.zeros_like(work)
        cnt = np.zeros((H, W), np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            neigh = np.roll(work, (dy, dx), axis=(0, 1))
            valid = np.roll(~remaining, (dy, dx), axis=(0, 1))
            if dy == 1:
                neigh[0] = 0
                valid[0] = False
            elif dy == -1:
                neigh[-1] = 0
                valid[-1] = False
            if dx == 1:
                neigh[:, 0] = 0
                valid[:, 0] = False
            elif dx == -1:
                neigh[:, -1] = 0
                valid[:, -1] = False
            acc += neigh
            cnt += valid
        ok = (cnt > 0) & remaining
        if not ok.any():
            break
        work[ok] = acc[ok] / np.maximum(cnt, 1e-6)[:, :, None][ok]
        remaining[ok] = False
    out[:] = np.clip(np.round(work), 0, 255).astype(np.uint8)
    return remaining


def warp_view(color_rgb, depth_gray, eye="left", base=24.0, smooth=0.35,
              fill="auto"):
    """Warp a colour image according to its depth map.

    Parameters
    ----------
    color_rgb : numpy.ndarray
        (H, W, 3) uint8 source image.
    depth_gray : numpy.ndarray
        (H, W) uint8 depth map; 0 = far, 255 = near.
    eye : {"left", "right"}
        "left" renders the view for the left eye (near content appears
        shifted right on the canvas), "right" the reverse.
    base : float
        Maximum disparity in pixels; 20-40 is typical for 1080p.
    smooth : float
        Gaussian blur radius applied to the depth, as a fraction of
        `base`.  Higher values shrink disocclusion holes but soften the
        depth edges; 0 disables smoothing.
    fill : {"auto", "none"}
        "auto" fills disocclusion holes, "none" leaves them black.

    Returns
    -------
    (H, W, 3) uint8 warped view.
    """
    disp = _disparity_map(depth_gray, radius=base * smooth)
    # The left eye sits left of the scene axis, so near content appears
    # shifted RIGHT in the left-eye view; the right eye sees it shifted LEFT.
    sign = 1.0 if eye == "left" else -1.0
    out, holes = _warp_forward(color_rgb, disp, base, sign)
    if fill == "none" or not holes.any():
        return out
    # Background is exposed on the side the content moved away from:
    # left eye shifts right, so its holes fall on the left side of objects.
    remaining = _hfill_direction(out, holes, use_left=(eye == "left"))
    if remaining.any():
        _diffuse(out, remaining)
    return out


# ---------------------------------------------------------------------------
# Anaglyph (red-cyan)
# ---------------------------------------------------------------------------

def anaglyph(color_rgb, depth_gray, base=24.0, smooth=0.35):
    """Return an (H, W, 3) uint8 red-cyan anaglyph.

    Left eye feeds the red channel, right eye the green and blue channels.
    """
    left = warp_view(color_rgb, depth_gray, eye="left", base=base,
                     smooth=smooth)
    right = warp_view(color_rgb, depth_gray, eye="right", base=base,
                      smooth=smooth)
    out = np.zeros_like(color_rgb)
    out[..., 0] = left[..., 0]    # red   ← left eye
    out[..., 1] = right[..., 1]   # green ← right eye
    out[..., 2] = right[..., 2]   # blue  ← right eye
    return out


# ---------------------------------------------------------------------------
# Straightforward PIL entry points
# ---------------------------------------------------------------------------

def make_stereo(color_pil, depth_pil, base=24.0, smooth=0.35):
    """Produce a (left, right) pair of stereo PIL Images from PIL images.

    Both inputs must share dimensions; the depth map may be grayscale or RGB.
    """
    from PIL import Image as PILImage
    color = np.asarray(color_pil.convert("RGB"), dtype=np.uint8)
    depth = np.asarray(depth_pil.convert("L"), dtype=np.uint8)
    left = warp_view(color, depth, eye="left", base=base, smooth=smooth)
    right = warp_view(color, depth, eye="right", base=base, smooth=smooth)
    return PILImage.fromarray(left), PILImage.fromarray(right)


def make_anaglyph_pil(color_pil, depth_pil, base=24.0, smooth=0.35):
    """Return a red-cyan anaglyph PIL Image from PIL inputs."""
    from PIL import Image as PILImage
    color = np.asarray(color_pil.convert("RGB"), dtype=np.uint8)
    depth = np.asarray(depth_pil.convert("L"), dtype=np.uint8)
    return PILImage.fromarray(anaglyph(color, depth, base=base, smooth=smooth))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from PIL import Image as PILImage
    W, H = 320, 180
    color = np.zeros((H, W, 3), dtype=np.uint8)
    color[:] = [32, 128, 200]
    depth = np.zeros((H, W), dtype=np.uint8)
    depth[:, :] = 64
    for x, dval in [(40, 40), (130, 180), (220, 255)]:
        color[30:100, x:x + 60] = [240, 240, 240]
        depth[30:100, x:x + 60] = dval

    left, right = make_stereo(PILImage.fromarray(color),
                              PILImage.fromarray(depth), base=20.0)
    left.save("/tmp/dibr_left.png")
    right.save("/tmp/dibr_right.png")
    make_anaglyph_pil(PILImage.fromarray(color), PILImage.fromarray(depth),
                      base=20.0).save("/tmp/dibr_anaglyph.png")
    print("Test images in /tmp/dibr_{left,right,anaglyph}.png")
