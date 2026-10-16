#!/usr/bin/env python3
"""Shared helpers for locating and downloading model checkpoints through
huggingface_hub, reusable by any GIMP plugin.

Resolution chain (see `resolve_model`): Hugging Face cache -> a
plugin-supplied local disk search (silently adopting any match into the hub
cache so huggingface_hub manages it from then on) -> download.
"""

import os
import shutil
import threading
from pathlib import Path


def hub_cache_dir(override=None):
    """Directory huggingface_hub uses for cached files. Honors HF_HOME /
    HF_HUB_CACHE; `override` (e.g. read from an env var) wins when set."""
    from huggingface_hub.constants import HF_HUB_CACHE
    if override:
        return Path(override)
    return Path(HF_HUB_CACHE)


def cached_file(repo_id, filename, cache=None):
    """Return the local path of `filename` if huggingface_hub already caches
    it, else None."""
    from huggingface_hub import try_to_load_from_cache
    cached = try_to_load_from_cache(repo_id=repo_id, filename=filename,
                                    cache_dir=str(cache))
    if isinstance(cached, str) and Path(cached).is_file():
        return Path(cached)
    return None


def _repo_dir(repo_id, cache):
    from huggingface_hub.file_download import repo_folder_name
    return cache / repo_folder_name(repo_id=repo_id, repo_type='model')


def adopt_into_cache(repo_id, filename, src, cache=None):
    """Silently register a locally-found file into the huggingface_hub cache
    so the hub manages it from now on (hardlinked when the filesystem allows,
    copied otherwise). Returns the hub-managed path, or the original when
    adoption is unnecessary or fails."""
    cache = Path(cache)
    repo_dir = _repo_dir(repo_id, cache)
    src = Path(src)
    try:
        if repo_dir.resolve() in src.resolve().parents:
            return src  # already inside the HF cache
    except OSError:
        return src
    snapshots = repo_dir / 'snapshots' / 'main'
    dest = snapshots / filename
    if dest.is_file() or dest.resolve() == src.resolve():
        return dest
    try:
        snapshots.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dest)  # keep the data where it already lives
        except OSError:
            shutil.copy2(src, dest)
        return dest
    except OSError:
        return src


def download_url(repo_id, filename):
    return ("https://huggingface.co/%s/resolve/main/%s"
            % (repo_id, filename))


def download_file(repo_id, filename, cache=None, progress=None):
    """Fetch a file through huggingface_hub into the HF cache (idempotent and
    resumable). Returns the local path. `progress` is an optional
    callable(fraction)."""
    import requests
    from huggingface_hub import hf_hub_download
    cache = Path(cache)

    # Total size for the percentage shown while downloading; 0 when the HEAD
    # request fails and percentage progress is skipped.
    total = 0
    try:
        head = requests.head(download_url(repo_id, filename),
                             allow_redirects=True, timeout=30)
        total = int(head.headers.get('Content-Length') or 0)
    except Exception:
        total = 0

    outcome = {}

    def _run():
        try:
            outcome['path'] = hf_hub_download(
                repo_id=repo_id, filename=filename, cache_dir=str(cache))
        except Exception as e:
            outcome['error'] = e

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # hf_hub_download streams into <cache>/models--<repo>/blobs/*.incomplete;
    # poll its size from this thread and report the fraction done.
    blobs = _repo_dir(repo_id, cache) / 'blobs'
    seen = -1
    while worker.is_alive():
        got = 0
        if blobs.is_dir():
            try:
                for partial in blobs.glob('*.incomplete'):
                    try:
                        got = max(got, partial.stat().st_size)
                    except OSError:
                        pass
            except OSError:
                pass
        if got != seen:
            seen = got
            if progress and total > 0 and got > 0:
                progress(min(1.0, got / float(total)))
        worker.join(timeout=0.1)
    worker.join()
    if 'error' in outcome:
        raise outcome['error']
    return outcome['path']


def resolve_model(repo_id, filename, cache=None, download=True, progress=None,
                  local_search=None):
    """HF cache > local search (adopted into the HF cache) > download.
    `local_search` is an optional callable(repo_id, filename) -> Path|None
    consulted when the cache misses; a match is silently adopted into the
    cache. `progress` is an optional callable(fraction). Raises RuntimeError
    when nothing resolves."""
    cache = Path(cache)
    managed = cached_file(repo_id, filename, cache)
    if managed:
        return managed
    if local_search is not None:
        found = local_search(repo_id, filename)
        if found:
            return adopt_into_cache(repo_id, filename, found, cache)
    if download:
        try:
            return download_file(repo_id, filename, cache, progress=progress)
        except Exception as e:
            raise RuntimeError(
                "Failed to download %s from %s into %s: %s"
                % (filename, download_url(repo_id, filename), cache, e))
    raise RuntimeError(
        "No %s checkpoint found. Enable download to fetch it from "
        "Hugging Face." % filename)
