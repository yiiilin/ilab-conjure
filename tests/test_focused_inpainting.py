from __future__ import annotations

import base64
import json
import math
import unittest
from io import BytesIO

from PIL import Image, ImageDraw

from codex_image.client_types import ImageResult
from codex_image.webui.focused_inpainting import (
    FocusedInpaintingError,
    FocusedInpaintingImageClient,
    composite_focused_result,
    parse_focused_inpainting,
    prepare_focused_inputs,
    validate_edit_mask,
)


def _data_url(image: Image.Image, fmt: str = "PNG") -> str:
    stream = BytesIO()
    image.save(stream, format=fmt)
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def _image_bytes(image: Image.Image, fmt: str = "PNG") -> bytes:
    stream = BytesIO()
    image.save(stream, format=fmt)
    return stream.getvalue()


class FakeFocusedClient:
    direct_images_concurrent = True

    def __init__(self) -> None:
        self.edit_calls: list[dict] = []

    def edit_image(self, **kwargs):
        self.edit_calls.append(kwargs)
        patch = Image.new("RGB", (1024, 1024), (255, 255, 255))
        return ImageResult(
            _image_bytes(patch), "", "png", "1024x1024", "auto", "low", {}, {}
        )

    def generate_image(self, **kwargs):
        raise AssertionError("focused wrapper must not generate")


class FocusedInpaintingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Image.new("RGB", (100, 80), (12, 34, 56))
        draw = ImageDraw.Draw(self.base)
        draw.rectangle((30, 20, 49, 39), fill=(200, 30, 30))
        self.mask = Image.new("RGBA", self.base.size, (0, 0, 0, 255))
        ImageDraw.Draw(self.mask).rectangle((32, 22, 47, 37), fill=(0, 0, 0, 0))

    def test_parse_focused_inpainting_normalizes_and_rejects_invalid_values(self) -> None:
        parsed = parse_focused_inpainting(json.dumps({
            "enabled": True,
            "rect": {"x": 0.2, "y": 0.1, "width": 0.4, "height": 0.5},
            "context": 0.35,
            "feather": 12,
            "target_size": 1024,
        }))
        self.assertTrue(parsed["enabled"])
        self.assertEqual(parsed["target_size"], 1024)
        self.assertEqual(parsed["rect"]["width"], 0.4)

        bad_payloads = [
            "not-json",
            json.dumps([]),
            json.dumps({"enabled": True, "context": math.inf}),
            json.dumps({"enabled": True, "feather": -1}),
            json.dumps({"enabled": True, "target_size": 777}),
            json.dumps({"enabled": True, "rect": {"x": 0.9, "y": 0.1, "width": 0.2, "height": 0.2}}),
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(FocusedInpaintingError):
                parse_focused_inpainting(payload)

    def test_validate_edit_mask_requires_png_matching_size_and_transparency(self) -> None:
        info = validate_edit_mask(_image_bytes(self.base), _image_bytes(self.mask))
        self.assertEqual(info["width"], 100)
        self.assertEqual(info["height"], 80)
        self.assertGreater(info["editable_pixels"], 0)

        with self.assertRaisesRegex(FocusedInpaintingError, "dimensions"):
            validate_edit_mask(
                _image_bytes(self.base),
                _image_bytes(Image.new("RGBA", (50, 40), (0, 0, 0, 0))),
            )
        with self.assertRaisesRegex(FocusedInpaintingError, "transparent"):
            validate_edit_mask(
                _image_bytes(self.base),
                _image_bytes(Image.new("RGBA", self.base.size, (0, 0, 0, 255))),
            )
        with self.assertRaisesRegex(FocusedInpaintingError, "PNG"):
            validate_edit_mask(_image_bytes(self.base), b"not-an-image")

    def test_prepare_uses_square_clamped_crop_and_openai_alpha_semantics(self) -> None:
        prepared = prepare_focused_inputs(
            _data_url(self.base),
            _data_url(self.mask),
            {"enabled": True, "rect": None, "context": 0.5, "feather": 8, "target_size": 1024},
        )
        self.assertEqual(prepared.context.original_size, (100, 80))
        left, top, right, bottom = prepared.context.crop_box
        self.assertEqual(right - left, bottom - top)
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(right, 100)
        self.assertLessEqual(bottom, 80)

        focused_image = Image.open(BytesIO(base64.b64decode(prepared.image_data_url.split(",", 1)[1])))
        focused_mask = Image.open(BytesIO(base64.b64decode(prepared.mask_data_url.split(",", 1)[1])))
        self.assertEqual(focused_image.size, (1024, 1024))
        self.assertEqual(focused_mask.size, (1024, 1024))
        self.assertEqual(focused_mask.mode, "RGBA")
        self.assertEqual(focused_mask.getextrema()[3][0], 0)
        self.assertEqual(focused_mask.getextrema()[3][1], 255)

    def test_composite_preserves_outside_crop_and_replaces_masked_center(self) -> None:
        prepared = prepare_focused_inputs(
            _data_url(self.base),
            _data_url(self.mask),
            {"enabled": True, "rect": None, "context": 0.25, "feather": 0, "target_size": 1024},
        )
        patch = _image_bytes(Image.new("RGB", (1024, 1024), (255, 255, 255)))
        result = composite_focused_result(patch, prepared.context, output_format="png")
        output = Image.open(BytesIO(result)).convert("RGB")
        self.assertEqual(output.size, self.base.size)
        self.assertEqual(output.getpixel((0, 0)), self.base.getpixel((0, 0)))
        self.assertEqual(output.getpixel((99, 79)), self.base.getpixel((99, 79)))
        self.assertEqual(output.getpixel((40, 30)), (255, 255, 255))
        self.assertEqual(output.getpixel((25, 15)), self.base.getpixel((25, 15)))

    def test_wrapper_transforms_request_and_returns_full_size_result(self) -> None:
        inner = FakeFocusedClient()
        client = FocusedInpaintingImageClient(
            inner,
            {"enabled": True, "rect": None, "context": 0.25, "feather": 4, "target_size": 1024},
        )
        result = client.edit_image(
            prompt="fix",
            images=[_data_url(self.base)],
            mask_image=_data_url(self.mask),
            size="1536x1024",
            output_format="png",
        )
        self.assertEqual(len(inner.edit_calls), 1)
        self.assertEqual(inner.edit_calls[0]["size"], "1024x1024")
        self.assertEqual(result.size, "100x80")
        self.assertEqual(Image.open(BytesIO(result.image_bytes)).size, (100, 80))
        self.assertTrue(client.direct_images_concurrent)


if __name__ == "__main__":
    unittest.main()
