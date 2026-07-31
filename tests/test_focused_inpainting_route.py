from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from codex_image.client_types import ImageResult
from codex_image.webui.app import create_app


def _png(size=(96, 72), color=(40, 80, 120), *, mask=False, transparent=True) -> bytes:
    mode = "RGBA" if mask else "RGB"
    fill = (0, 0, 0, 255) if mask else color
    image = Image.new(mode, size, fill)
    if mask and transparent:
        ImageDraw.Draw(image).rectangle((30, 20, 50, 40), fill=(0, 0, 0, 0))
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class ValidImageClient:
    direct_images_concurrent = True

    def __init__(self) -> None:
        self.edit_calls: list[dict[str, Any]] = []

    def edit_image(self, **kwargs: Any) -> ImageResult:
        self.edit_calls.append(kwargs)
        output = Image.new("RGB", (1024, 1024), (250, 250, 250))
        stream = BytesIO()
        output.save(stream, format="PNG")
        return ImageResult(stream.getvalue(), "", "png", "1024x1024", "auto", "low", {}, {})

    def generate_image(self, **kwargs: Any) -> ImageResult:
        raise AssertionError("not used")


class FocusedInpaintingRouteTests(unittest.TestCase):
    def _app(self, root: Path, fake: ValidImageClient):
        return create_app(
            output_root=root,
            client_factory=lambda: fake,
            auth_checker=lambda: True,
            batch_delay_seconds=0,
            auto_start_queue=False,
        )

    def test_edit_rejects_invalid_and_mismatched_masks(self) -> None:
        fake = ValidImageClient()
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(self._app(Path(tmp), fake))
            invalid = client.post(
                "/api/edit",
                data={"prompt": "fix"},
                files={"images": ("base.png", _png(), "image/png"), "mask": ("mask.png", b"bad", "image/png")},
            )
            mismatch = client.post(
                "/api/edit",
                data={"prompt": "fix"},
                files={
                    "images": ("base.png", _png(), "image/png"),
                    "mask": ("mask.png", _png((48, 36), mask=True), "image/png"),
                },
            )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["detail"]["code"], "mask_format_invalid")
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(mismatch.json()["detail"]["code"], "mask_dimensions_mismatch")

    def test_focused_edit_requires_mask(self) -> None:
        fake = ValidImageClient()
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(self._app(Path(tmp), fake))
            response = client.post(
                "/api/edit",
                data={"prompt": "fix", "focused_inpainting": json.dumps({"enabled": True})},
                files={"images": ("base.png", _png(), "image/png")},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "focused_inpainting_requires_mask")

    def test_focused_edit_metadata_survives_queue_and_output_is_full_image(self) -> None:
        fake = ValidImageClient()
        config = {"enabled": True, "context": 0.25, "feather": 4, "target_size": 1024}
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(Path(tmp), fake)
            client = TestClient(app)
            response = client.post(
                "/api/edit",
                data={
                    "prompt": "fix center",
                    "size": "1536x1024",
                    "output_format": "png",
                    "focused_inpainting": json.dumps(config),
                },
                files={
                    "images": ("base.png", _png(), "image/png"),
                    "mask": ("mask.png", _png(mask=True), "image/png"),
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            task_id = response.json()["task"]["task_id"]
            queued = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertTrue(queued["focused_inpainting"]["enabled"])
            self.assertIn("mask_file", queued)

            asyncio.run(app.state.queue_manager.run_available_once())
            completed = client.get(f"/api/tasks/{task_id}").json()["task"]
            output = client.get(completed["output_url"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(fake.edit_calls), 1)
        self.assertEqual(fake.edit_calls[0]["size"], "1024x1024")
        self.assertEqual(Image.open(BytesIO(output.content)).size, (96, 72))
        self.assertTrue(completed["focused_inpainting"]["enabled"])
        self.assertEqual(completed["output_size"], "96x72")


if __name__ == "__main__":
    unittest.main()
