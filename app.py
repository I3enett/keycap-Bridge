"""
Keycap Color-by-Number bridge (v5 -- AI Horde plus photo library previews).

The original AI generation endpoints remain unchanged. The photo-library endpoints
use Wikimedia Commons, so the Roblox game can search, preview, and convert public
images without exposing an API key.
"""

import base64
import io
import os
from urllib.parse import quote, urlparse

import requests
from flask import Flask, jsonify, request
from PIL import Image

app = Flask(__name__)

BLOCKED_SUBSTRINGS = ["nude", "naked", "nsfw", "porn", "sex", "explicit", "hentai", "erotic"]

MAX_PROMPT_LEN = 100
DEFAULT_MAX_COLORS = 40
MAX_OUTPUT_SIZE = 313
SOURCE_IMAGE_SIZE = 512
REQUEST_TIMEOUT_SECONDS = 30
MAX_LIBRARY_DOWNLOAD_BYTES = 12 * 1024 * 1024
COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
COMMONS_USER_AGENT = "KeycapColorByNumber/1.0 (Roblox photo library; I3enett)"

# Anonymous usage is officially supported with ten zeroes and has no image charge.
# A free registered Horde key can optionally be placed in Render for better queue priority.
AI_HORDE_API_KEY = os.environ.get("AI_HORDE_API_KEY", "0000000000").strip()
AI_HORDE_CLIENT_AGENT = "keycap-bridge:2.0:I3enett"
HORDE_BASE_URL = "https://aihorde.net/api/v2/generate"


def is_blocked(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(word in lowered for word in BLOCKED_SUBSTRINGS)


def bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def valid_job_id(job_id: str) -> bool:
    return (
        1 <= len(job_id) <= 64
        and all(character.isalnum() or character == "-" for character in job_id)
    )


def horde_headers(include_json=False):
    headers = {
        "apikey": AI_HORDE_API_KEY or "0000000000",
        "Client-Agent": AI_HORDE_CLIENT_AGENT,
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def decode_image_value(value: str) -> Image.Image:
    if value.startswith("http://") or value.startswith("https://"):
        response = requests.get(value, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    if "," in value and value.lower().startswith("data:"):
        value = value.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(value))).convert("RGB")


def square_crop(img: Image.Image) -> Image.Image:
    width, height = img.size
    edge = min(width, height)
    left = (width - edge) // 2
    top = (height - edge) // 2
    return img.crop((left, top, left + edge, top + edge))


def quantized_grid(img: Image.Image, size: int, max_colors: int) -> dict:
    resampling = getattr(Image, "Resampling", Image)
    img = square_crop(img.convert("RGB")).resize((size, size), resampling.LANCZOS)
    quantized = img.quantize(colors=max_colors, method=Image.Quantize.MAXCOVERAGE)
    used = quantized.getcolors() or []
    used_indices = sorted(index for _, index in used)
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
    return {
        "grid": grid,
        "palette": palette_out,
        "width": size,
        "height": size,
    }


def grid_response(img: Image.Image, size: int, max_colors: int, generation: dict):
    result = quantized_grid(img, size, max_colors)
    result.update({
        "status": "complete",
        "provider": "ai-horde",
        "model": generation.get("model"),
    })
    return jsonify(result)


def is_allowed_commons_image_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "upload.wikimedia.org"
            and not parsed.username
            and not parsed.password
            and parsed.port is None
            and bool(parsed.path)
        )
    except (TypeError, ValueError):
        return False


