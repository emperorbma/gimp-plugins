#!/usr/bin/env python3
#   Copyright (C) 2026
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
import sys
import tempfile
from pathlib import Path
import locale
import gettext

import gi
gi.require_version('Gimp', '3.0')
from gi.repository import Gimp
gi.require_version('GimpUi', '3.0')
from gi.repository import GimpUi
gi.require_version('Gegl', '0.4')
from gi.repository import Gegl
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gio

PLUGIN_NAME = "python-fu-depthmap"
LOCALE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'locale'))

locale.bindtextdomain(PLUGIN_NAME, LOCALE_DIR)
gettext.bindtextdomain(PLUGIN_NAME, LOCALE_DIR)
gettext.textdomain(PLUGIN_NAME)

def _(message): return gettext.gettext(message)
def N_(message): return message

# ---------------------------------------------------------------------------
# Model configuration (Depth Anything V2, official depth-anything-v2 package)
# ---------------------------------------------------------------------------

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

HF_REPOS = {
    'vits': 'depth-anything/Depth-Anything-V2-Small',
    'vitb': 'depth-anything/Depth-Anything-V2-Base',
    'vitl': 'depth-anything/Depth-Anything-V2-Large',
}

VARIANT_LABELS = {
    'vits': _('Small (vits)'),
    'vitb': _('Base (vitb)'),
    'vitl': _('Large (vitl)'),
}

MODEL_CACHE = {}


def variant_from_path(name):
    """Return the variant key ('vits'/'vitb'/'vitl') for a checkpoint filename,
    or None if the name is not a Depth Anything V2 checkpoint."""
    m = re.match(r'depth_anything_v2_(vit[bsl])\.pth$', name)
    return m.group(1) if m else None


def cache_dir():
    """Directory used for auto-downloaded checkpoints. Overridable via the
    GIMP_DEPTHMAP_CACHE environment variable, else XDG cache dir."""
    env = os.environ.get('GIMP_DEPTHMAP_CACHE')
    if env:
        return Path(env)
    base = os.environ.get('XDG_CACHE_HOME')
    if base:
        return Path(base) / 'gimp-depthmap'
    return Path.home() / '.cache' / 'gimp-depthmap'


def _candidate_dirs():
    home = Path.home()
    dirs = []
    if os.name == 'nt':
        dirs = [home / 'models', home / 'Downloads',
                home / 'Downloads' / 'src' / 'ComfyUI' / 'models']
    else:
        dirs = [home / 'models', home / 'Downloads',
                home / 'Downloads' / 'src' / 'ComfyUI' / 'models',
                Path('/opt/models')]
    dirs.append(cache_dir())
    return dirs


def _locate_matches():
    """Best-effort `locate` lookup; returns [] when locate is missing/fails."""
    import shutil
    import subprocess
    if not shutil.which('locate'):
        return []
    try:
        out = subprocess.run(
            ['locate', '-r', r'depth_anything_v2_vit[bsl]\.pth'],
            capture_output=True, text=True, timeout=10)
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def find_depth_models(extra_dirs=None):
    """Find Depth Anything V2 checkpoints on disk. Returns
    {variant: [sorted Paths]}. V1 checkpoints are excluded by the pattern.
    rglob is safe here because the candidate dirs are curated and narrow
    (a fixed list covering the real ComfyUI depth, ~/Downloads/src/ComfyUI/
    models/, which is 4 levels below the candidate base)."""
    found = {v: [] for v in MODEL_CONFIGS}
    paths = set()
    for line in _locate_matches():
        paths.add(Path(line))
    for d in (extra_dirs if extra_dirs is not None else _candidate_dirs()):
        try:
            for p in d.rglob('depth_anything_v2_vit*.pth'):
                paths.add(p)
        except OSError:
            continue
    for p in sorted(paths):
        v = variant_from_path(p.name)
        if v and p.is_file():
            try:
                if p.stat().st_size < 1000:
                    continue
            except OSError:
                continue
            found[v].append(p)
    return found


