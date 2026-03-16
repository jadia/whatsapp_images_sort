"""
============================================================
test_prompt_builder.py — Prompt Builder Tests
============================================================
Tests prompt construction for both Standard and Batch modes.
============================================================
"""

import json

import pytest

from src.prompt_builder import (
    build_batch_request,
    build_standard_parts,
    build_standard_prompt,
)
from src.config_manager import CategoryDef

def mock_cats(names: list[str]) -> list[CategoryDef]:
    return [CategoryDef(name=n, description=f"Desc of {n}") for n in names]


class TestBuildStandardPrompt:
    """Tests for Standard mode prompt generation."""

    def test_prompt_contains_image_count(self):
        """Prompt should dynamically state the number of images."""
        prompt = build_standard_prompt(7, mock_cats(["Cat A", "Cat B"]), "Uncategorized_Review", ["Rule 1"])
        assert "exactly 7 image" in prompt
        assert "Image_1 through Image_7" in prompt

    def test_prompt_lists_all_categories(self):
        """All categories should appear in the prompt."""
        categories = ["Documents & IDs", "People & Social", "Memes & Junk"]
        prompt = build_standard_prompt(3, mock_cats(categories), "Uncategorized_Review", ["Rule 1"])

        for cat in categories:
            assert cat in prompt

    def test_prompt_enforces_uncategorized_review(self):
        """Prompt should mention Uncategorized_Review as fallback."""
        prompt = build_standard_prompt(1, mock_cats(["Test"]), "Uncategorized_Review", ["Rule 1"])
        assert "Uncategorized_Review" in prompt

    def test_prompt_demands_json_output(self):
        """Prompt should ask for JSON array output."""
        prompt = build_standard_prompt(2, mock_cats(["Cat"]), "Uncategorized_Review", ["Rule 1"])
        assert "JSON" in prompt
        assert '"image"' in prompt
        assert '"category"' in prompt

    def test_prompt_handles_single_image(self):
        """Prompt should work correctly for 1 image."""
        prompt = build_standard_prompt(1, mock_cats(["Cat"]), "Uncategorized_Review", ["Rule 1"])
        assert "exactly 1 image" in prompt

    def test_prompt_handles_large_batch(self):
        """Prompt should work for large batches."""
        prompt = build_standard_prompt(50, mock_cats(["A", "B", "C"]), "Uncategorized_Review", ["Rule 1"])
        assert "exactly 50 image" in prompt
        assert "Image_1 through Image_50" in prompt


class TestBuildStandardParts:
    """Tests for Standard mode parts array construction."""

    def test_parts_interleave_text_and_image(self):
        """Parts should alternate: text label, image data."""
        images = [
            ("Image_1", b"\xff\xd8\xff\xe0"),
            ("Image_2", b"\xff\xd8\xff\xe1"),
        ]
        parts = build_standard_parts(images)

        # Should have 4 parts: text, image, text, image
        assert len(parts) == 4
        assert parts[0]["text"] == "Image_1:"
        assert "inline_data" in parts[1]
        assert parts[2]["text"] == "Image_2:"
        assert "inline_data" in parts[3]

    def test_parts_use_correct_mime_type(self):
        """All image parts should use image/jpeg mime type."""
        images = [("Image_1", b"\xff\xd8\xff\xe0")]
        parts = build_standard_parts(images)

        assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"

    def test_parts_base64_encoded(self):
        """Image data should be base64-encoded."""
        original_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        images = [("Image_1", original_bytes)]
        parts = build_standard_parts(images)

        import base64
        encoded = parts[1]["inline_data"]["data"]
        decoded = base64.b64decode(encoded)
        assert decoded == original_bytes

    def test_empty_images_returns_empty_parts(self):
        """No images should return empty parts list."""
        parts = build_standard_parts([])
        assert len(parts) == 0


class TestBuildBatchRequest:
    """Tests for Batch mode JSONL request construction."""

    def test_batch_request_structure(self):
        """Batch request should have correct top-level keys."""
        req = build_batch_request(
            image_uri="files/abc123",
            image_label="Image_1",
            categories=mock_cats(["Cat A", "Cat B"]),
            fallback_category="Fallback",
            global_rules=["Rule 1"],
            model="gemini-3-flash-lite",
        )

        assert "key" in req
        assert req["key"] == "Image_1"
        assert "request" in req
        assert "model" in req["request"]
        assert "contents" in req["request"]

    def test_batch_request_model_path(self):
        """Model should be prefixed with 'models/'."""
        req = build_batch_request(
            image_uri="files/abc123",
            image_label="Image_1",
            categories=mock_cats(["Cat"]),
            fallback_category="Uncategorized_Review",
            global_rules=["Rule 2"],
            model="gemini-3-flash-lite",
        )

        assert req["request"]["model"] == "models/gemini-3-flash-lite"

    def test_batch_request_contains_categories(self):
        """Prompt in request should list all categories."""
        categories = ["Test A", "Test B"]
        req = build_batch_request(
            image_uri="files/abc",
            image_label="Image_1",
            categories=mock_cats(categories),
            fallback_category="Uncategorized_Review",
            global_rules=["Test rule"],
            model="model",
        )

        # Find the text part
        parts = req["request"]["contents"][0]["parts"]
        text_part = parts[0]["text"]

        for cat in categories:
            assert cat in text_part

    def test_batch_request_references_file_uri(self):
        """Request should reference the File API URI."""
        req = build_batch_request(
            image_uri="files/uploaded_123",
            image_label="Image_1",
            categories=mock_cats(["Cat"]),
            fallback_category="Uncategorized_Review",
            global_rules=[],
            model="model",
        )

        parts = req["request"]["contents"][0]["parts"]
        file_part = parts[1]
        assert file_part["file_data"]["file_uri"] == "files/uploaded_123"

    def test_batch_request_enforces_uncategorized(self):
        """Batch prompt should mention Uncategorized_Review."""
        req = build_batch_request(
            image_uri="files/abc",
            image_label="Image_1",
            categories=mock_cats(["Cat"]),
            fallback_category="Uncategorized_Review",
            global_rules=[],
            model="model",
        )

        parts = req["request"]["contents"][0]["parts"]
        text_part = parts[0]["text"]
        assert "Uncategorized_Review" in text_part

    def test_batch_request_json_serializable(self):
        """Entire request should be JSON-serializable."""
        req = build_batch_request(
            image_uri="files/abc",
            image_label="Image_1",
            categories=mock_cats(["Cat A"]),
            fallback_category="Uncategorized",
            global_rules=["Some rule"],
            model="gemini-3-flash-lite",
        )

        # Should not raise
        serialized = json.dumps(req)
        assert len(serialized) > 0