def download_commons_image(url: str) -> Image.Image:
    if not is_allowed_commons_image_url(url):
        raise ValueError("unsupported photo source")
    response = requests.get(
        url,
        headers={"User-Agent": COMMONS_USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_LIBRARY_DOWNLOAD_BYTES:
        raise ValueError("photo is too large")
    if len(response.content) > MAX_LIBRARY_DOWNLOAD_BYTES:
        raise ValueError("photo is too large")
    return Image.open(io.BytesIO(response.content)).convert("RGB")


@app.route("/library/search", methods=["POST"])
def library_search():
    data = request.get_json(force=True, silent=True) or {}
    query = str(data.get("query") or "").strip()[:MAX_PROMPT_LEN]
    limit = bounded_int(data.get("limit"), 5, 1, 5)
    # Photo-library cards are displayed through Roblox EditableImages. Keep enough
    # detail here for them to look like photos; board conversion still happens only
    # after a player selects a result.
    preview_size = bounded_int(data.get("preview_size"), 96, 32, 96)
    preview_colors = bounded_int(data.get("colors"), 128, 16, 128)
    if len(query) < 2:
        return jsonify({"error": "search must be at least 2 characters"}), 400
    if is_blocked(query):
        return jsonify({"error": "blocked search"}), 400

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query + " filetype:bitmap",
        "gsrnamespace": 6,
        # Commons search results often include GIF/TIFF/DjVu files that this game
        # intentionally skips. Pull a wider candidate pool so five usable JPEG/PNG/WebP
        # previews are still returned for ordinary searches.
        "gsrlimit": 50,
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": 640,
        "iiurlheight": 640,
        "format": "json",
        "formatversion": 2,
    }

    try:
        response = requests.get(
            COMMONS_API_URL,
            params=params,
            headers={"User-Agent": COMMONS_USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        pages = (response.json().get("query") or {}).get("pages") or []
        results = []
        for page in pages:
            image_info = (page.get("imageinfo") or [None])[0]
            if not image_info:
                continue
            mime = str(image_info.get("mime") or "").lower()
            if mime not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            source_url = image_info.get("thumburl") or image_info.get("url")
            if not source_url or not is_allowed_commons_image_url(source_url):
                continue
            try:
                preview = quantized_grid(
                    download_commons_image(source_url),
                    preview_size,
                    preview_colors,
                )
            except Exception:
                app.logger.warning("Skipping unusable Commons result", exc_info=True)
                continue
            title = str(page.get("title") or "Photo")
            clean_title = title[5:] if title.lower().startswith("file:") else title
            results.append({
                "id": str(page.get("pageid") or len(results) + 1),
                "title": clean_title[:80],
                "source_url": source_url,
                "source_page": "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_")),
                **preview,
            })
            if len(results) >= limit:
                break

        if not results:
            return jsonify({
                "status": "empty",
                "error": "No usable photos found. Try a different search",
                "results": [],
            }), 404
        return jsonify({"status": "complete", "query": query, "results": results})
    except Exception as exc:
        app.logger.exception("Could not search Wikimedia Commons")
        return jsonify({"error": "photo search failed", "detail": str(exc)}), 503


@app.route("/library/convert", methods=["POST"])
def library_convert():
    data = request.get_json(force=True, silent=True) or {}
    source_url = str(data.get("source_url") or "").strip()
    size = bounded_int(data.get("size"), 125, 4, MAX_OUTPUT_SIZE)
    max_colors = bounded_int(data.get("colors"), DEFAULT_MAX_COLORS, 2, 64)
    try:
        result = quantized_grid(download_commons_image(source_url), size, max_colors)
        result.update({"status": "complete", "provider": "wikimedia-commons"})
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Could not convert Wikimedia Commons image")
        return jsonify({"error": "photo conversion failed", "detail": str(exc)}), 503


@app.route("/generate/start", methods=["POST"])
def start_generation():
    data = request.get_json(force=True, silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()[:MAX_PROMPT_LEN]
    if not prompt:
        return jsonify({"error": "empty prompt"}), 400
    if is_blocked(prompt):
        return jsonify({"error": "blocked prompt"}), 400

    payload = {
        "prompt": (
            prompt
            + " ### text, words, watermark, signature, blurry, distorted, low quality"
        ),
        "params": {
            "cfg_scale": 5,
            "sampler_name": "k_euler_a",
            "height": SOURCE_IMAGE_SIZE,
            "width": SOURCE_IMAGE_SIZE,
            "steps": 12,
            "n": 1,
            "karras": True,
        },
        "allow_downgrade": True,
        "nsfw": False,
        "censor_nsfw": True,
        "r2": True,
        "shared": False,
        "slow_workers": True,
    }

    try:
        response = requests.post(
            HORDE_BASE_URL + "/async",
            headers=horde_headers(include_json=True),
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
        job_id = result.get("id")
        if not job_id:
            raise RuntimeError("AI Horde returned no job id")
        return jsonify({
            "status": "queued",
            "job_id": job_id,
            "kudos": result.get("kudos"),
        }), 202
    except Exception as exc:
        app.logger.exception("Could not submit AI Horde generation")
        return jsonify({
            "error": "could not queue image",
            "detail": str(exc),
        }), 503


@app.route("/generate/status/<job_id>", methods=["GET"])
def generation_status(job_id):
    if not valid_job_id(job_id):
        return jsonify({"error": "invalid job id"}), 400

    size = bounded_int(request.args.get("size"), 125, 4, MAX_OUTPUT_SIZE)
    max_colors = bounded_int(request.args.get("colors"), DEFAULT_MAX_COLORS, 2, 64)

    try:
        check_response = requests.get(
            HORDE_BASE_URL + "/check/" + job_id,
            headers=horde_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        check_response.raise_for_status()
        state = check_response.json()

        if state.get("faulted"):
            return jsonify({
                "status": "faulted",
                "error": "AI Horde generation faulted",
            }), 502

        if not state.get("done"):
            return jsonify({
                "status": "waiting",
                "queue_position": state.get("queue_position"),
                "wait_time": state.get("wait_time"),
                "processing": state.get("processing"),
            })

        result_response = requests.get(
            HORDE_BASE_URL + "/status/" + job_id,
            headers=horde_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        result_response.raise_for_status()
        generations = result_response.json().get("generations") or []
        if not generations:
            raise RuntimeError("AI Horde returned no completed image")

        generation = generations[0]
        if generation.get("censored"):
            return jsonify({
                "status": "failed",
                "error": "Prompt failed safety check. Try again",
            }), 422

        image_value = generation.get("img") or ""
        if not image_value:
            raise RuntimeError("AI Horde returned an empty image")

        return grid_response(
            decode_image_value(image_value),
            size,
            max_colors,
            generation,
        )
    except Exception as exc:
        app.logger.exception("Could not retrieve AI Horde generation")
        return jsonify({
            "status": "failed",
            "error": "image retrieval failed",
            "detail": str(exc),
        }), 503


@app.route("/generate/cancel/<job_id>", methods=["DELETE"])
def cancel_generation(job_id):
    if not valid_job_id(job_id):
        return jsonify({"error": "invalid job id"}), 400
    try:
        response = requests.delete(
            HORDE_BASE_URL + "/status/" + job_id,
            headers=horde_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return jsonify({"status": "canceled"})
    except Exception as exc:
        return jsonify({"error": "cancel failed", "detail": str(exc)}), 503


@app.route("/generate", methods=["POST"])
def retired_synchronous_generation():
    return jsonify({
        "error": "client update required",
        "detail": "Use /generate/start and /generate/status/<job_id>",
    }), 409


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "provider": "ai-horde",
        "queue_mode": True,
        "photo_library": "wikimedia-commons",
        "anonymous": AI_HORDE_API_KEY == "0000000000",
        "max_output_size": MAX_OUTPUT_SIZE,
        "source_image_size": SOURCE_IMAGE_SIZE,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
