"""
Keycap Color-by-Number: Pollinations image-generation bridge.
Fetches an image from Pollinations, shrinks it to the board's resolution, and maps
every pixel to the nearest of the game's 8 palette colors, returning the grid Roblox
expects: { "grid": [[1..8, ...], ...] }
"""

import io
import urllib.parse

import requests
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

# Must match ReplicatedStorage.KeycapRemakeConfig.Palette exactly (in order, 1-indexed).
PALETTE = [
    (36, 39, 50),      # 1 Background
    (247, 230, 187),   # 2 Cream
    (239, 86, 122),    # 3 Pink
    (255, 174, 69),    # 4 Orange
    (86, 212, 142),    # 5 Green
    (79, 166, 255),    # 6 Blue
    (147, 98, 255),    # 7 Purple
    (255, 244, 92),    # 8 Glow
]

# Blunt, extend-as-needed defense-in-depth on top of Pollinations' own safe=true filter.
BLOCKED_SUBSTRINGS = ["nude", "naked", "nsfw", "porn", "sex", "explicit", "hentai", "erotic"]

MAX_PROMPT_LEN = 100
POLLINATIONS_TIMEOUT_SECONDS = 25


def nearest_palette_index(rgb):
    best_index, best_dist = 0, None
    for i, p in enumerate(PALETTE):
        dist = sum((a - b) ** 2 for a, b in zip(rgb, p))
        if best_dist is None or dist < best_dist:
            best_dist, best_index = dist, i
    return best_index + 1  # Lua/Roblox side is 1-indexed


def is_blocked(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(word in lowered for word in BLOCKED_SUBSTRINGS)


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()[:MAX_PROMPT_LEN]
    size = int(data.get("size") or 25)
    size = max(4, min(size, 128))

    if not prompt:
        return jsonify({"error": "empty prompt"}), 400
    if is_blocked(prompt):
        return jsonify({"error": "blocked prompt"}), 400

    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}"
    params = {
        "width": size * 8,
        "height": size * 8,
        "nologo": "true",
        "safe": "true",
        "model": "flux",  # free, unlimited tier per Pollinations' docs
    }

    try:
        resp = requests.get(url, params=params, timeout=POLLINATIONS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as exc:
        return jsonify({"error": f"image generation failed: {exc}"}), 502

    img = img.resize((size, size), Image.LANCZOS)

    grid = []
    for y in range(size):
        row = [nearest_palette_index(img.getpixel((x, y))) for x in range(size)]
        grid.append(row)

    return jsonify({"grid": grid})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