def download_url(variant):
    return ("https://huggingface.co/%s/resolve/main/%s"
            % (HF_REPOS[variant], CHECKPOINTS[variant]))


def download_model(variant, dest_dir, progress=None):
    """Download a checkpoint into dest_dir (idempotent). Returns the file path."""
    import ssl
    import urllib.request
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / CHECKPOINTS[variant]
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    url = download_url(variant)
    request = urllib.request.Request(url, headers={'User-Agent': 'gimp-depthmap/1.0'})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, context=context, timeout=60) as resp, \
            open(dest, 'wb') as out:
        total = int(resp.headers.get('Content-Length') or 0)
        read = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            read += len(chunk)
            if progress and total:
                progress(min(1.0, read / float(total)))
    if progress:
        progress(1.0)
    return dest


def _require_deps():
    """Verify torch and depth-anything-v2 are importable; raise RuntimeError
    with install instructions otherwise. Returns the torch module."""
    missing = []
    try:
        import torch
    except ImportError:
        missing.append('torch')
    try:
        import depth_anything_v2  # noqa: F401
    except ImportError:
        missing.append('depth-anything-v2')
    if missing:
        raise RuntimeError(
            _("Missing dependency: %s. Install with: pip install %s pillow")
            % (', '.join(missing), ' '.join(missing)))
    return torch


def get_model(path, variant):
    """Load a Depth Anything V2 checkpoint, cached by path."""
    path = str(path)
    if path in MODEL_CACHE:
        return MODEL_CACHE[path]
    torch = _require_deps()
    from depth_anything_v2.dpt import DepthAnythingV2
    model = DepthAnythingV2(**MODEL_CONFIGS[variant])
    state = torch.load(path, map_location='cpu')
    model.load_state_dict(state)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device).eval()
    MODEL_CACHE[path] = model
    return model


def infer_depth(model, pil_image, max_side):
    """Run the model and return (grayscale HxW uint8, colored HxWx3 uint8).
    The PIL image is passed as BGR numpy because depth_anything_v2's
    infer_image expects OpenCV-style input and picks `input_size` as the
    processing resolution (aspect-preserving, multiple of 14)."""
    import numpy as np
    from matplotlib import colormaps
    rgb = np.asarray(pil_image, dtype=np.uint8)
    bgr = rgb[:, :, ::-1]
    depth = model.infer_image(bgr, input_size=max_side)  # float32 HxW
    depth = depth - depth.min()
    span = depth.max()
    gray = (depth / span * 255.0).astype(np.uint8) if span > 0 else np.zeros_like(depth, dtype=np.uint8)
    colored = (colormaps['inferno'](gray / 255.0)[:, :, :3] * 255.0).astype(np.uint8)
    return gray, colored


def paste_layer_aligned(image, target_layer, source_layer):
    """Copy `source_layer`'s pixels into `target_layer`, aligned to the target
    layer origin. An active selection is temporarily cleared so the marching
    ants cannot shift the pasted content, then restored."""
    selection = image.get_selection()
    result = selection.bounds(image)
    non_empty = result[1] if len(result) == 6 else result[0] if result else False
    saved_channel = None
    if non_empty:
        saved_channel = selection.save(image)
        Gimp.Selection.none(image)
    Gimp.edit_copy([source_layer])
    floating_sel = Gimp.edit_paste(target_layer, False)
    if floating_sel:
        Gimp.floating_sel_anchor(floating_sel[0])
    if saved_channel is not None:
        image.select_item(Gimp.ChannelOps.REPLACE, saved_channel)
        image.remove_channel(saved_channel)


