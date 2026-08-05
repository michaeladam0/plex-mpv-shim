"""
Human-readable guidance for the built-in shader profiles, shown in the
preferences window (a one-line summary under the dropdown, and a full
comparison in the "Shader guide" dialog).

Kept as plain data so the UI code just renders it. Ratings are deliberately
coarse and partly subjective -- they're meant to steer a choice, not to be
benchmarks.
"""

# One comparison card per shader family.
SHADER_FAMILIES = [
    {
        "name": "Nvidia Image Scaler (NIS)",
        "perf": "Light",
        "impact": "Moderate",
        "best_for": "General upscaling on low-to-mid GPUs; live-action or mixed content.",
        "pros": [
            "Very cheap — runs on almost any GPU.",
            "Sharpens as it upscales.",
            "Safe, well-behaved all-rounder.",
        ],
        "cons": [
            "Doesn't reconstruct detail like neural upscalers.",
            "Over-sharpening can look slightly artificial.",
        ],
        "note": ("A sensible default if you're unsure where to start. Despite the "
                 "Nvidia name it's an open shader that runs on any GPU (AMD/Intel "
                 "included) — not Nvidia-only."),
        "profiles": ["nvscaler"],
    },
    {
        "name": "AMD FidelityFX Super Resolution (FSR)",
        "perf": "Light",
        "impact": "Moderate",
        "best_for": "Upscaling lower-resolution sources to your display; edge-aware.",
        "pros": [
            "Cheap, edge-adaptive upscaling.",
            "Keeps edges clean.",
            "Widely used and well understood.",
        ],
        "cons": [
            "Spatial only — no detail reconstruction.",
            "Can shimmer on fine textures.",
        ],
        "note": ("Similar niche to NIS — try both and keep whichever looks better "
                 "to you. Despite the AMD name the shader runs on any GPU, not "
                 "just AMD."),
        "profiles": ["AMD FidelityFX Super Resolution"],
    },
    {
        "name": "AMD FidelityFX CAS",
        "perf": "Very light",
        "impact": "Subtle (sharpening only)",
        "best_for": "Adding crispness without upscaling; content already near display resolution.",
        "pros": [
            "Almost free.",
            "Contrast-adaptive — avoids over-sharpening flat areas.",
            "Pairs well with other scalers.",
        ],
        "cons": [
            "Does NOT upscale — sharpening only.",
            "Won't rescue heavily blurred or low-res sources.",
        ],
        "note": ("Use when the picture is soft but the resolution is already fine. "
                 "Despite the AMD name the shader runs on any GPU."),
        "profiles": ["AMD FidelityFX Contrast Adaptive Sharpening"],
    },
    {
        "name": "FSRCNNX (neural)",
        "perf": "Moderate → Heavy",
        "impact": "High (detail reconstruction)",
        "best_for": "Higher-quality upscaling of live-action/general content on a capable GPU.",
        "pros": [
            "Neural upscaler — recovers real detail.",
            "Noticeably sharper and cleaner than spatial scalers.",
            "'x16' variant pushes detail further.",
        ],
        "cons": [
            "Heavier — wants a decent GPU (x16 much more so).",
            "Overkill if content is already at display resolution.",
        ],
        "note": "'FSRCNNX x16' (generic-high) spends a lot more GPU for a modest detail gain.",
        "profiles": ["generic", "generic-high"],
    },
    {
        "name": "NNEDI3 (neural)",
        "perf": "Heavy → Very heavy",
        "impact": "High (very clean edges)",
        "best_for": "Top-tier edge-directed upscaling when you have GPU headroom.",
        "pros": [
            "Excellent edge quality with minimal ringing.",
            "Great for clean upscales of good sources.",
        ],
        "cons": [
            "GPU-hungry, especially the 128-neuron variant.",
            "Little texture reconstruction — can look soft.",
            "May not keep up on weaker GPUs or at high refresh rates.",
        ],
        "note": "The 128-neuron profile is demanding — confirm your GPU keeps up.",
        "profiles": ["nnedi-high", "nnedi-very-high"],
    },
    {
        "name": "Anime4K",
        "perf": "Fast tier: Moderate · HQ tier: Heavy",
        "impact": "High (for animation)",
        "best_for": "Animation — restores and sharpens line art, de-blurs and de-rings.",
        "pros": [
            "Purpose-built for anime/cartoons.",
            "Modes A/B/C target different source quality.",
            "Fast tier for weaker GPUs, HQ tier for quality.",
        ],
        "cons": [
            "Designed for animation — not ideal for live-action.",
            "Many variants; needs matching to the source.",
            "HQ modes are GPU-heavy.",
        ],
        "note": ("Pick by source: A = very blurry/compressed, B = blurry/ringing, "
                 "C = already crisp. Doubled names (AA/BB/CA) chain two passes for a "
                 "stronger effect. Fast vs HQ is the performance/quality trade-off "
                 "(this is also the LQ/HQ subtype)."),
        "profiles": ["anime4k-high-a", "anime4k-high-b", "anime4k-high-c",
                     "anime4k-high-aa", "anime4k-high-bb", "anime4k-high-ca",
                     "anime4k-fast-a", "anime4k-fast-b", "anime4k-fast-c",
                     "anime4k-fast-aa", "anime4k-fast-bb", "anime4k-fast-ca"],
    },
    {
        "name": "ArtCNN (neural, downloadable)",
        "perf": "C4F16: Moderate · C4F32: Heavy",
        "impact": "High (modern detail reconstruction)",
        "best_for": "High-quality upscaling of anime and general content; a "
                    "current state-of-the-art shader upscaler.",
        "pros": [
            "Excellent detail — often beats the bundled upscalers.",
            "Tiers: C4F16 (fast) vs C4F32 (quality).",
            "'DS' variants also denoise and sharpen.",
        ],
        "cons": [
            "Not bundled — downloaded on first use (MIT, from GitHub).",
            "Requires mpv's gpu-next video output.",
            "C4F32 is GPU-heavy.",
        ],
        "note": ("Downloaded on demand and checksum-verified into your config "
                 "folder. Needs gpu-next: the built-in player has a "
                 "'Use gpu-next video output' option; external mpv needs "
                 "'vo=gpu-next' in your own mpv.conf."),
        "profiles": ["artcnn-c4f16", "artcnn-c4f16-ds",
                     "artcnn-c4f32", "artcnn-c4f32-ds"],
    },
]

