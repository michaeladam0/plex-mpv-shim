"""
Registry + downloader for shader profiles that aren't bundled with the shim.

Everything here is fetched on demand into the user's config directory, verified
against a pinned SHA-256, and accompanied by its upstream licence. URLs are
pinned to an immutable commit so the content can't change under us.

Currently: ArtCNN (https://github.com/Artoriuz/ArtCNN, MIT) -- a modern neural
upscaler. Note: ArtCNN requires mpv's gpu-next video output.
"""

import os
import ssl
import hashlib
import logging
import urllib.request

import certifi

from . import conffile

log = logging.getLogger("downloadable_shaders")

APP_NAME = "plex-mpv-shim"

# Pinned to an immutable commit so the raw URLs and hashes stay valid.
_ARTCNN_COMMIT = "a91902eeb2f8dc37bdd42892dc502211c6de5525"
_ARTCNN_RAW = "https://raw.githubusercontent.com/Artoriuz/ArtCNN/" + _ARTCNN_COMMIT + "/"

# ArtCNN is MIT-licensed; fetched alongside every ArtCNN shader and kept next to
# the files. (Each shader also carries the notice in its header.)
_ARTCNN_LICENSE = {
    "url": _ARTCNN_RAW + "LICENSE",
    "sha256": "285055c4b8b8bcb891685ca9cc6047baf25a8c1cf56e8ab2cbcdecaea0437c0b",
    "dest": "ArtCNN-LICENSE.txt",
}


def _artcnn(shader_file, sha256):
    return {
        "url": _ARTCNN_RAW + "GLSL/" + shader_file,
        "sha256": sha256,
        "dest": shader_file,
    }


# Each entry is a selectable shader profile that stores its ``key``.
DOWNLOADABLE = [
    {
        "key": "artcnn-c4f16",
        "display": "ArtCNN C4F16 (fast)",
        "shaders": ["ArtCNN_C4F16.glsl"],
        "files": [_artcnn("ArtCNN_C4F16.glsl",
                          "03d0b3d31cb82c898a94a46663021a3e8f02c5a21d69c5cfdf0208de4bfd453e")],
        "requires_gpu_next": True,
        "family": "ArtCNN",
        "license": "MIT",
        "source": "github.com/Artoriuz/ArtCNN",
        "summary": "ArtCNN C4F16 — fast modern neural upscaler; strong quality for the cost. Needs gpu-next.",
    },
    {
        "key": "artcnn-c4f16-ds",
        "display": "ArtCNN C4F16 DS (fast, denoise+sharpen)",
        "shaders": ["ArtCNN_C4F16_DS.glsl"],
        "files": [_artcnn("ArtCNN_C4F16_DS.glsl",
                          "57df650fddec3969e17799f5522c9b03dd2d33b1aeace237fef216bf3858125a")],
        "requires_gpu_next": True,
        "family": "ArtCNN",
        "license": "MIT",
        "source": "github.com/Artoriuz/ArtCNN",
        "summary": "ArtCNN C4F16 DS — fast neural upscaler that also denoises and sharpens. Needs gpu-next.",
    },
    {
        "key": "artcnn-c4f32",
        "display": "ArtCNN C4F32 (quality)",
        "shaders": ["ArtCNN_C4F32.glsl"],
        "files": [_artcnn("ArtCNN_C4F32.glsl",
                          "f773bce6cf5fe7e5e5d599a695edd40df5cd7a20c3d08c4d164d07591d5bead3")],
        "requires_gpu_next": True,
        "family": "ArtCNN",
        "license": "MIT",
        "source": "github.com/Artoriuz/ArtCNN",
        "summary": "ArtCNN C4F32 — higher-quality neural upscaler; heavier than C4F16. Needs gpu-next.",
    },
    {
        "key": "artcnn-c4f32-ds",
        "display": "ArtCNN C4F32 DS (quality, denoise+sharpen)",
        "shaders": ["ArtCNN_C4F32_DS.glsl"],
        "files": [_artcnn("ArtCNN_C4F32_DS.glsl",
                          "a04c9cba6fbb8e6db9239d61848390208aedf8e348ef116e12174c803d22077e")],
        "requires_gpu_next": True,
        "family": "ArtCNN",
        "license": "MIT",
        "source": "github.com/Artoriuz/ArtCNN",
        "summary": "ArtCNN C4F32 DS — top-quality neural upscaler that also denoises and sharpens; heavier. Needs gpu-next.",
    },
]

_BY_KEY = {entry["key"]: entry for entry in DOWNLOADABLE}


def by_key(key):
    return _BY_KEY.get(key)


def is_downloadable(key):
    return key in _BY_KEY


def download_dir():
    return os.path.join(conffile.confdir(APP_NAME), "downloaded_shaders")


def is_downloaded(key):
    entry = _BY_KEY.get(key)
    if not entry:
        return False
    directory = download_dir()
    return all(os.path.isfile(os.path.join(directory, s)) for s in entry["shaders"])


def shader_paths(key):
    """Absolute paths of the downloaded shader files for a profile key."""
    directory = download_dir()
    return [os.path.join(directory, s) for s in _BY_KEY[key]["shaders"]]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(spec, directory, context):
    dest = os.path.join(directory, spec["dest"])
    if os.path.isfile(dest) and _sha256(dest) == spec["sha256"]:
        return
    log.info("Downloading shader asset %s", spec["url"])
    data = urllib.request.urlopen(spec["url"], timeout=30, context=context).read()
    got = hashlib.sha256(data).hexdigest()
    if got != spec["sha256"]:
        raise ValueError("Checksum mismatch for %s (expected %s, got %s)"
                         % (spec["dest"], spec["sha256"], got))
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def download(key):
    """
    Download and verify every file for a profile (plus its licence). Raises on
    a network error or checksum mismatch; files are only committed once their
    hash matches.
    """
    entry = _BY_KEY.get(key)
    if not entry:
        raise KeyError(key)
    directory = download_dir()
    os.makedirs(directory, exist_ok=True)
    context = ssl.create_default_context(cafile=certifi.where())
    specs = list(entry["files"])
    if entry.get("license") == "MIT":
        specs.append(_ARTCNN_LICENSE)
    for spec in specs:
        _fetch(spec, directory, context)
    return True
