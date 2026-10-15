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
import json
import tempfile
import threading
from pathlib import Path
import locale
import gettext

import gi
from gi.repository import Gimp
gi.require_version('GimpUi', '3.0')
from gi.repository import GimpUi
from gi.repository import Gegl
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gio
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk

try:
    import dibr
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dibr

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
    'vits': _('vits (small)'),
    'vitb': _('vitb (base)'),
    'vitl': _('vitl (large)'),
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


CONFIG_DIR = Path(os.path.join(os.environ.get('HOME', str(Path.home())),
                               '.config', 'gimp-plugins', 'depthmap'))
CONFIG_FILE = CONFIG_DIR / 'config.json'


def load_saved():
    """Load previously saved dialog settings and discovered checkpoint
    paths. Returns a dict that is always missing at least the keys it does
    not know about."""
    default = {
        "variant": "vits",
        "model_path": "",
        "download_if_missing": True,
        "output": "grayscale",
        "max_side": 1024,
        "invert": False,
        "export_3d": False,
        "discovered": {},
    }
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    merged = dict(default)
    merged.update({k: v for k, v in data.items() if k in default})
    if not isinstance(merged["discovered"], dict):
        merged["discovered"] = {}
    return merged


def save_config(cfg):
    """Persist dialog settings and discovered model paths to disk."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


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


def invert_depth(gray, colored):
    """Invert a depth map (near<->far) and recolor it, for lens blur."""
    import numpy as np
    from matplotlib import colormaps
    gray = np.asarray(gray, dtype=np.uint8)
    inverted = (255 - gray).astype(np.uint8)
    recolored = (colormaps['inferno'](inverted / 255.0)[:, :, :3] * 255.0).astype(np.uint8)
    return inverted, recolored


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


def save_layer_jpg(image, layer, path):
    """Render a single layer at its canvas position to a full-canvas JPEG at
    `path`, compositing alpha onto white (JPEG has no alpha channel)."""
    from PIL import Image as PILImage
    tmp_png = Path(tempfile.gettempdir()) / f"gimp_depthmap_export_{os.getpid()}.png"
    try:
        export_layer_png(image, layer, tmp_png)
        rgba = PILImage.open(str(tmp_png)).convert("RGBA")
        canvas = PILImage.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[3])
        canvas.save(str(path), "JPEG", quality=95)
    finally:
        if tmp_png.exists():
            os.remove(tmp_png)


def export_3d_chooser():
    """Ask for the base name/location of a Facebook 3D photo export. Returns
    a Path without extension (the color and depth files get `.jpg` and
    `_depth.jpg` appended), or None when the user cancels."""
    chooser = Gtk.FileChooserDialog(
        title=_("Export 3D Photo (Facebook)"),
        transient_for=None,
        action=Gtk.FileChooserAction.SAVE)
    chooser.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL,
                        _("_Save"), Gtk.ResponseType.ACCEPT)
    chooser.set_do_overwrite_confirmation(True)
    chooser.set_current_name("3d_photo.jpg")
    result = None
    if chooser.run() == Gtk.ResponseType.ACCEPT:
        chosen = Path(chooser.get_filename())
        if chosen.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp',
                                     '.bmp', '.tif', '.tiff', '.gif'):
            chosen = chosen.with_suffix('')
        result = chosen
    chooser.destroy()
    display = Gdk.Display.get_default()
    if display is not None:
        display.flush()
    return result


def resolve_model(variant, explicit, download, cache, progress=None):
    """Explicit path > discovered checkpoint > download. Raises RuntimeError
    with a helpful message when nothing resolves. `progress` is an optional
    callable(fraction) forwarded to download_model while downloading."""
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
            return download_model(variant, cache, progress=progress)
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


def depthmap_dialog(config):
    """Custom dialog for depth map options. Loads last-used settings and any
    previously discovered checkpoint paths so it opens instantly; starts a
    background `locate` search (with a spinner) only when a variant has no
    cached path. Writes the chosen values into `config` and persists them.
    Returns True when accepted."""
    saved = load_saved()
    discovered = {k: list(v) for k, v in saved.get("discovered", {}).items()}

    dialog = Gtk.Dialog(title=_("Depth Map"), transient_for=None)
    dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL,
                       _("_OK"), Gtk.ResponseType.OK)
    dialog.set_default_size(520, 250)
    dialog.set_resizable(True)

    grid = Gtk.Grid()
    grid.set_border_width(12)
    grid.set_row_spacing(6)
    grid.set_column_spacing(8)
    dialog.get_content_area().add(grid)

    row = 0

    # Depth Anything V2 variant
    variant_label = Gtk.Label.new_with_mnemonic(_("Depth Anything V2 _Variant:"))
    variant_label.set_halign(Gtk.Align.START)
    variant_combo = Gtk.ComboBoxText()
    for key in MODEL_CONFIGS:
        variant_combo.append(key, VARIANT_LABELS[key])

    model_entry = Gtk.Entry()

    # Spinner + label shown while a background discovery runs.
    search_spinner = Gtk.Spinner()
    search_spinner.set_size_request(16, 16)
    search_label = Gtk.Label()
    search_label.set_halign(Gtk.Align.START)

    # Discovery is run once in a worker thread and merged into `discovered`.
    # A second search is only started when the user picks a variant with no
    # cached or already-found path.
    search_lock = threading.Lock()
    set_searching = lambda active, msg="": ( \
        (search_label.set_text(msg), search_spinner.start(), search_spinner.show(), search_label.show()) \
        if active else (search_spinner.stop(), search_spinner.hide(), search_label.hide()))

    def refresh_field(variant):
        matches = discovered.get(variant) or []
        if matches:
            model_entry.set_text(str(matches[0]))

    def merge_find(result):
        for v in MODEL_CONFIGS:
            hits = result.get(v)
            if hits and not discovered.get(v):
                discovered[v] = [str(p) for p in hits]
        # Done: hide the spinner and refill the current variant's field if it
        # now has a path but the entry is still empty.
        set_searching(False)
        variant_t = variant_combo.get_active_id() or "vits"
        if not model_entry.get_text().strip():
            refresh_field(variant_t)
        return False

    def start_search():
        """Run discovery in a worker thread; keep any fresh results cached."""
        if not search_lock.acquire(blocking=False):
            return
        set_searching(True, _("Searching for models..."))

        def worker():
            try:
                result = find_depth_models()
            finally:
                search_lock.release()
            GLib.idle_add(merge_find, result)

        threading.Thread(target=worker, daemon=True).start()

    def on_variant_changed(cb):
        variant = cb.get_active_id() or "vits"
        config.set_property("variant", variant)
        if discovered.get(variant) or model_entry.get_text().strip():
            refresh_field(variant)
        else:
            # Nothing cached for this variant: look for it in the background.
            model_entry.set_text("")
            start_search()

    variant_combo.connect("changed", on_variant_changed)
    grid.attach(variant_label, 0, row, 1, 1)
    grid.attach(variant_combo, 1, row, 1, 1)
    grid.attach(search_spinner, 2, row, 1, 1)
    grid.attach(search_label, 3, row, 1, 1)
    row += 1

    # Model file (auto-filled by variant; editable as an override)
    model_label = Gtk.Label.new_with_mnemonic(_("_Model file:"))
    model_label.set_halign(Gtk.Align.START)

    def on_model_changed(entry):
        config.set_property("model_path", entry.get_text())

    model_entry.connect("changed", on_model_changed)
    grid.attach(model_label, 0, row, 1, 1)
    grid.attach(model_entry, 1, row, 1, 1)
    row += 1

    # Download if missing
    download_check = Gtk.CheckButton.new_with_mnemonic(_("_Download if missing"))

    def on_download_toggled(cb):
        config.set_property("download_if_missing", cb.get_active())

    download_check.connect("toggled", on_download_toggled)
    grid.attach(download_check, 1, row, 1, 1)
    row += 1

    # Output mode
    output_label = Gtk.Label.new_with_mnemonic(_("_Output:"))
    output_label.set_halign(Gtk.Align.START)
    output_combo = Gtk.ComboBoxText()
    output_combo.append("grayscale", _("Grayscale"))
    output_combo.append("colored", _("Colored (inferno)"))
    output_combo.append("both", _("Both"))

    def on_output_changed(cb):
        config.set_property("output", cb.get_active_id() or "grayscale")

    output_combo.connect("changed", on_output_changed)
    grid.attach(output_label, 0, row, 1, 1)
    grid.attach(output_combo, 1, row, 1, 1)
    row += 1

    # Max side (px)
    max_label = Gtk.Label.new_with_mnemonic(_("_Max side (px):"))
    max_label.set_halign(Gtk.Align.START)
    max_adj = Gtk.Adjustment.new(1024, 256, 4096, 1, 64, 0)
    max_spin = Gtk.SpinButton(adjustment=max_adj, climb_rate=1.0, digits=0)

    def on_max_changed(spin):
        config.set_property("max_side", spin.get_value_as_int())

    max_spin.connect("value-changed", on_max_changed)
    grid.attach(max_label, 0, row, 1, 1)
    grid.attach(max_spin, 1, row, 1, 1)
    row += 1

    # Invert (for lens blur)
    invert_check = Gtk.CheckButton.new_with_mnemonic(_("_Invert (for lens blur)"))

    def on_invert_toggled(cb):
        config.set_property("invert", cb.get_active())

    invert_check.connect("toggled", on_invert_toggled)
    grid.attach(invert_check, 1, row, 1, 1)
    row += 1

    # Export as 3D photo (Facebook)
    export_check = Gtk.CheckButton.new_with_mnemonic(
        _("Export as _3D photo (Facebook)"))

    def on_export_toggled(cb):
        config.set_property("export_3d", cb.get_active())

    export_check.connect("toggled", on_export_toggled)
    grid.attach(export_check, 1, row, 1, 1)
    row += 1

    # Initialize widgets from saved settings. Setting the active variant
    # fires on_variant_changed and helps fill the model path field.
    variant_combo.set_active_id(saved.get("variant") or "vits")
    model_entry.set_text(saved.get("model_path") or "")
    download_check.set_active(bool(saved.get("download_if_missing")))
    output_combo.set_active_id(saved.get("output") or "grayscale")
    max_spin.set_value(int(saved.get("max_side") or 1024))
    invert_check.set_active(bool(saved.get("invert")))
    export_check.set_active(bool(saved.get("export_3d")))

    # Reflect saved settings into config so OK can rely on config alone.
    config.set_property("variant", variant_combo.get_active_id() or "vits")
    config.set_property("model_path", model_entry.get_text())
    config.set_property("download_if_missing", download_check.get_active())
    config.set_property("output", output_combo.get_active_id() or "grayscale")
    config.set_property("max_side", max_spin.get_value_as_int())
    config.set_property("invert", invert_check.get_active())
    config.set_property("export_3d", export_check.get_active())

    # Kick off a search only when the persisted variant has no cached path.
    variant_now = variant_combo.get_active_id() or "vits"
    if not discovered.get(variant_now) and not model_entry.get_text().strip():
        start_search()

    dialog.show_all()
    response = dialog.run()
    dialog.destroy()
    display = Gdk.Display.get_default()
    if display is not None:
        display.flush()
    if response != Gtk.ResponseType.OK:
        return False

    # Persist the last-used settings and the merged discovery cache.
    persist = {
        "variant": config.get_property("variant"),
        "model_path": config.get_property("model_path").strip(),
        "download_if_missing": bool(config.get_property("download_if_missing")),
        "output": config.get_property("output"),
        "max_side": int(config.get_property("max_side")),
        "invert": bool(config.get_property("invert")),
        "export_3d": bool(config.get_property("export_3d")),
        "discovered": discovered,
    }
    save_config(persist)
    return True

def estimate_depth_for_layer(image, drawable, variant, explicit, do_download,
                             max_side):
    """Export `drawable` to a temp PNG, resolve the model, run depth
    estimation, and return (depth, colored, pil_image, model_path). The
    temp input file is cleaned up before returning. Progress is reported
    through Gimp."""
    from PIL import Image as PILImage
    temp_dir = Path(tempfile.gettempdir())
    input_path = temp_dir / f"gimp_depthmap_input_{os.getpid()}.png"
    try:
        Gimp.progress_init(_("Saving layer for depth estimation..."))
        export_layer_png(image, drawable, input_path)

        Gimp.progress_set_text(_("Resolving depth model..."))
        Gimp.progress_pulse()
        model_path = resolve_model(
            variant, explicit, do_download, cache_dir(),
            progress=lambda frac: Gimp.progress_set_text(
                _("Downloading %s (%d%%)...") % (CHECKPOINTS[variant], int(frac * 100))))

        Gimp.progress_set_text(_("Estimating depth (this may take time)..."))
        Gimp.progress_pulse()
        pil_image = PILImage.open(str(input_path)).convert("RGB")
        model = get_model(model_path, variant)
        depth, colored = infer_depth(model, pil_image, max_side)
        return depth, colored, pil_image, model_path
    finally:
        if input_path.exists():
            os.remove(input_path)
def depthmap_func(procedure, run_mode, image, drawables, config, data):
    if run_mode == Gimp.RunMode.INTERACTIVE:
        GimpUi.init(PLUGIN_NAME)
        if not depthmap_dialog(config):
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())

    export_3d = bool(config.get_property("export_3d"))
    export_base = None
    if export_3d:
        configured = config.get_property("export_3d_path").strip()
        if run_mode == Gimp.RunMode.INTERACTIVE and not configured:
            export_base = export_3d_chooser()
            if export_base is None:
                return procedure.new_return_values(Gimp.PDBStatusType.CANCEL,
                                                   GLib.Error())
        elif configured:
            export_base = Path(configured)
        else:
            return procedure.new_return_values(
                Gimp.PDBStatusType.EXECUTION_ERROR,
                GLib.Error(_("3D photo export needs a target path: set the "
                             "'export_3d_path' argument or run interactively.")))

    if not drawables:
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(_("No drawable provided.")))

    Gimp.context_push()
    image.undo_group_start()

    output_gray = None
    output_color = None
    try:
        temp_dir = Path(tempfile.gettempdir())
        output_gray = temp_dir / f"gimp_depthmap_output_gray_{os.getpid()}.png"
        output_color = temp_dir / f"gimp_depthmap_output_color_{os.getpid()}.png"

        from PIL import Image as PILImage
        depth, colored, _pil, _model = estimate_depth_for_layer(
            image, drawables[0],
            config.get_property("variant"),
            config.get_property("model_path").strip(),
            config.get_property("download_if_missing"),
            config.get_property("max_side"))

        output_mode = config.get_property("output")
        if config.get_property("invert"):
            depth, colored = invert_depth(depth, colored)

        PILImage.fromarray(depth).save(str(output_gray), "PNG")
        if output_mode in ("colored", "both"):
            PILImage.fromarray(colored).save(str(output_color), "PNG")

        Gimp.progress_set_text(_("Loading depth layer..."))
        Gimp.progress_pulse()

        drawable = drawables[0]
        make_depth_layer(image, drawable, output_gray, _("Depth (grayscale)"))
        if output_mode in ("colored", "both"):
            make_depth_layer(image, drawable, output_color, _("Depth (colored)"))
        if export_3d and export_base is not None:
            Gimp.progress_set_text(_("Exporting 3D photo..."))
            Gimp.progress_pulse()
            color_jpg = Path(str(export_base) + '.jpg')
            depth_jpg = Path(str(export_base) + '_depth.jpg')
            save_layer_jpg(image, drawables[0], color_jpg)
            PILImage.fromarray(depth).save(str(depth_jpg), "JPEG", quality=95)
            Gimp.message(_("3D photo saved:\n%s\n%s") % (color_jpg, depth_jpg))

        Gimp.displays_flush()
    except Exception as e:
        Gimp.message(_("An unexpected error occurred: %s") % e)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(str(e)))
    finally:
        for p in (output_gray, output_color):
            if p is not None and p.exists():
                os.remove(p)

    image.undo_group_end()
    Gimp.context_pop()
    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

def stereo_dialog(config):
    """Custom dialog for stereo view options. Loads last-used settings so it
    opens instantly and persists the chosen values. Returns True when
    accepted."""
    saved = load_saved()

    dialog = Gtk.Dialog(title=_("Stereo Views"), transient_for=None)
    dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL,
                       _("_OK"), Gtk.ResponseType.OK)
    dialog.set_default_size(520, 280)
    dialog.set_resizable(True)

    grid = Gtk.Grid()
    grid.set_border_width(12)
    grid.set_row_spacing(6)
    grid.set_column_spacing(8)
    dialog.get_content_area().add(grid)

    row = 0

    # Depth Anything V2 variant
    variant_label = Gtk.Label.new_with_mnemonic(
        _("Depth Anything V2 _Variant:"))
    variant_label.set_halign(Gtk.Align.START)
    variant_combo = Gtk.ComboBoxText()
    for key in MODEL_CONFIGS:
        variant_combo.append(key, VARIANT_LABELS[key])

    def on_variant_changed(cb):
        config.set_property("variant", cb.get_active_id() or "vits")

    variant_combo.connect("changed", on_variant_changed)
    grid.attach(variant_label, 0, row, 1, 1)
    grid.attach(variant_combo, 1, row, 1, 1)
    row += 1

    # Model file (optional override)
    model_label = Gtk.Label.new_with_mnemonic(_("_Model file:"))
    model_label.set_halign(Gtk.Align.START)
    model_entry = Gtk.Entry()

    def on_model_changed(entry):
        config.set_property("model_path", entry.get_text())

    model_entry.connect("changed", on_model_changed)
    grid.attach(model_label, 0, row, 1, 1)
    grid.attach(model_entry, 1, row, 1, 1)
    row += 1

    # Download if missing
    download_check = Gtk.CheckButton.new_with_mnemonic(_("_Download if missing"))

    def on_download_toggled(cb):
        config.set_property("download_if_missing", cb.get_active())

    download_check.connect("toggled", on_download_toggled)
    grid.attach(download_check, 1, row, 1, 1)
    row += 1

    # Inference resolution
    max_label = Gtk.Label.new_with_mnemonic(_("_Max side (px):"))
    max_label.set_halign(Gtk.Align.START)
    max_adj = Gtk.Adjustment.new(1024, 256, 4096, 1, 64, 0)
    max_spin = Gtk.SpinButton(adjustment=max_adj, climb_rate=1.0, digits=0)

    def on_max_changed(spin):
        config.set_property("max_side", spin.get_value_as_int())

    max_spin.connect("value-changed", on_max_changed)
    grid.attach(max_label, 0, row, 1, 1)
    grid.attach(max_spin, 1, row, 1, 1)
    row += 1

    # Maximum parallax
    shift_label = Gtk.Label.new_with_mnemonic(_("_Parallax (px):"))
    shift_label.set_halign(Gtk.Align.START)
    shift_adj = Gtk.Adjustment.new(24, 0, 512, 1, 8, 0)
    shift_spin = Gtk.SpinButton(adjustment=shift_adj, climb_rate=1.0, digits=0)

    def on_shift_changed(spin):
        config.set_property("max_shift", spin.get_value_as_int())

    shift_spin.connect("value-changed", on_shift_changed)
    grid.attach(shift_label, 0, row, 1, 1)
    grid.attach(shift_spin, 1, row, 1, 1)
    row += 1

    # Create: combined views in a new tab
    output_label = Gtk.Label.new_with_mnemonic(_("_Create:"))
    output_label.set_halign(Gtk.Align.START)
    output_combo = Gtk.ComboBoxText()
    output_combo.append("side_by_side", _("Side-by-side (new tab)"))
    output_combo.append("anaglyph", _("Red-cyan anaglyph (new tab)"))
    output_combo.append("both", _("Both (new tabs)"))

    def on_output_changed(cb):
        config.set_property("stereo_output",
                            cb.get_active_id() or "side_by_side")

    output_combo.connect("changed", on_output_changed)
    grid.attach(output_label, 0, row, 1, 1)
    grid.attach(output_combo, 1, row, 1, 1)
    row += 1

    # Initialize widgets from saved settings, then mirror into config.
    variant_combo.set_active_id(saved.get("variant") or "vits")
    model_entry.set_text(saved.get("model_path") or "")
    download_check.set_active(bool(saved.get("download_if_missing")))
    max_spin.set_value(int(saved.get("max_side") or 1024))
    shift_spin.set_value(int(saved.get("stereo_max_shift") or 24))
    output_combo.set_active_id(saved.get("stereo_output") or "side_by_side")

    config.set_property("variant", variant_combo.get_active_id() or "vits")
    config.set_property("model_path", model_entry.get_text())
    config.set_property("download_if_missing", download_check.get_active())
    config.set_property("max_side", max_spin.get_value_as_int())
    config.set_property("max_shift", shift_spin.get_value_as_int())
    config.set_property("stereo_output",
                        output_combo.get_active_id() or "side_by_side")

    dialog.show_all()
    response = dialog.run()
    dialog.destroy()
    display = Gdk.Display.get_default()
    if display is not None:
        display.flush()
    if response != Gtk.ResponseType.OK:
        return False

    # Persist the last-used settings alongside the existing configuration.
    persist = load_saved()
    persist.update({
        "variant": config.get_property("variant"),
        "model_path": config.get_property("model_path").strip(),
        "download_if_missing": bool(config.get_property("download_if_missing")),
        "max_side": int(config.get_property("max_side")),
        "stereo_max_shift": int(config.get_property("max_shift")),
        "stereo_output": config.get_property("stereo_output"),
    })
    save_config(persist)
    return True


def stereo_func(procedure, run_mode, image, drawables, config, data):
    if run_mode == Gimp.RunMode.INTERACTIVE:
        GimpUi.init(PLUGIN_NAME)
        if not stereo_dialog(config):
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, GLib.Error())

    if not drawables:
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(_("No drawable provided.")))

    Gimp.context_push()

    out_paths = []
    try:
        from PIL import Image as PILImage
        depth, _colored, pil_color, _model = estimate_depth_for_layer(
            image, drawables[0],
            config.get_property("variant"),
            config.get_property("model_path").strip(),
            config.get_property("download_if_missing"),
            config.get_property("max_side"))

        max_shift = config.get_property("max_shift")
        output_mode = config.get_property("stereo_output")

        Gimp.progress_set_text(_("Rendering stereo views..."))
        Gimp.progress_pulse()
        temp_dir = Path(tempfile.gettempdir())
        if output_mode in ("side_by_side", "both"):
            left, right = dibr.make_stereo(pil_color,
                                           PILImage.fromarray(depth),
                                           base=float(max_shift))
            combo = PILImage.new("RGB", (left.width + right.width, left.height))
            combo.paste(left, (0, 0))
            combo.paste(right, (left.width, 0))
            sbs_path = temp_dir / f"gimp_depthmap_sbs_{os.getpid()}.png"
            combo.save(str(sbs_path), "PNG")
            out_paths.append((sbs_path, _("Stereo (left | right)")))
        if output_mode in ("anaglyph", "both"):
            anaglyph_pil = dibr.make_anaglyph_pil(
                pil_color, PILImage.fromarray(depth), base=float(max_shift))
            anag_path = temp_dir / f"gimp_depthmap_anaglyph_{os.getpid()}.png"
            anaglyph_pil.save(str(anag_path), "PNG")
            out_paths.append((anag_path, _("Anaglyph")))

        Gimp.progress_set_text(_("Opening combined view in a new tab..."))
        Gimp.progress_pulse()
        for path, layer_name in out_paths:
            new_image = Gimp.file_load(Gimp.RunMode.NONINTERACTIVE,
                                       Gio.File.new_for_path(str(path)))
            if not new_image:
                continue
            layers = new_image.get_layers()
            if layers:
                layers[0].set_name(layer_name)
            Gimp.Display.new(new_image)
    except Exception as e:
        Gimp.message(_("An unexpected error occurred: %s") % e)
        return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                           GLib.Error(str(e)))
    finally:
        for p, _name in out_paths:
            if p.exists():
                os.remove(p)
        Gimp.context_pop()

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())

def create_stereo_procedure(plugin, name):
    """Build and return the Stereo Views ImageProcedure."""
    procedure = Gimp.ImageProcedure.new(plugin, name,
                                        Gimp.PDBProcType.PLUGIN,
                                        stereo_func, None)
    procedure.set_image_types("RGB*, GRAY*")
    procedure.set_sensitivity_mask(Gimp.ProcedureSensitivityMask.DRAWABLE |
                                   Gimp.ProcedureSensitivityMask.DRAWABLES)
    procedure.set_documentation(
        _("Stereo Views"),
        _("Renders left and right eye views of the scene from its estimated "
          "depth (depth-image-based rendering)."),
        name,
    )
    procedure.set_menu_label(_("Stereo _Views..."))
    procedure.set_attribution("Hugging Face (Depth Anything V2)",
                              "Plugin Author",
                              "2026")
    procedure.add_menu_path("<Image>/Filters/AI/")

    # Depth Anything V2 variant
    variant_choice = Gimp.Choice.new()
    for i, key in enumerate(MODEL_CONFIGS):
        variant_choice.add(key, i, key, VARIANT_LABELS[key])
    procedure.add_choice_argument(
        "variant", _("Depth Anything V2 _Variant"), _("Depth Anything V2 model size"),
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

    # Inference resolution
    procedure.add_int_argument(
        "max_side", _("_Max side (px)"),
        _("Larger side of the inference resolution; higher = better detail, slower"),
        256, 4096, 1024, GObject.ParamFlags.READWRITE,
    )

    # Maximum parallax
    procedure.add_int_argument(
        "max_shift", _("_Parallax (px)"),
        _("Maximum horizontal shift between the eyes; larger = stronger 3D"),
        0, 512, 24, GObject.ParamFlags.READWRITE,
    )

    # Output mode
    output_choice = Gimp.Choice.new()
    output_choice.add("side_by_side", 0, "side_by_side",
                      _("Side-by-side (new tab)"))
    output_choice.add("anaglyph", 1, "anaglyph", _("Red-cyan anaglyph (new tab)"))
    output_choice.add("both", 2, "both", _("Both (new tabs)"))
    procedure.add_choice_argument(
        "stereo_output", _("_Create"),
        _("Which combined result to open in a new tab"),
        output_choice, "side_by_side", GObject.ParamFlags.READWRITE,
    )

    return procedure


class DepthMap (Gimp.PlugIn):
    __gtype_name__ = "DepthMap"

    def do_set_i18n(self, procname):
        return True, PLUGIN_NAME, None

    def do_query_procedures(self):
        return ['python-fu-depthmap', 'python-fu-depthmap-stereo']

    def do_create_procedure(self, name):
        Gegl.init(None)
        if name == 'python-fu-depthmap-stereo':
            return create_stereo_procedure(self, name)
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
            "variant", _("Depth Anything V2 _Variant"), _("Depth Anything V2 model size"),
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
            256, 4096, 1024, GObject.ParamFlags.READWRITE,
        )

        # Invert (for lens blur)
        procedure.add_boolean_argument(
            "invert", _("_Invert (for lens blur)"),
            _("Swap near and far so lens blur can blur the background"),
            False, GObject.ParamFlags.READWRITE,
        )

        # Export as 3D photo (Facebook)
        procedure.add_boolean_argument(
            "export_3d", _("_Export as 3D photo (Facebook)"),
            _("Prompts for a location, then saves the layer as name.jpg and "
              "the depth map as name_depth.jpg for Facebook 3D photos"),
            False, GObject.ParamFlags.READWRITE,
        )

        # Base path for non-interactive 3D photo export
        procedure.add_string_argument(
            "export_3d_path", _("_3D photo path"),
            _("Base path for the 3D photo export (name.jpg and name_depth.jpg "
              "are written beside it)"),
            "", GObject.ParamFlags.READWRITE,
        )

        return procedure


Gimp.main(DepthMap.__gtype_name__, sys.argv)
