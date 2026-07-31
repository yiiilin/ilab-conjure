#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "ilab-conjure-operations" / "scripts" / "ilab_conjure_ops.py"
SPEC = importlib.util.spec_from_file_location("ilab_conjure_ops", MODULE_PATH)
assert SPEC and SPEC.loader
ops = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ops
SPEC.loader.exec_module(ops)


class CoreTests(unittest.TestCase):
    def test_redaction_is_recursive_and_keeps_usage_tokens(self):
        value = {
            "api_key": "secret-value",
            "nested": {"password": "pw", "output_tokens": 123},
            "items": [{"authorization": "Basic abc"}],
        }
        result = ops.redact(value)
        self.assertEqual(result["api_key"], "[REDACTED]")
        self.assertEqual(result["nested"]["password"], "[REDACTED]")
        self.assertEqual(result["nested"]["output_tokens"], 123)
        self.assertEqual(result["items"][0]["authorization"], "[REDACTED]")

    def test_safe_filename_removes_path_and_shell_characters(self):
        self.assertEqual(ops.safe_filename("../../hello world;$(x).png", "x.png"), "hello-world-x-.png")

    def test_multipart_supports_repeated_fields_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            body, content_type = ops.encode_multipart(
                [("prompt", "测试"), ("tag", "a"), ("tag", "b")],
                [("images", path)],
            )
            self.assertIn("multipart/form-data; boundary=", content_type)
            self.assertIn('name="prompt"'.encode(), body)
            self.assertEqual(body.count('name="tag"'.encode()), 2)
            self.assertIn('name="images"; filename="input.png"'.encode(), body)
            self.assertIn(b"\x89PNG", body)

    def test_client_rejects_group_readable_netrc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "netrc"
            path.write_text("machine conjure.example login example-user password sample\n", encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ops.CLIError, "permissions are too broad"):
                ops.Client(base_url="https://conjure.example", netrc_path=str(path), timeout=1)

    def test_client_accepts_owner_only_netrc_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "netrc"
            path.write_text('machine conjure.example login example-user password "sample"\n', encoding="utf-8")
            os.chmod(path, 0o600)
            client = ops.Client(base_url="https://conjure.example", netrc_path=str(path), timeout=1)
            self.assertTrue(client.auth_header.startswith("Basic "))
            self.assertNotIn("sample", client.auth_header)

    def test_completed_outputs_are_one_based(self):
        task = {
            "outputs": [
                {"index": 1, "status": "completed", "url": "/a.png"},
                {"index": 2, "status": "failed"},
                {"index": 3, "status": "completed", "url": "/c.png"},
            ]
        }
        self.assertEqual([item["index"] for item in ops.completed_outputs(task)], [1, 3])

    def test_clone_form_fields_preserve_snapshot_and_replace_prompt_only(self):
        task = {
            "task_id": "source-1",
            "mode": "edit",
            "prompt": "old prompt",
            "params": {"main_model": "gpt-5.4-mini", "prompt_fidelity": "strict"},
            "generation_snapshot": {
                "canonical_model_id": "gpt-image-2",
                "provider_id": "example-provider",
                "provider_name": "Example Provider",
                "binding_id": "example-gpt-image-2",
                "protocol_profile": "openai_images",
                "parameter_codec": "gpt_openai_images",
                "remote_model_id": "gpt-image-2",
                "requested_parameters": {
                    "canvas.size": "864x1536",
                    "gpt.quality": "high",
                    "output.count": 4,
                    "output.format": "png",
                },
            },
            "reference_assets": [{"id": "asset-1"}],
            "gallery_refs": [],
        }
        mode, fields, summary = ops.clone_form_fields(task, "new prompt")
        field_map = {}
        for key, value in fields:
            field_map.setdefault(key, []).append(value)
        self.assertEqual(mode, "edit")
        self.assertEqual(field_map["prompt"], ["new prompt"])
        self.assertEqual(field_map["reference_asset_ids"], ["asset-1"])
        self.assertEqual(json.loads(field_map["parameters_json"][0]), task["generation_snapshot"]["requested_parameters"])
        self.assertEqual(summary["source_task_id"], "source-1")
        self.assertNotEqual(summary["source_prompt_sha256"], summary["new_prompt_sha256"])

    def test_parser_uses_real_prompt_fidelity_values(self):
        parser = ops.build_parser()
        args = parser.parse_args(["generate", "--prompt", "x", "--prompt-fidelity", "original", "--dry-run"])
        self.assertEqual(args.prompt_fidelity, "original")

    def test_generate_parser_accepts_legacy_generation_aliases(self):
        parser = ops.build_parser()
        args = parser.parse_args(
            [
                "generate",
                "--prompt",
                "x",
                "--model",
                "gpt-image-2",
                "--canvas",
                "864x1536",
                "--strict",
                "--ref",
                "/tmp/reference-a.png",
                "--ref",
                "/tmp/reference-b.png",
            ]
        )
        self.assertEqual(args.model, "gpt-image-2")
        self.assertEqual(args.size, "864x1536")
        self.assertEqual(args.prompt_fidelity, "strict")
        self.assertEqual(args.reference_image, ["/tmp/reference-a.png", "/tmp/reference-b.png"])

    def test_parser_supports_local_no_auth(self):
        parser = ops.build_parser()
        args = parser.parse_args(["--no-auth", "health"])
        self.assertTrue(args.no_auth)
        client = ops.Client(base_url="http://127.0.0.1:8787", netrc_path=None, timeout=1)
        self.assertEqual(client.auth_header, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
