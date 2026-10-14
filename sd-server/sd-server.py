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
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
from gi.repository import Gdk

import sys
import os
import tempfile
import json
from pathlib import Path
import locale
import gettext

# Internationalization setup
PLUGIN_NAME = "python-fu-sd-server"
LOCALE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), 'locale'))

locale.bindtextdomain(PLUGIN_NAME, LOCALE_DIR)
gettext.bindtextdomain(PLUGIN_NAME, LOCALE_DIR)
gettext.textdomain(PLUGIN_NAME)

def _(message): return gettext.gettext(message)
def N_(message): return message

# Default SD server URL
DEFAULT_SERVER_URL = "http://127.0.0.1:1234"


def _fetch_capabilities(server_url):
    """Fetch server capabilities, returns dict or None on failure."""
    try:
        import requests
        resp = requests.get(f"{server_url}/sdcpp/v1/capabilities", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def export_selection_mask(image, path):
    """Export the current selection (marching ants or quick mask) as a
    full-canvas grayscale PNG mask at `path`. White marks the selected region,
    which the server repaints via the img2img mask field. Returns True when a
    non-empty selection existed and the file was written."""
    selection = image.get_selection()
    result = selection.bounds(image)
    if not result:
        return False
    non_empty = result[1] if len(result) == 6 else result[0]
    if not non_empty:
        return False

    width = image.get_width()
    height = image.get_height()
    mask_image = Gimp.Image.new(width, height, Gimp.ImageBaseType.GRAY)
    layer = Gimp.Layer.new(mask_image, "mask", width, height,
                           Gimp.ImageType.GRAY_IMAGE, 100.0,
                           Gimp.LayerMode.NORMAL)
    mask_image.insert_layer(layer, None, 0)
    Gimp.edit_copy([selection])
    floating = Gimp.edit_paste(layer, False)
    if floating:
        Gimp.floating_sel_anchor(floating[0])
    Gimp.file_save(Gimp.RunMode.NONINTERACTIVE, mask_image,
                   Gio.File.new_for_path(str(path)), None)
    Gimp.Image.delete(mask_image)
    return True


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


def sd_server_func(procedure, run_mode, image, drawables, config, data):
    if run_mode == Gimp.RunMode.INTERACTIVE:
        GimpUi.init(PLUGIN_NAME)

        dialog = Gtk.Dialog(title=_("SD Server"), transient_for=None)
        dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL,
                           _("_OK"), Gtk.ResponseType.OK)
        dialog.set_default_size(600, 500)
        dialog.set_resizable(True)

        grid = Gtk.Grid()
        grid.set_border_width(12)
        grid.set_row_spacing(6)
        grid.set_column_spacing(8)
        dialog.get_content_area().add(grid)

        row = 0

        # --- Server URL ---
        server_label = Gtk.Label.new_with_mnemonic(_("_Server:"))
        server_label.set_halign(Gtk.Align.START)
        server_label.set_hexpand(False)
        server_entry = Gtk.Entry()
        server_entry.set_text(config.get_property("server-url") or DEFAULT_SERVER_URL)
        server_entry.set_hexpand(True)
        server_entry.set_valign(Gtk.Align.CENTER)
        server_entry.set_tooltip_text(_("URL of the running SDCPP-compatible inference server"))
        server_label.set_mnemonic_widget(server_entry)

        def on_server_changed(e):
            config.set_property("server-url", e.get_text())

        server_entry.connect("changed", on_server_changed)
        grid.attach(server_label, 0, row, 1, 1)
        grid.attach(server_entry, 1, row, 1, 1)
        row += 1

        # --- Model info spinner + label ---
        model_info_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        model_info_hbox.set_hexpand(True)

        query_spinner = Gtk.Spinner()
        query_spinner.start()
        model_info_hbox.pack_start(query_spinner, False, False, 0)

        model_info_label = Gtk.Label(label=_("Querying server..."))
        model_info_label.set_halign(Gtk.Align.START)
        model_info_label.set_hexpand(True)
        model_info_label.set_ellipsize(3)
        model_info_hbox.pack_start(model_info_label, True, True, 0)

        grid.attach(model_info_hbox, 0, row, 2, 1)
        row += 1

        # Probe server capabilities in a background thread
        import threading

        def _on_probe_done(stem):
            if stem:
                model_info_label.set_text(_("Model: %s") % stem)
                model_info_label.set_tooltip_text(stem)
                if ("turbo" in stem.lower() and
                        steps_spin.get_value_as_int() > 10):
                    steps_spin.set_value(8)
                    cfg_spin.set_value(1.0)
                _apply_multi_layer_defaults()
            else:
                model_info_label.set_text(_("Connected"))
            query_spinner.stop()
            return False

        def _on_probe_failed():
            model_info_label.set_text(_("Could not fetch server capabilities"))
            query_spinner.stop()
            return False

        def _do_probe():
            url = server_entry.get_text()
            caps = _fetch_capabilities(url)
            if caps:
                stem = caps.get("model", {}).get("stem", "")
                GLib.idle_add(_on_probe_done, stem)
            else:
                GLib.idle_add(_on_probe_failed)

        def _start_probe():
            query_spinner.start()
            model_info_label.set_text(_("Querying server..."))
            threading.Thread(target=_do_probe, daemon=True).start()
            _probe_timeout_id[0] = None
            return False

        # Re-probe when server URL changes (debounced)
        _probe_timeout_id = [None]
        def _schedule_probe(*_args):
            if _probe_timeout_id[0] is not None:
                GLib.source_remove(_probe_timeout_id[0])
            _probe_timeout_id[0] = GLib.timeout_add(600, _start_probe)
        server_entry.connect("changed", _schedule_probe)

        _start_probe()

        # --- Generate new image checkbox ---
        generate_label = Gtk.CheckButton.new_with_mnemonic(
            _("Generate new image (text2img)")
        )
        generate_label.set_halign(Gtk.Align.START)
        generate_label.set_valign(Gtk.Align.CENTER)
        generate_label.set_active(
            config.get_property("generate-new") or False
        )
        generate_label.set_tooltip_text(_("When checked, create a new image instead of editing the current layer"))

        def on_generate_changed(cb):
            config.set_property("generate-new", cb.get_active())

        generate_label.connect("toggled", on_generate_changed)
        grid.attach(generate_label, 0, row, 2, 1)
        row += 1

        # --- Use selection as mask checkbox ---
        use_selection_check = Gtk.CheckButton.new_with_mnemonic(
            _("Use _selection as mask (edit only selected area)")
        )
        use_selection_check.set_halign(Gtk.Align.START)
        use_selection_check.set_valign(Gtk.Align.CENTER)
        use_selection_check.set_active(
            config.get_property("use-selection") or False
        )
        use_selection_check.set_tooltip_text(_("When editing, restrict changes to the current marching-ants selection or quick mask. Without a selection, the whole image is edited. Has no effect when generating a new image."))

        def on_use_selection_changed(cb):
            config.set_property("use-selection", cb.get_active())

        use_selection_check.connect("toggled", on_use_selection_changed)
        grid.attach(use_selection_check, 0, row, 2, 1)
        row += 1

        # --- Multi-layer edit ---
        layer_rows = []
        ref_scrolled = Gtk.ScrolledWindow()
        ref_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        ref_scrolled.set_hexpand(True)
        ref_scrolled.set_vexpand(True)
        ref_scrolled.set_min_content_height(120)

        ref_list = Gtk.ListBox()
        ref_list.set_selection_mode(Gtk.SelectionMode.NONE)
        ref_scrolled.add(ref_list)

        def _update_ref_numbers():
            idx = 0
            for _lid, _check, _num_label in layer_rows:
                if _check.get_active():
                    idx += 1
                    _num_label.set_text(_("image %d:") % idx)
                else:
                    _num_label.set_text("")
            return idx

        if image is not None:
            target_id = drawables[0].get_id() if drawables else None
            for layer in image.get_layers():
                row_widget = Gtk.ListBoxRow()
                hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=6)
                check = Gtk.CheckButton()
                check.set_active(layer.get_id() == target_id)
                check.set_sensitive(layer.get_id() != target_id)
                check.set_tooltip_text(_("Include this layer as a reference image"))
                num_label = Gtk.Label(label="")
                num_label.set_width_chars(9)
                num_label.set_halign(Gtk.Align.START)
                name_label = Gtk.Label(label=layer.get_name())
                name_label.set_halign(Gtk.Align.START)
                name_label.set_hexpand(True)
                hbox.pack_start(check, False, False, 0)
                hbox.pack_start(num_label, False, False, 0)
                hbox.pack_start(name_label, True, True, 0)
                row_widget.add(hbox)
                ref_list.add(row_widget)
                check.connect("toggled",
                              lambda *a: _update_ref_numbers())
                layer_rows.append((layer.get_id(), check, num_label))

        multi_layer_hint = Gtk.Label(label="")
        multi_layer_hint.set_halign(Gtk.Align.START)
        multi_layer_hint.set_hexpand(True)
        multi_layer_hint.set_line_wrap(True)
        multi_defaults_ready = [False]

        multi_layer_check = Gtk.CheckButton.new_with_mnemonic(
            _("Multi-layer e_dit (send layers as image 1, image 2, ...)")
        )
        multi_layer_check.set_halign(Gtk.Align.START)
        multi_layer_check.set_valign(Gtk.Align.CENTER)
        multi_layer_check.set_tooltip_text(_("Send individual layers to the server as separate reference images. In the prompt, refer to each checked layer positionally as image 1, image 2, etc. The layer being edited is always included. Checked layers appear in layer-stack order."))

        def _apply_multi_layer_defaults():
            if not multi_defaults_ready[0]:
                return
            if multi_layer_check.get_active():
                if denoise_spin.get_value() <= 0.8:
                    denoise_spin.set_value(0.9)
                if cfg_spin.get_value() <= 1.5:
                    cfg_spin.set_value(3.0)
                multi_layer_hint.set_text(_("Init Strength ~0.9 and Guidance > 1.0 recommended."))
            else:
                multi_layer_hint.set_text("")

        def on_multi_layer_changed(cb):
            config.set_property("multi-layer", cb.get_active())
            ref_scrolled.set_sensitive(cb.get_active())
            _apply_multi_layer_defaults()

        multi_layer_check.connect("toggled", on_multi_layer_changed)
        multi_layer_check.set_active(
            config.get_property("multi-layer") or False
        )
        _update_ref_numbers()
        ref_scrolled.set_sensitive(multi_layer_check.get_active())
        grid.attach(multi_layer_check, 0, row, 2, 1)
        row += 1
        grid.attach(ref_scrolled, 0, row, 2, 1)
        row += 1
        grid.attach(multi_layer_hint, 0, row, 2, 1)
        row += 1

        # --- Width / Height (same line) ---
        width_height_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        width_label = Gtk.Label.new_with_mnemonic(_("_Width:"))
        width_label.set_halign(Gtk.Align.START)
        width_label.set_hexpand(False)
        width_adj = Gtk.Adjustment(
            value=config.get_property("width") or 1024,
            lower=64, upper=8192, step_increment=64, page_increment=64
        )
        width_spin = Gtk.SpinButton(adjustment=width_adj)
        width_spin.set_valign(Gtk.Align.CENTER)
        width_spin.set_tooltip_text(_("Output image width in pixels (64–8192)"))
        width_label.set_mnemonic_widget(width_spin)

        def on_width_changed(s):
            config.set_property("width", s.get_value_as_int())

        width_spin.connect("value-changed", on_width_changed)
        width_height_hbox.pack_start(width_label, False, False, 0)
        width_height_hbox.pack_start(width_spin, False, False, 0)

        height_label = Gtk.Label.new_with_mnemonic(_("_Height:"))
        height_label.set_halign(Gtk.Align.START)
        height_label.set_hexpand(True)
        height_adj = Gtk.Adjustment(
            value=config.get_property("height") or 1024,
            lower=64, upper=8192, step_increment=64, page_increment=64
        )
        height_spin = Gtk.SpinButton(adjustment=height_adj)
        height_spin.set_valign(Gtk.Align.CENTER)
        height_spin.set_tooltip_text(_("Output image height in pixels (64–8192)"))
        height_label.set_mnemonic_widget(height_spin)

        def on_height_changed(s):
            config.set_property("height", s.get_value_as_int())

        height_spin.connect("value-changed", on_height_changed)
        width_height_hbox.pack_start(height_label, False, False, 0)
        width_height_hbox.pack_start(height_spin, False, False, 0)

        grid.attach(width_height_hbox, 0, row, 2, 1)
        row += 1

        # --- Steps / Guidance (same line) ---
        steps_guidance_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        steps_label = Gtk.Label.new_with_mnemonic(_("_Steps:"))
        steps_label.set_halign(Gtk.Align.START)
        steps_label.set_hexpand(False)
        steps_adj = Gtk.Adjustment(
            value=config.get_property("steps") or 30,
            lower=1, upper=100, step_increment=1, page_increment=10
        )
        steps_spin = Gtk.SpinButton(adjustment=steps_adj)
        steps_spin.set_valign(Gtk.Align.CENTER)
        steps_spin.set_tooltip_text(_("Number of sampling steps — higher values generally improve quality but take longer (1–100)"))
        steps_label.set_mnemonic_widget(steps_spin)

        def on_steps_changed(s):
            config.set_property("steps", s.get_value_as_int())

        steps_spin.connect("value-changed", on_steps_changed)
        steps_guidance_hbox.pack_start(steps_label, False, False, 0)
        steps_guidance_hbox.pack_start(steps_spin, False, False, 0)

        cfg_label = Gtk.Label.new_with_mnemonic(_("_Guidance:"))
        cfg_label.set_halign(Gtk.Align.START)
        cfg_label.set_hexpand(True)
        cfg_adj = Gtk.Adjustment(
            value=config.get_property("cfg-scale") or 7.0,
            lower=1.0, upper=30.0, step_increment=0.5, page_increment=5
        )
        cfg_spin = Gtk.SpinButton(adjustment=cfg_adj, digits=1)
        cfg_spin.set_valign(Gtk.Align.CENTER)
        cfg_spin.set_tooltip_text(_("cfg_scale — how strongly the prompt is followed (1=creative, ~30=strict)"))
        cfg_label.set_mnemonic_widget(cfg_spin)

        def on_cfg_changed(s):
            config.set_property("cfg-scale", s.get_value())

        cfg_spin.connect("value-changed", on_cfg_changed)
        steps_guidance_hbox.pack_start(cfg_label, False, False, 0)
        steps_guidance_hbox.pack_start(cfg_spin, False, False, 0)

        grid.attach(steps_guidance_hbox, 0, row, 2, 1)
        row += 1

        # --- Seed / Init Strength (same line) ---
        seed_denoise_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        seed_label = Gtk.Label.new_with_mnemonic(_("_Seed:"))
        seed_label.set_halign(Gtk.Align.START)
        seed_label.set_hexpand(False)
        seed_adj = Gtk.Adjustment(
            value=config.get_property("seed") or -1,
            lower=-1, upper=2147483647, step_increment=1, page_increment=64
        )
        seed_spin = Gtk.SpinButton(adjustment=seed_adj)
        seed_spin.set_valign(Gtk.Align.CENTER)
        seed_spin.set_tooltip_text(_("RNG seed (-1 for random) — use the same seed to reproduce results"))
        seed_label.set_mnemonic_widget(seed_spin)

        def on_seed_changed(s):
            config.set_property("seed", s.get_value_as_int())

        seed_spin.connect("value-changed", on_seed_changed)
        seed_denoise_hbox.pack_start(seed_label, False, False, 0)
        seed_denoise_hbox.pack_start(seed_spin, False, False, 0)

        denoise_label = Gtk.Label.new_with_mnemonic(_("_Init Strength:"))
        denoise_label.set_halign(Gtk.Align.START)
        denoise_label.set_hexpand(True)
        denoise_adj = Gtk.Adjustment(
            value=config.get_property("denoising-strength") or 0.75,
            lower=0.0, upper=1.0, step_increment=0.05, page_increment=0.1
        )
        denoise_spin = Gtk.SpinButton(adjustment=denoise_adj, digits=2)
        denoise_spin.set_valign(Gtk.Align.CENTER)
        denoise_spin.set_tooltip_text(_("denoising_strength — how much the original image is altered (0=unchanged, 1=fully replaced)"))
        denoise_label.set_mnemonic_widget(denoise_spin)

        def on_denoise_changed(s):
            config.set_property("denoising-strength", s.get_value())

        denoise_spin.connect("value-changed", on_denoise_changed)
        seed_denoise_hbox.pack_start(denoise_label, False, False, 0)
        seed_denoise_hbox.pack_start(denoise_spin, False, False, 0)

        grid.attach(seed_denoise_hbox, 0, row, 2, 1)
        row += 1

        # --- Sampler ---
        sampler_label = Gtk.Label.new_with_mnemonic(_("_Sampler:"))
        sampler_label.set_halign(Gtk.Align.START)
        sampler_label.set_hexpand(False)
        sampler_combo = Gtk.ComboBoxText()
        SAMPLER_NAMES = [
            "euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m",
            "dpm++2mv2", "ipndm", "ipndm_v", "lcm", "ddim_trailing", "tcd",
            "res_multistep", "res_2s", "er_sde", "euler_cfg_pp",
            "euler_a_cfg_pp", "euler_ge",
        ]
        for name in SAMPLER_NAMES:
            sampler_combo.append_text(name)
        current_sampler = config.get_property("sampler-name") or "euler"
        if current_sampler in SAMPLER_NAMES:
            sampler_combo.set_active(SAMPLER_NAMES.index(current_sampler))
        else:
            sampler_combo.set_active(0)
        sampler_combo.set_hexpand(True)
        sampler_combo.set_valign(Gtk.Align.CENTER)
        sampler_combo.set_tooltip_text(_("Sampling method — determines how noise is removed each step"))
        sampler_label.set_mnemonic_widget(sampler_combo)

        def on_sampler_changed(cb):
            config.set_property("sampler-name", cb.get_active_text())

        sampler_combo.connect("changed", on_sampler_changed)
        grid.attach(sampler_label, 0, row, 1, 1)
        grid.attach(sampler_combo, 1, row, 1, 1)
        row += 1

        # --- Prompt ---
        prompt_label = Gtk.Label.new_with_mnemonic(_("_Prompt:"))
        prompt_label.set_halign(Gtk.Align.START)
        prompt_label.set_valign(Gtk.Align.START)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(120)

        text_view = Gtk.TextView()
        text_view.set_tooltip_text(_("Describe the image you want to generate — be as detailed as you like"))
        text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        text_view.set_hexpand(True)
        text_view.set_vexpand(True)
        prompt_text = config.get_property("prompt")
        if prompt_text:
            text_view.get_buffer().set_text(prompt_text, len(prompt_text))

        def on_text_changed(buf):
            config.set_property("prompt",
                                buf.get_text(buf.get_start_iter(),
                                             buf.get_end_iter(), False))

        text_view.get_buffer().connect("changed", on_text_changed)
        prompt_label.set_mnemonic_widget(text_view)

        def on_text_view_key(view, event, dlg):
            if (event.keyval == Gdk.KEY_Return and
                    (event.state & Gdk.ModifierType.CONTROL_MASK)):
                dlg.response(Gtk.ResponseType.OK)
                return True
            return False

        text_view.connect("key-press-event", on_text_view_key, dialog)

        scrolled.add(text_view)
        grid.attach(prompt_label, 0, row, 1, 1)
        grid.attach(scrolled, 1, row, 1, 1)
        row += 1

        # --- Negative prompt ---
        neg_label = Gtk.Label.new_with_mnemonic(_("_Negative Prompt:"))
        neg_label.set_halign(Gtk.Align.START)
        neg_label.set_valign(Gtk.Align.START)
        neg_scrolled = Gtk.ScrolledWindow()
        neg_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        neg_scrolled.set_hexpand(True)
        neg_scrolled.set_vexpand(True)
        neg_scrolled.set_min_content_height(60)

        neg_text_view = Gtk.TextView()
        neg_text_view.set_tooltip_text(_("Things to exclude from the generated image (optional)"))
        neg_text_view.set_wrap_mode(Gtk.WrapMode.WORD)
        neg_text_view.set_hexpand(True)
        neg_text_view.set_vexpand(True)
        neg_text = config.get_property("negative-prompt")
        if neg_text:
            neg_text_view.get_buffer().set_text(neg_text, len(neg_text))

        def on_neg_text_changed(buf):
            config.set_property("negative-prompt",
                                buf.get_text(buf.get_start_iter(),
                                             buf.get_end_iter(), False))

        neg_text_view.get_buffer().connect("changed", on_neg_text_changed)
        neg_label.set_mnemonic_widget(neg_text_view)

        def on_neg_text_view_key(view, event, dlg):
            if (event.keyval == Gdk.KEY_Return and
                    (event.state & Gdk.ModifierType.CONTROL_MASK)):
                dlg.response(Gtk.ResponseType.OK)
                return True
            return False

        neg_text_view.connect("key-press-event", on_neg_text_view_key, dialog)

        neg_scrolled.add(neg_text_view)
        grid.attach(neg_label, 0, row, 1, 1)
        grid.attach(neg_scrolled, 1, row, 1, 1)
        row += 1

        multi_defaults_ready[0] = True
        _apply_multi_layer_defaults()
        dialog.show_all()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            selected_ids = [str(lid) for lid, check, _nl in layer_rows
                            if check.get_active()]
            config.set_property("ref-layers", ",".join(selected_ids))
        dialog.destroy()
        # gtk_widget_destroy() only queues the teardown; flush it so the dead
        # window does not linger (as "not responding") while the server runs.
        display = Gdk.Display.get_default()
        if display is not None:
            display.flush()

        if response != Gtk.ResponseType.OK:
            return procedure.new_return_values(
                Gimp.PDBStatusType.CANCEL, GLib.Error()
            )

    # Determine if we should generate a new image
    generate_new = config.get_property("generate-new")
    no_drawables = not drawables

    if generate_new or no_drawables:
        return _generate_new(procedure, run_mode, image, config)
    else:
        return _edit_layer(procedure, run_mode, image, drawables, config)