def export_layer_png(image, layer, path):
    """Render a single layer at its canvas position to a full-canvas PNG at
    `path`. An active selection is temporarily cleared so only the layer's own
    pixels are copied, then restored."""
    width = image.get_width()
    height = image.get_height()
    tmp_img = Gimp.Image.new(width, height, Gimp.ImageBaseType.RGB)
    tmp_layer = Gimp.Layer.new(tmp_img, layer.get_name(), width, height,
                               Gimp.ImageType.RGBA_IMAGE, 100.0,
                               Gimp.LayerMode.NORMAL)
    tmp_img.insert_layer(tmp_layer, None, 0)
    selection = image.get_selection()
    result = selection.bounds(image)
    non_empty = result[1] if len(result) == 6 else result[0] if result else False
    saved_channel = None
    if non_empty:
        saved_channel = selection.save(image)
        Gimp.Selection.none(image)
    Gimp.edit_copy([layer])
    floating_sel = Gimp.edit_paste(tmp_layer, False)
    if floating_sel:
        Gimp.floating_sel_anchor(floating_sel[0])
    if saved_channel is not None:
        image.select_item(Gimp.ChannelOps.REPLACE, saved_channel)
        image.remove_channel(saved_channel)
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, tmp_img,
                   Gio.File.new_for_path(str(path)), None)
    Gimp.Image.delete(tmp_img)