# One-liners shown inline under the dropdown for the selected profile.
_SUMMARIES = {
    "nvscaler": "Nvidia Image Scaler — light, general upscaling + sharpening. Safe default.",
    "AMD FidelityFX Super Resolution":
        "AMD FSR — light, edge-aware spatial upscaling for low-res sources.",
    "AMD FidelityFX Contrast Adaptive Sharpening":
        "AMD CAS — very light adaptive sharpening; does not upscale.",
    "generic": "FSRCNNX — neural upscaler that recovers detail; moderate GPU cost.",
    "generic-high": "FSRCNNX x16 — more detail, noticeably heavier on the GPU.",
    "nnedi-high": "NNEDI3 (64 neurons) — clean edge-directed upscaling; GPU-heavy.",
    "nnedi-very-high": "NNEDI3 (128 neurons) — top edge quality; very GPU-heavy.",
}

_ANIME4K_HQ = ("Anime4K (HQ) — quality anime line-art restoration; GPU-heavy. "
               "Suffix picks source type (A/B/C).")
_ANIME4K_FAST = ("Anime4K (Fast) — lighter anime restoration for weaker GPUs. "
                 "Suffix picks source type (A/B/C).")


def summary_for(profile):
    """One-line summary for a profile key, or None if we don't have one."""
    if not profile:
        return None
    if profile in _SUMMARIES:
        return _SUMMARIES[profile]
    if profile.startswith("anime4k-high"):
        return _ANIME4K_HQ
    if profile.startswith("anime4k-fast"):
        return _ANIME4K_FAST
    return None