def _generate_new(procedure, run_mode, image, config):
    """Generate a new image via the SD server API."""
    server_url = config.get_property("server-url") or DEFAULT_SERVER_URL
    prompt = config.get_property("prompt") or ""
    width = config.get_property("width") or 1024
    height = config.get_property("height") or 1024
    steps = config.get_property("steps") or 30
    cfg_scale = config.get_property("cfg-scale") or 7.0
    negative_prompt = config.get_property("negative-prompt") or ""
    seed = config.get_property("seed")
    sampler = config.get_property("sampler-name") or "euler"

    output_path = None

    try:
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"gimp_sdserver_{os.getpid()}.png"

        Gimp.progress_init(_("Generating image via SD server..."))
        Gimp.progress_pulse()

        payload = {
            "prompt": prompt,
            "steps": steps,
            "width": width,
            "height": height,
            "cfg_scale": cfg_scale,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None and seed >= 0:
            payload["seed"] = seed
        if sampler:
            payload["sampler_name"] = sampler

        import requests
        response = requests.post(
            f"{server_url}/sdapi/v1/txt2img",
            json=payload,
            timeout=600,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("images"):
            raise RuntimeError("No images in API response")

        import io
        import base64
        from PIL import Image

        image_data = Image.open(
            io.BytesIO(base64.b64decode(data["images"][0]))
        )
        image_data.save(str(output_path))

        Gimp.progress_set_text(_("Loading generated image..."))

        imported_image = Gimp.file_load(
            run_mode, Gio.File.new_for_path(str(output_path))
        )
        if not imported_image:
            raise RuntimeError("Failed to load generated image")

        Gimp.Display.new(imported_image)

    except Exception as e:
        Gimp.message(_("Error: %s") % e)
        return procedure.new_return_values(
            Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(e))
        )

    finally:
        if output_path and output_path.exists():
            os.remove(output_path)

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


def _edit_layer(procedure, run_mode, image, drawables, config):
    """Edit the current layer via the SD server API."""
    server_url = config.get_property("server-url") or DEFAULT_SERVER_URL
    prompt = config.get_property("prompt") or ""
    negative_prompt = config.get_property("negative-prompt") or ""
    steps = config.get_property("steps") or 30
    cfg_scale = config.get_property("cfg-scale") or 7.0
    denoising = config.get_property("denoising-strength") or 0.75
    seed = config.get_property("seed")
    sampler = config.get_property("sampler-name") or "euler"

    multi_layer = config.get_property("multi-layer") or False
    if multi_layer:
        # Placing objects into a scene needs near-full denoising and enough
        # guidance to follow the instruction instead of merging the layers.
        if denoising < 0.85:
            denoising = 1.0
        if cfg_scale <= 1.5:
            cfg_scale = 7.0

    Gimp.context_push()
    image.undo_group_start()

    if not drawables:
        return procedure.new_return_values(
            Gimp.PDBStatusType.EXECUTION_ERROR,
            GLib.Error("No drawable provided.")
        )

    drawable = drawables[0]
    input_path = None
    output_path = None
    mask_path = None

    # Check server capabilities to pick the right endpoint
    caps = _fetch_capabilities(server_url)
    model_name = (caps.get("model", {}) or {}).get("stem", "") if caps else ""
    supports_init_image = caps.get("features", {}).get("init_image", False) if caps else False

    # Vision-based edit models (Boogu, Qwen Image Edit) use the native
    # sdcpp backend with init_image for reference conditioning.
    is_vision_edit = any(kw in model_name.lower() for kw in
                         ["boogu-edit", "boogu_image", "qwen-image-edit",
                          "qwen_image_edit", "longcat-image-edit"])
    is_kontext = "kontext" in model_name.lower()
    # Models that support ref_images need both init_image and extra_images
    supports_ref = caps.get("features", {}).get("ref_images", False) if caps else False

    try:
        temp_dir = Path(tempfile.gettempdir())
        input_path = temp_dir / f"gimp_sdserver_input_{os.getpid()}.png"
        output_path = temp_dir / f"gimp_sdserver_output_{os.getpid()}.png"

        Gimp.progress_init(_("Saving layer for editing..."))

        Gimp.file_save(
            Gimp.RunMode.NONINTERACTIVE,
            image,
            Gio.File.new_for_path(str(input_path)),
            None
        )

        Gimp.progress_set_text(_("Sending to SD server for editing..."))

        Gimp.progress_pulse()

        import requests
        import base64
        from PIL import Image
        import io

        with open(input_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        # Export the current selection (marching ants or quick mask) as an
        # inpaint mask when requested and a non-empty selection exists.
        mask_b64 = None
        if config.get_property("use-selection"):
            mask_path = temp_dir / f"gimp_sdserver_mask_{os.getpid()}.png"
            if export_selection_mask(image, mask_path):
                with open(mask_path, "rb") as f:
                    mask_b64 = base64.b64encode(f.read()).decode("utf-8")
            else:
                mask_path = None

        # Vision edit models (Boogu, Qwen, Longcat) and Kontext/ref models
        # need extra_images for reference conditioning. When multi-layer edit
        # is enabled, send each selected layer as its own reference image so
        # the prompt can refer to them positionally (image 1, 2, ...).
        extra_images = [img_b64]
        if (multi_layer and
                (is_vision_edit or is_kontext or supports_ref)):
            ref_ids = config.get_property("ref-layers") or ""
            layers_by_id = {layer.get_id(): layer
                            for layer in image.get_layers()}
            ref_layers = []
            for sid in ref_ids.split(","):
                sid = sid.strip()
                if sid.isdigit() and int(sid) in layers_by_id:
                    ref_layers.append(layers_by_id[int(sid)])
            if ref_layers:
                extra_images = []
                for i, layer in enumerate(ref_layers):
                    ref_path = temp_dir / (
                        f"gimp_sdserver_ref_{os.getpid()}_{i}.png"
                    )
                    export_layer_png(image, layer, ref_path)
                    with open(ref_path, "rb") as f:
                        extra_images.append(
                            base64.b64encode(f.read()).decode("utf-8")
                        )
                    ref_path.unlink(missing_ok=True)

        if is_vision_edit or is_kontext or supports_ref:
            payload = {
                "prompt": prompt,
                "init_images": [img_b64],
                "extra_images": extra_images,
                "denoising_strength": denoising,
                "steps": steps,
                "cfg_scale": cfg_scale,
            }
            if seed is not None and seed >= 0:
                payload["seed"] = seed
            if sampler:
                payload["sampler_name"] = sampler
        else:
            # Standard SD img2img: init_images + denoising_strength.
            payload = {
                "prompt": prompt,
                "init_images": [img_b64],
                "steps": steps,
                "cfg_scale": cfg_scale,
                "denoising_strength": denoising,
            }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None and seed >= 0:
            payload["seed"] = seed
        if sampler:
            payload["sampler_name"] = sampler
        if mask_b64:
            payload["mask"] = mask_b64

        # Match the edit output to the source canvas so the new layer does not
        # resize the image. The width/height config only applies to generation.
        payload["width"] = image.get_width()
        payload["height"] = image.get_height()

        response = requests.post(
            f"{server_url}/sdapi/v1/img2img",
            json=payload,
            timeout=600,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("images"):
            raise RuntimeError("No images in API response")
        image_data = Image.open(
            io.BytesIO(base64.b64decode(data["images"][0]))
        )
        image_data.save(str(output_path))

        Gimp.progress_set_text(_("Loading edited image..."))

        imported_image = Gimp.file_load(
            run_mode, Gio.File.new_for_path(str(output_path))
        )
        if not imported_image:
            raise RuntimeError("Failed to load edited image")

        new_layer = imported_image.get_layers()[0]
        layer_type = Gimp.ImageType.RGBA_IMAGE

        final_layer = Gimp.Layer.new(
            image,
            _("SD Server Edit"),
            new_layer.get_width(),
            new_layer.get_height(),
            layer_type,
            100.0,
            Gimp.LayerMode.NORMAL
        )

        drawable = drawables[0]
        image.insert_layer(
            final_layer, drawable.get_parent(),
            image.get_item_position(drawable)
        )

        Gimp.progress_set_text(_("Transferring layer data..."))

        paste_layer_aligned(image, final_layer, new_layer)

        Gimp.Image.delete(imported_image)

        final_layer.set_name(_("SD Server"))
        final_layer.set_visible(True)
        drawable.set_visible(False)
        image.active_layer = final_layer
        image.resize_to_layers()

    except Exception as e:
        Gimp.message(_("Error: %s") % e)
        return procedure.new_return_values(
            Gimp.PDBStatusType.EXECUTION_ERROR, GLib.Error(str(e))
        )

    finally:
        if input_path and input_path.exists():
            os.remove(input_path)
        if output_path and output_path.exists():
            os.remove(output_path)
        if mask_path and mask_path.exists():
            os.remove(mask_path)

    Gimp.displays_flush()
    image.undo_group_end()
    Gimp.context_pop()

    return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, GLib.Error())


class SDServer(Gimp.PlugIn):
    __gtype_name__ = "SDServer"

    def do_set_i18n(self, procname):
        return True, PLUGIN_NAME, None

    def do_query_procedures(self):
        return ["python-fu-sd-server"]

    def do_create_procedure(self, name):
        Gegl.init(None)

        procedure = Gimp.ImageProcedure.new(
            self, name, Gimp.PDBProcType.PLUGIN, sd_server_func, None
        )

        # Available with or without an image (for generation mode)
        procedure.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.ALWAYS
        )
        procedure.set_image_types("*")

        procedure.set_documentation(
            _("SD Server"),
            _("Edit the current layer or generate a new image using a "
              "local stable-diffusion.cpp server API."),
            name,
        )
        procedure.set_menu_label(_("_SD Server..."))
        procedure.set_attribution(
            "stable-diffusion.cpp (API), Henry Kroll III (Plugin)",
            "Henry Kroll III",
            "2026"
        )
        procedure.add_menu_path("<Image>/Filters/AI")

        # Server URL
        procedure.add_string_argument(
            "server-url", _("Server URL"),
            _("URL of the stable-diffusion.cpp server"),
            DEFAULT_SERVER_URL, GObject.ParamFlags.READWRITE,
        )
        # Generate new image flag
        procedure.add_boolean_argument(
            "generate-new", _("Generate new"),
            _("Generate a new image instead of editing the current layer"),
            False, GObject.ParamFlags.READWRITE,
        )
        # Use selection as inpaint mask (edit mode)
        procedure.add_boolean_argument(
            "use-selection", _("Use _selection as mask"),
            _("Restrict edits to the current selection (inpaint)"),
            True, GObject.ParamFlags.READWRITE,
        )
        # Multi-layer edit flag (edit mode)
        procedure.add_boolean_argument(
            "multi-layer", _("Multi-layer _edit"),
            _("Send selected layers as separate reference images"),
            False, GObject.ParamFlags.READWRITE,
        )
        # Reference layer IDs (comma-separated, edit mode)
        procedure.add_string_argument(
            "ref-layers", _("Reference layers"),
            _("Comma-separated layer IDs used as reference images"),
            "", GObject.ParamFlags.READWRITE,
        )
        # Width
        procedure.add_int_argument(
            "width", _("_Width"), _("Image width in pixels"),
            64, 8192, 1024, GObject.ParamFlags.READWRITE,
        )
        # Height
        procedure.add_int_argument(
            "height", _("_Height"), _("Image height in pixels"),
            64, 8192, 1024, GObject.ParamFlags.READWRITE,
        )
        # Steps
        procedure.add_int_argument(
            "steps", _("_Steps"), _("Number of sampling steps"),
            1, 100, 30, GObject.ParamFlags.READWRITE,
        )
        # CFG scale
        procedure.add_double_argument(
            "cfg-scale", _("CFG S_cale"),
            _("Classifier-free guidance scale"),
            1.0, 30.0, 7.0, GObject.ParamFlags.READWRITE,
        )
        # Seed
        procedure.add_int_argument(
            "seed", _("_Seed"),
            _("RNG seed (-1 for random)"),
            -1, 2147483647, -1, GObject.ParamFlags.READWRITE,
        )
        # Sampler name
        procedure.add_string_argument(
            "sampler-name", _("_Sampler"),
            _("Sampler method (euler, heun, dpm++2m, lcm, etc.)"),
            "euler", GObject.ParamFlags.READWRITE,
        )
        # Denoising strength
        procedure.add_double_argument(
            "denoising-strength", _("_Denoising"),
            _("img2img denoising strength (0=no change, 1=full change)"),
            0.0, 1.0, 0.75, GObject.ParamFlags.READWRITE,
        )
        # Prompt
        procedure.add_string_argument(
            "prompt", _("_Prompt"),
            _("Describe the image you want to generate or edit"),
            "", GObject.ParamFlags.READWRITE,
        )
        # Negative prompt
        procedure.add_string_argument(
            "negative-prompt", _("_Negative Prompt"),
            _("Things you don't want in the image"),
            "", GObject.ParamFlags.READWRITE,
        )

        return procedure


Gimp.main(SDServer.__gtype_name__, sys.argv)
