"""
Keycap Color-by-Number: Pollinations image-generation bridge (v3 -- high-resolution grids).
Fetches a consistently sized source image from Pollinations, resamples it to the requested
board resolution, and extracts an adaptive palette chosen from that image. Returns:
  { "grid": [[1..N, ...], ...], "palette": [[r,g,b], ...], "width": W, "height": H }
"""

import io
import urllib.parse

import requests
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

BLOCKED_SUBSTRINGS = ["nude", "naked", "nsfw", "porn", "sex", "explicit", "hentai", "erotic"]

MAX_PROMPT_LEN = 100
POLLINATIONS_TIMEOUT_SECONDS = 90
DEFAULT_MAX_COLORS = 40
MAX_OUTPUT_SIZE = 313
SOURCE_IMAGE_SIZE = 1024


def is_blocked(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(word in lowered for word in BLOCKED_SUBSTRINGS)


def bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()[:MAX_PROMPT_LEN]
    size = bounded_int(data.get("size"), 125, 4, MAX_OUTPUT_SIZE)
    max_colors = bounded_int(data.get("colors"), DEFAULT_MAX_COLORS, 2, 64)

    if not prompt:
        return jsonify({"error": "empty prompt"}), 400
    if is_blocked(prompt):
        return jsonify({"error": "blocked prompt"}), 400

    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
    params = {
        "width": SOURCE_IMAGE_SIZE,
        "height": SOURCE_IMAGE_SIZE,
        "nologo": "true",
        "safe": "true",
        "model": "flux",
    }

    try:
        resp = requests.get(url, params=params, timeout=POLLINATIONS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as exc:
        return jsonify({"error": f"image generation failed: {exc}"}), 502

    resampling = getattr(Image, "Resampling", Image)
    img = img.resize((size, size), resampling.LANCZOS)

    # Pick the best colors for this specific image instead of using a fixed palette.
    quantized = img.quantize(colors=max_colors, method=Image.Quantize.MAXCOVERAGE)
    used = quantized.getcolors() or []
    used_indices = sorted(idx for _, idx in used)
    index_map = {old: new + 1 for new, old in enumerate(used_indices)}

    flat_palette = quantized.getpalette()
    palette_out = [
        [flat_palette[i * 3], flat_palette[i * 3 + 1], flat_palette[i * 3 + 2]]
        for i in used_indices
    ]

    pixels = quantized.load()
    grid = [
        [index_map[pixels[x, y]] for x in range(size)]
        for y in range(size)
    ]

    return jsonify({
        "grid": grid,
        "palette": palette_out,
        "width": size,
        "height": size,
    })


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "max_output_size": MAX_OUTPUT_SIZE,
        "source_image_size": SOURCE_IMAGE_SIZE,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
