"""
============================================================
prompt_builder.py — Dynamic Gemini Prompt Construction
============================================================
Builds structured prompts for image categorization:

Standard mode:
  - System prompt dynamically states the exact image count.
  - Lists allowed categories from config.
  - Enforces Uncategorized_Review fallback.
  - Demands strict JSON array output.
  - Builds interleaved text+image parts array.

Batch mode:
  - Builds individual GenerateContentRequest dicts for JSONL.
============================================================
"""

import base64
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("whatsapp_sorter")


def build_standard_prompt(num_images: int, categories: List[str]) -> str:
    """
    Build the system/instruction prompt for Standard mode.

    The prompt dynamically adjusts to the exact number of
    images in the current batch (handles partial final batches).

    Args:
        num_images: Exact number of images in this batch.
        categories: List of valid category names from config.

    Returns:
        The complete prompt string.
    """
    # Format category list as a numbered list for clarity
    category_list = "\n".join(f"  {i+1}. {cat}" for i, cat in enumerate(categories))

    prompt = f"""You are an expert image categorization assistant. You will be given exactly {num_images} image(s) labeled Image_1 through Image_{num_images}.

For EACH image, classify it into exactly ONE of the following categories:
{category_list}

IMPORTANT RULES:
- If an image does NOT clearly fit any of the above categories, you MUST classify it as "Uncategorized_Review".
- You MUST return a valid JSON array with exactly {num_images} object(s).
- Each object must have exactly two keys: "image" and "category".
- The "image" value must match the label exactly (e.g., "Image_1").
- The "category" value must be exactly one of the listed categories or "Uncategorized_Review".
- Do NOT include any text outside the JSON array.

Example output format for {num_images} image(s):
[
  {{"image": "Image_1", "category": "People & Social"}},
  {{"image": "Image_2", "category": "Uncategorized_Review"}}
]

Return ONLY the JSON array. No explanations, no markdown formatting."""

    logger.debug("Built standard prompt for %d images, %d categories", num_images, len(categories))
    return prompt


def build_standard_parts(
    images: List[Tuple[str, bytes]],
) -> List[Dict[str, Any]]:
    """
    Build the interleaved parts array for a Standard mode API call.

    Each image gets a text label part ("Image_N:") followed by
    an inline_data part containing the base64-encoded JPEG.

    Args:
        images: List of (label, jpeg_bytes) tuples.
            label: e.g., "Image_1"
            jpeg_bytes: Resized JPEG image data.

    Returns:
        List of dicts suitable for the Gemini parts array.
        Each dict is either:
          {"text": "Image_N:"}
          {"inline_data": {"mime_type": "image/jpeg", "data": "<base64>"}}
    """
    parts: List[Dict[str, Any]] = []

    for label, jpeg_bytes in images:
        # Text label part
        parts.append({"text": f"{label}:"})

        # Base64-encoded image part
        b64_data = base64.b64encode(jpeg_bytes).decode("ascii")
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": b64_data,
            }
        })

    logger.debug("Built %d parts for %d images", len(parts), len(images))
    return parts


def build_batch_request(
    image_uri: str,
    image_label: str,
    categories: List[str],
    model: str,
) -> Dict[str, Any]:
    """
    Build a single GenerateContentRequest dict for Batch mode JSONL.

    Each line in the batch JSONL file is one self-contained
    request for a single image.

    Args:
        image_uri: The File API URI (e.g., "files/abc123").
        image_label: Label like "Image_1" for response matching.
        categories: List of valid categories from config.
        model: The model name to use.

    Returns:
        Dict conforming to Gemini Batch API JSONL format with
        a custom 'key' field for matching responses to inputs.
    """
    category_list = ", ".join(f'"{cat}"' for cat in categories)

    prompt_text = (
        f'Classify this image into exactly ONE category from: [{category_list}]. '
        f'If it does not fit any category, use "Uncategorized_Review". '
        f'Return ONLY a JSON object: {{"image": "{image_label}", "category": "<chosen>"}}'
    )

    request = {
        "key": image_label,
        "request": {
            "model": f"models/{model}",
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt_text},
                        {"file_data": {"file_uri": image_uri, "mime_type": "image/jpeg"}},
                    ],
                }
            ],
            "generation_config": {
                "response_mime_type": "application/json",
                "temperature": 0.1,
            },
        },
    }

    logger.debug("Built batch request for %s → %s", image_label, image_uri)
    return request
