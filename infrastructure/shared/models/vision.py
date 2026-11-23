"""Minimal visual model client using the existing model environment variables."""

import base64
import json
from io import BytesIO
import mimetypes
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from PIL import Image, ImageOps
from infrastructure.utils.presentation.model_io import http_client, invoke, request_timeout
from infrastructure.utils.presentation.console import DemoConsole
from infrastructure.shared.agent.proposals import propose_json
from infrastructure.shared.models.text import (DEFAULT_MODEL, JSON_OUTPUT_RULE, parse_json_object,
    reasoning_arguments, reasoning_rejected, transient_retry_limit, wait_transient)


VISION_INSTRUCTIONS = """Describe the supplied UI reference using visible evidence only.
Return {"layout":"...", "elements":["..."], "style":"..."}.
layout describes regions, alignment and relative positions; elements lists visible
controls, labels, options and states; style describes colors, typography and spacing.
Each elements entry is one string that starts with its region in brackets, one of
[header], [navigation], [main], [form], [sidebar], [footer]. For a control, follow the
region with its semantic kind: heading, text field, password field, date field, dropdown,
radio group, checkbox, button, link or static text. A heading is a page or card title
shown larger or bolder than body text (for example a form's title); report it as heading,
not static text. Classify a field as a password
field when its value is masked (dots or asterisks), it shows a lock icon, or its label
or placeholder mentions a password. Classify a field as a date field when it shows a
date format, a calendar icon or a date picker. For a radio group or dropdown list every
visible option in order. Quote each control's visible label exactly, without required
markers such as asterisks and without trailing colons, and report any placeholder text
separately from the label. Neutral examples:
  [form] text field, label "Email", placeholder "you@example.com"
  [form] radio group, label "Size", options "Small", "Medium", "Large"
  [header] link, label "Home"
  [main] heading, label "Account settings"
Note visible states such as selected, checked, disabled or error messages.
Write descriptions in English while quoting visible labels in their original language.
Identify illegible or uncertain details instead of inventing them. Do not infer hidden
business behavior, APIs, database fields or interactions from appearance alone.
"""

VISUAL_REPAIR_PREFIX = "Correct the previous response:\n"


def _text(value) -> str:
    """Coerce one free-text analysis field to a string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return json.dumps(value, ensure_ascii=False)


def normalize_analysis(value) -> dict:
    """Validate the {layout, elements, style} shape, coercing missing or loose fields to defaults."""
    if not isinstance(value, dict):
        raise ValueError('Return one JSON object {"layout":"...", "elements":["..."], "style":"..."}')
    if not any(key in value for key in ("layout", "elements", "style")):
        raise ValueError('The analysis must contain layout (string), elements (array of strings) and style (string)')
    elements = value.get("elements")
    if elements is None:
        elements = []
    elif not isinstance(elements, list):
        elements = [elements]
    return {"layout": _text(value.get("layout")),
        "elements": [_text(item) for item in elements if item not in (None, "")],
        "style": _text(value.get("style"))}

def compress_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """Resize static references to 2048px and encode JPEG, retaining smaller originals."""
    with Image.open(BytesIO(data)) as source:
        if getattr(source, "is_animated", False):
            return data, mime
        image = ImageOps.exif_transpose(source)
        original_size = image.size
        image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        # Composite transparency against white so transparent UI text stays readable.
        rgba = image.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        buffer = BytesIO()
        rgb.save(buffer, format="JPEG", quality=85, subsampling=0)
        compressed = buffer.getvalue()
        if len(compressed) < len(data) or image.size != original_size:
            return compressed, "image/jpeg"
    return data, mime


class VisualModel:
    """Extract visible image evidence without inferring hidden business behavior."""
    def __init__(self) -> None:
        """Read compiler/.env and configure the image-capable model, falling back to text-model settings."""
        load_dotenv(Path(__file__).resolve().parents[3] / ".env", encoding="utf-8-sig")
        self.name = os.getenv("VISUAL_MODEL") or os.getenv("MODEL") or DEFAULT_MODEL
        api_key = os.getenv("VISUAL_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("VISUAL_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        if not api_key:
            raise ValueError("Set VISUAL_API_KEY or OPENAI_API_KEY in compiler/.env; the model must accept images")
        self.reasoning = os.getenv("VISUAL_REASONING", os.getenv("MODEL_REASONING", "low")).lower()
        if self.reasoning not in {"auto", "provider", "none", "low", "high", "max"}:
            raise ValueError("VISUAL_REASONING must be auto, provider, none, low, high or max")
        self._reasoning_supported = True
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=request_timeout(), max_retries=0, http_client=http_client())

    def analyze(self, image: Path, max_attempts: int = 10) -> dict:
        """Send one supported reference image and parse the returned layout, elements, and style evidence."""
        mime = mimetypes.guess_type(image.name)[0]
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError(f"Unsupported image format: {image.name}")
        original = image.read_bytes()
        compressed, mime = compress_image(original, mime)
        print(f"Image compression: {image.name} | {len(original) / 1024:.1f} -> {len(compressed) / 1024:.1f} KiB", flush=True)
        encoded = base64.b64encode(compressed).decode("ascii")
        result, _ = propose_json(self, VISION_INSTRUCTIONS, {"image_url": f"data:{mime};base64,{encoded}"},
            normalize_analysis, DemoConsole(), f"Visual analysis {image.name}", max_attempts)
        return result

    def generate_json(self, instructions: str, payload: dict, task_complexity: str = "high") -> dict:
        """Request visual JSON and include prior response errors when repairing it."""
        text = instructions.rstrip() + "\n" + JSON_OUTPUT_RULE
        if "correction" in payload:
            text += "\n" + VISUAL_REPAIR_PREFIX + json.dumps(payload["correction"], ensure_ascii=False)
        mode = getattr(self, "reasoning", "low")
        arguments = {
                "model": self.name,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": payload["image_url"], "detail": "high"}},
                ]}],
                "response_format": {"type": "json_object"},
            }
        transient_attempts = 0
        try:
            while True:
                controls = reasoning_arguments(self.name, mode) if getattr(self, "_reasoning_supported", True) else {}
                try:
                    response = invoke(self.client, {**arguments, **controls})
                    break
                except OpenAIError as error:
                    if transient_attempts < transient_retry_limit(error):
                        transient_attempts += 1
                        wait_transient(error, transient_attempts, "Visual model")
                        continue
                    if not getattr(self, "_reasoning_supported", True) or not reasoning_rejected(error):
                        raise
                    self._reasoning_supported = False
                    print("Provider rejected reasoning controls; using provider defaults.", flush=True)
        except OpenAIError as error:
            raise RuntimeError(f"Visual model request failed: {error}") from error
        if response.choices and response.choices[0].finish_reason == "length":
            raise ValueError("Visual model output was truncated (finish_reason=length). "
                "Return a shorter complete JSON object with concise element descriptions.")
        if response.choices and getattr(response.choices[0].message, "refusal", None):
            raise ValueError(f"Visual model refused the request: {response.choices[0].message.refusal}")
        if not response.choices or not response.choices[0].message.content:
            raise ValueError("Visual model returned empty content")
        return parse_json_object(response.choices[0].message.content)
