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


def height_from_max_side(w, h, max_side):
    """Aspect-preserving processing height so the larger side ~= max_side.
    Native height is kept when the image is already smaller than max_side."""
    if max(w, h) <= max_side:
        return h
    scale = float(max_side) / max(w, h)
    return int(round(h * scale))


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


def depthmap_func(procedure, run_mode, image, drawables, config, data):
    return procedure.new_return_values(Gimp.PDBStatusType.EXECUTION_ERROR,
                                       GLib.Error(_("Not implemented yet.")))


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
        return procedure


Gimp.main(DepthMap.__gtype_name__, sys.argv)
