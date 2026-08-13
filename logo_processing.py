"""
Automatic logo cleanup, run once when a company uploads their logo image.

What it does:
  1. Makes plain white/near-white backgrounds transparent (a simple color-key,
     not full ML background removal - see limitation note below).
  2. Trims away empty transparent margin around the actual artwork.
  3. Centers the result on a padded square transparent canvas, so every
     company's logo behaves consistently at render time regardless of the
     original image's shape or size.
  4. Caps the resolution so file size stays reasonable.

Known limitation: only plain white/near-white backgrounds are removed
reliably. A logo photographed against a patterned or colored background, or
one that's already got a busy background baked in, won't be cleanly
separated - true background removal for arbitrary backgrounds needs an ML
segmentation model, which isn't part of this pipeline. For those cases the
logo will keep its original background.
"""
from PIL import Image
import numpy as np

CANVAS_SIZE = 500          # final square canvas, in pixels
PADDING_RATIO = 0.12       # fraction of canvas reserved as empty margin
WHITE_THRESHOLD = 235      # RGB values above this become transparent
MAX_INPUT_DIMENSION = 2000 # downscale huge uploads before processing, for speed


def _make_light_background_transparent(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    arr = np.array(img)  # shape (h, w, 4)

    rgb = arr[:, :, :3]
    is_light = np.all(rgb >= WHITE_THRESHOLD, axis=2)
    arr[is_light, 3] = 0  # set alpha to 0 wherever the pixel is near-white

    return Image.fromarray(arr, mode="RGBA")


def _trim_to_content(img: Image.Image) -> Image.Image:
    bbox = img.getbbox()  # bounding box of all non-fully-transparent pixels
    if bbox:
        return img.crop(bbox)
    return img


def process_logo(input_path: str, output_path: str):
    """Reads the raw uploaded image at input_path, cleans it up, and writes
    a PNG (with transparency) to output_path."""
    img = Image.open(input_path)

    # Downscale very large photos before pixel processing, purely for speed -
    # doesn't affect final output quality since we resize to CANVAS_SIZE anyway.
    if max(img.size) > MAX_INPUT_DIMENSION:
        ratio = MAX_INPUT_DIMENSION / max(img.size)
        img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)

    img = _make_light_background_transparent(img)
    img = _trim_to_content(img)

    if img.width == 0 or img.height == 0:
        # Degenerate case (e.g. a fully-white image) - fall back to the
        # untouched original rather than producing an empty file.
        img = Image.open(input_path).convert("RGBA")

    # Scale to fit within the padded content area, preserving aspect ratio
    content_area = int(CANVAS_SIZE * (1 - PADDING_RATIO * 2))
    scale = min(content_area / img.width, content_area / img.height)
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (255, 255, 255, 0))
    offset = ((CANVAS_SIZE - new_w) // 2, (CANVAS_SIZE - new_h) // 2)
    canvas.paste(img, offset, img)

    canvas.save(output_path, format="PNG")