def resolve_model(variant, explicit, download, cache):
    """Explicit path > discovered checkpoint > download. Raises RuntimeError
    with a helpful message when nothing resolves."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise RuntimeError(_("Model file not found: %s") % explicit)
        return p
    found = find_depth_models()
    if found.get(variant):
        return found[variant][0]
    if download:
        try:
            return download_model(variant, cache, progress=None)
        except Exception as e:
            raise RuntimeError(
                _("Failed to download %s from %s into %s: %s")
                % (CHECKPOINTS[variant], download_url(variant), cache, e))
    raise RuntimeError(
        _("No %s checkpoint found. Enable download or set the model file path.") % CHECKPOINTS[variant])


def make_depth_layer(image, drawable, png_path, layer_name):
    """Load a depth PNG and insert it as a new canvas-sized layer above
    `drawable`. Returns the new layer."""
    imported = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE,
                              Gio.File.new_for_path(str(png_path)))
    if not imported:
        raise RuntimeError(_("Failed to load the depth map file."))
    source = imported.get_layers()[0]
    layer = Gimp.Layer.new(image, layer_name,
                           image.get_width(), image.get_height(),
                           Gimp.ImageType.RGBA_IMAGE, 100.0,
                           Gimp.LayerMode.NORMAL)
    image.insert_layer(layer, drawable.get_parent(),
                       image.get_item_position(drawable))
    paste_layer_aligned(image, layer, source)
    Gimp.Image.delete(imported)
    image.active_layer = layer
    return layer


def depthmap_func(procedure, run_mode, image, drawables, config, data):
    if run_mode == Gimp.RunMode.INTERACTIVE:
        GimpUi.init(PLUGIN_NAME)
        dialog = GimpUi.ProcedureDialog.new(procedure, config, _("Depth Map"))
        dialog.fill(None)
        response = dialog.run()
        # Tear the dialog down so it does not linger while inference runs.
        display = dialog.get_display()
        dialog.destroy()
        if display is not None:
            display.flush()
        if not response:
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())

    Gimp.context_push()
    image.undo_group_start()

    if not drawables:
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(_("No drawable provided.")))

    input_path = None
    output_gray = None
    output_color = None
    try:
        temp_dir = Path(tempfile.gettempdir())
        input_path = temp_dir / f"gimp_depthmap_input_{os.getpid()}.png"
        output_gray = temp_dir / f"gimp_depthmap_output_gray_{os.getpid()}.png"
        output_color = temp_dir / f"gimp_depthmap_output_color_{os.getpid()}.png"

        from PIL import Image as PILImage

        Gimp.progress_init(_("Saving layer for depth estimation..."))
        export_layer_png(image, drawables[0], input_path)

        variant = config.get_property("variant")
        explicit = config.get_property("model_path").strip()
        do_download = config.get_property("download_if_missing")
        output_mode = config.get_property("output")
        max_side = config.get_property("max_side")

        Gimp.progress_set_text(_("Resolving depth model..."))
        Gimp.progress_pulse()
        model_path = resolve_model(variant, explicit, do_download, cache_dir())

        Gimp.progress_set_text(_("Estimating depth (this may take time)..."))
        Gimp.progress_pulse()
        pil_image = PILImage.open(str(input_path)).convert("RGB")
        model = get_model(model_path, variant)
        depth, colored = infer_depth(model, pil_image, max_side)

        PILImage.fromarray(depth).save(str(output_gray), "PNG")
        if output_mode in ("colored", "both"):
            PILImage.fromarray(colored).save(str(output_color), "PNG")

        Gimp.progress_set_text(_("Loading depth layer..."))
        Gimp.progress_pulse()

        drawable = drawables[0]
        make_depth_layer(image, drawable, output_gray, _("Depth (grayscale)"))
        if output_mode in ("colored", "both"):
            make_depth_layer(image, drawable, output_color, _("Depth (colored)"))

        Gimp.displays_flush()
    except Exception as e:
        Gimp.message(_("An unexpected error occurred: %s") % e)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(str(e)))
    finally:
        for p in (input_path, output_gray, output_color):
            if p is not None and p.exists():
                os.remove(p)

    image.undo_group_end()
    Gimp.context_pop()
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


class DepthMap (Gimp.PlugIn):
    __gtype_name__ = "DepthMap"

    def do_set_i18n(self, procname):
        return True, PLUGIN_NAME, None

    def do_query_procedures(self):
        return ['python-fu-depthmap']

    def do_create_procedure(self, name):
        Gegl.init(None)
        procedure = Gimp.ImageProcedure.new(self, name,
                                            Gimp.PDBProcType.PLUGIN,
                                            depthmap_func, None)
        procedure.set_image_types("RGB*, GRAY*")
        procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE |
                                       Gimp.ProcedureSensitivityMask.DRAWABLES)
        procedure.set_documentation(
            _("Depth Map"),
            _("Creates a depth map layer from the active layer using Depth Anything V2."),
            name,
        )
        procedure.set_menu_label(_("_Depth Map..."))
        procedure.set_attribution("Hugging Face (Depth Anything V2)",
                                  "Plugin Author",
                                  "2026")
        procedure.add_menu_path("<Image>/Filters/AI/")

        # Variant dropdown
        variant_choice = Gimp.Choice.new()
        for i, key in enumerate(MODEL_CONFIGS):
            variant_choice.add(key, i, key, VARIANT_LABELS[key])
        procedure.add_choice_argument(
            "variant", _("_Variant"), _("Depth Anything V2 model size"),
            variant_choice, "vits", GObject.ParamFlags.READWRITE,
        )

        # Optional explicit checkpoint path
        procedure.add_string_argument(
            "model_path", _("_Model file"),
            _("Optional path to a .pth checkpoint (overrides discovery)"),
            "", GObject.ParamFlags.READWRITE,
        )

        # Auto-download toggle
        procedure.add_boolean_argument(
            "download_if_missing", _("_Download if missing"),
            _("Fetch the checkpoint from Hugging Face when not found on disk"),
            True, GObject.ParamFlags.READWRITE,
        )

        # Output mode
        output_choice = Gimp.Choice.new()
        output_choice.add("grayscale", 0, "grayscale", _("Grayscale"))
        output_choice.add("colored", 1, "colored", _("Colored (inferno)"))
        output_choice.add("both", 2, "both", _("Both"))
        procedure.add_choice_argument(
            "output", _("_Output"), _("Depth layer contents"),
            output_choice, "grayscale", GObject.ParamFlags.READWRITE,
        )

        # Inference resolution
        procedure.add_int_argument(
            "max_side", _("_Max side (px)"),
            _("Larger side of the inference resolution; higher = better detail, slower"),
            1024, 256, 4096, GObject.ParamFlags.READWRITE,
        )

        return procedure


Gimp.main(DepthMap.__gtype_name__, sys.argv)
