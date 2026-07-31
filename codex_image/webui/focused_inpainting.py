from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from codex_image.client_types import ImageResult


class FocusedInpaintingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class FocusedInpaintingContext:
    original: Image.Image
    edit_mask: Image.Image
    crop_box: tuple[int, int, int, int]
    feather: float

    @property
    def original_size(self) -> tuple[int, int]:
        return self.original.size


@dataclass(frozen=True)
class PreparedFocusedInputs:
    image_data_url: str
    mask_data_url: str
    context: FocusedInpaintingContext
    target_size: int


_ALLOWED_TARGET_SIZES = frozenset({1024})


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise FocusedInpaintingError("focused_inpainting_invalid", f"{field} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FocusedInpaintingError("focused_inpainting_invalid", f"{field} must be a finite number.") from exc
    if not math.isfinite(number):
        raise FocusedInpaintingError("focused_inpainting_invalid", f"{field} must be a finite number.")
    return number


def parse_focused_inpainting(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FocusedInpaintingError(
            "focused_inpainting_invalid", "Focused inpainting settings must be valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise FocusedInpaintingError(
            "focused_inpainting_invalid", "Focused inpainting settings must be a JSON object."
        )
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise FocusedInpaintingError("focused_inpainting_invalid", "enabled must be a boolean.")
    context = _finite_number(payload.get("context", 0.35), field="context")
    feather = _finite_number(payload.get("feather", 12), field="feather")
    if not 0 <= context <= 1:
        raise FocusedInpaintingError("focused_inpainting_invalid", "context must be between 0 and 1.")
    if not 0 <= feather <= 128:
        raise FocusedInpaintingError("focused_inpainting_invalid", "feather must be between 0 and 128 pixels.")
    try:
        target_size = int(payload.get("target_size", 1024))
    except (TypeError, ValueError) as exc:
        raise FocusedInpaintingError("focused_inpainting_invalid", "target_size is not supported.") from exc
    if target_size not in _ALLOWED_TARGET_SIZES:
        raise FocusedInpaintingError("focused_inpainting_invalid", "target_size is not supported.")

    rect_payload = payload.get("rect")
    rect: dict[str, float] | None = None
    if rect_payload is not None:
        if not isinstance(rect_payload, dict):
            raise FocusedInpaintingError("focused_inpainting_invalid", "rect must be an object.")
        rect = {
            key: _finite_number(rect_payload.get(key), field=f"rect.{key}")
            for key in ("x", "y", "width", "height")
        }
        if (
            rect["x"] < 0
            or rect["y"] < 0
            or rect["width"] <= 0
            or rect["height"] <= 0
            or rect["x"] + rect["width"] > 1
            or rect["y"] + rect["height"] > 1
        ):
            raise FocusedInpaintingError(
                "focused_inpainting_invalid", "rect must stay inside normalized image bounds."
            )
    return {
        "enabled": enabled,
        "rect": rect,
        "context": context,
        "feather": feather,
        "target_size": target_size,
    }


def _decode_data_url(data_url: str) -> bytes:
    if not isinstance(data_url, str) or not data_url.startswith("data:") or "," not in data_url:
        raise FocusedInpaintingError("focused_inpainting_input_invalid", "Focused input image is invalid.")
    header, payload = data_url.split(",", 1)
    if ";base64" not in header:
        raise FocusedInpaintingError("focused_inpainting_input_invalid", "Focused input image is invalid.")
    try:
        return base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FocusedInpaintingError("focused_inpainting_input_invalid", "Focused input image is invalid.") from exc


def _png_data_url(image: Image.Image) -> str:
    stream = BytesIO()
    image.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def _open_image(data: bytes, *, mask: bool = False) -> Image.Image:
    try:
        with Image.open(BytesIO(data)) as image:
            if mask and str(image.format or "").upper() != "PNG":
                raise FocusedInpaintingError("mask_format_invalid", "Mask must be a valid PNG image.")
            image.load()
            return image.copy()
    except FocusedInpaintingError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        label = "Mask must be a valid PNG image." if mask else "Input image could not be decoded."
        code = "mask_format_invalid" if mask else "focused_inpainting_input_invalid"
        raise FocusedInpaintingError(code, label) from exc


def validate_edit_mask(base_image_bytes: bytes, mask_bytes: bytes) -> dict[str, int]:
    base = _open_image(base_image_bytes)
    mask = _open_image(mask_bytes, mask=True)
    if mask.mode not in {"RGBA", "LA"} and "transparency" not in mask.info:
        raise FocusedInpaintingError("mask_alpha_required", "Mask PNG must contain an alpha channel.")
    mask = mask.convert("RGBA")
    if mask.size != base.size:
        raise FocusedInpaintingError(
            "mask_dimensions_mismatch", "Mask dimensions must match the first edit image."
        )
    alpha = mask.getchannel("A")
    histogram = alpha.histogram()
    editable_pixels = sum(histogram[:255])
    if editable_pixels <= 0:
        raise FocusedInpaintingError(
            "mask_transparency_required", "Mask must contain transparent pixels to edit."
        )
    return {"width": base.width, "height": base.height, "editable_pixels": editable_pixels}


def validate_edit_mask_data_urls(base_image_data_url: str, mask_data_url: str) -> dict[str, int]:
    return validate_edit_mask(_decode_data_url(base_image_data_url), _decode_data_url(mask_data_url))


def _normalized_rect_box(rect: Mapping[str, float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    left = max(0, min(width - 1, math.floor(rect["x"] * width)))
    top = max(0, min(height - 1, math.floor(rect["y"] * height)))
    right = max(left + 1, min(width, math.ceil((rect["x"] + rect["width"]) * width)))
    bottom = max(top + 1, min(height, math.ceil((rect["y"] + rect["height"]) * height)))
    return left, top, right, bottom


def _square_crop_box(
    focus_box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    context: float,
) -> tuple[int, int, int, int]:
    image_width, image_height = image_size
    left, top, right, bottom = focus_box
    focus_width = max(1, right - left)
    focus_height = max(1, bottom - top)
    side = max(focus_width, focus_height)
    side = max(1, math.ceil(side * (1 + 2 * context)))
    max_side = min(image_width, image_height)
    if max(focus_width, focus_height) > max_side:
        raise FocusedInpaintingError(
            "focused_inpainting_region_too_large",
            "Focused region is too large for a square crop; use normal inpainting instead.",
        )
    side = min(side, max_side)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_left = round(center_x - side / 2)
    crop_top = round(center_y - side / 2)
    crop_left = max(0, min(image_width - side, crop_left))
    crop_top = max(0, min(image_height - side, crop_top))
    return crop_left, crop_top, crop_left + side, crop_top + side


def prepare_focused_inputs(
    base_image_data_url: str,
    mask_data_url: str,
    config: Mapping[str, Any],
) -> PreparedFocusedInputs:
    base_bytes = _decode_data_url(base_image_data_url)
    mask_bytes = _decode_data_url(mask_data_url)
    validate_edit_mask(base_bytes, mask_bytes)
    original = _open_image(base_bytes).convert("RGBA")
    mask = _open_image(mask_bytes, mask=True).convert("RGBA")
    alpha = mask.getchannel("A")
    edit_mask = ImageOps.invert(alpha)
    bbox = edit_mask.getbbox()
    if bbox is None:
        raise FocusedInpaintingError(
            "mask_transparency_required", "Mask must contain transparent pixels to edit."
        )
    rect = config.get("rect")
    focus_box = _normalized_rect_box(rect, original.size) if isinstance(rect, Mapping) else bbox
    # A manually selected focus rectangle must include all painted edit pixels.
    if isinstance(rect, Mapping):
        union = (
            min(focus_box[0], bbox[0]),
            min(focus_box[1], bbox[1]),
            max(focus_box[2], bbox[2]),
            max(focus_box[3], bbox[3]),
        )
        focus_box = union
    crop_box = _square_crop_box(focus_box, original.size, float(config.get("context", 0.35)))
    target_size = int(config.get("target_size", 1024))
    focused_image = original.crop(crop_box).resize((target_size, target_size), Image.Resampling.LANCZOS)
    focused_alpha = alpha.crop(crop_box).resize((target_size, target_size), Image.Resampling.LANCZOS)
    focused_mask = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 255))
    focused_mask.putalpha(focused_alpha)
    context = FocusedInpaintingContext(
        original=original,
        edit_mask=edit_mask,
        crop_box=crop_box,
        feather=float(config.get("feather", 12)),
    )
    return PreparedFocusedInputs(
        image_data_url=_png_data_url(focused_image),
        mask_data_url=_png_data_url(focused_mask),
        context=context,
        target_size=target_size,
    )


def composite_focused_result(
    result_bytes: bytes,
    context: FocusedInpaintingContext,
    *,
    output_format: str,
) -> bytes:
    patch = _open_image(result_bytes).convert("RGBA")
    left, top, right, bottom = context.crop_box
    crop_size = (right - left, bottom - top)
    patch = patch.resize(crop_size, Image.Resampling.LANCZOS)
    blend_mask = context.edit_mask.crop(context.crop_box)
    if context.feather > 0:
        blend_mask = blend_mask.filter(ImageFilter.GaussianBlur(radius=context.feather))
    original = context.original.copy()
    original_crop = original.crop(context.crop_box)
    composed_crop = Image.composite(patch, original_crop, blend_mask)
    original.paste(composed_crop, (left, top))

    normalized_format = str(output_format or "png").strip().lower()
    if normalized_format == "jpg":
        normalized_format = "jpeg"
    stream = BytesIO()
    if normalized_format == "jpeg":
        flattened = Image.new("RGB", original.size, (255, 255, 255))
        flattened.paste(original.convert("RGB"))
        flattened.save(stream, format="JPEG", quality=95)
    elif normalized_format == "webp":
        original.save(stream, format="WEBP", quality=95)
    else:
        original.save(stream, format="PNG")
    return stream.getvalue()


class FocusedInpaintingImageClient:
    """Compatibility client that performs focused crop before edit and full-image composition after it."""

    def __init__(self, client: Any, config: Mapping[str, Any]) -> None:
        self._client = client
        self._config = dict(config)
        self.direct_images_concurrent = bool(getattr(client, "direct_images_concurrent", False))

    def edit_image(self, **kwargs: Any) -> ImageResult:
        images = list(kwargs.get("images") or [])
        mask_image = kwargs.get("mask_image")
        if not images or not mask_image:
            raise FocusedInpaintingError(
                "focused_inpainting_requires_inputs",
                "Focused inpainting requires a base image and a valid mask.",
            )
        prepared = prepare_focused_inputs(images[0], mask_image, self._config)
        focused_kwargs = dict(kwargs)
        focused_kwargs["images"] = [prepared.image_data_url, *images[1:]]
        focused_kwargs["mask_image"] = prepared.mask_data_url
        focused_kwargs["size"] = f"{prepared.target_size}x{prepared.target_size}"
        result = self._client.edit_image(**focused_kwargs)
        output_format = result.output_format or str(kwargs.get("output_format") or "png")
        full_bytes = composite_focused_result(
            result.image_bytes,
            prepared.context,
            output_format=output_format,
        )
        width, height = prepared.context.original_size
        tool_usage = dict(result.tool_usage or {})
        tool_usage["focused_inpainting"] = {
            "crop_box": list(prepared.context.crop_box),
            "target_size": prepared.target_size,
            "feather": prepared.context.feather,
        }
        return ImageResult(
            full_bytes,
            result.revised_prompt,
            output_format,
            f"{width}x{height}",
            result.background,
            result.quality,
            dict(result.usage or {}),
            tool_usage,
        )

    def generate_image(self, **kwargs: Any) -> ImageResult:
        return self._client.generate_image(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


__all__ = (
    "FocusedInpaintingContext",
    "FocusedInpaintingError",
    "FocusedInpaintingImageClient",
    "PreparedFocusedInputs",
    "composite_focused_result",
    "parse_focused_inpainting",
    "prepare_focused_inputs",
    "validate_edit_mask",
    "validate_edit_mask_data_urls",
)
