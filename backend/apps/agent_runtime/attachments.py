"""Validated chat attachments and multimodal model message helpers."""
from __future__ import annotations

import base64
import io
import re
from typing import Any

from PIL import Image
from pypdf import PdfReader

MAX_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_PDF_TEXT = 120_000
_ALLOWED_IMAGES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)


def validate_chat_attachments(values: Any) -> list[dict[str, Any]]:
    if values in (None, ""):
        return []
    if not isinstance(values, list) or len(values) > MAX_ATTACHMENTS:
        raise ValueError(f"attachments must be a list with at most {MAX_ATTACHMENTS} files")
    return [_validate_one(value, index) for index, value in enumerate(values)]


def public_attachment_metadata(values: Any) -> list[dict[str, Any]]:
    return [
        {key: value.get(key) for key in ("name", "mime_type", "size", "page_count") if value.get(key) is not None}
        for value in list(values or []) if isinstance(value, dict)
    ]


def message_content_with_attachments(text: str, values: Any) -> str | list[dict[str, Any]]:
    attachments = list(values or [])
    image_parts = []
    document_parts = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime_type = str(item.get("mime_type") or "")
        if mime_type in _ALLOWED_IMAGES and item.get("data_url"):
            image_parts.append({"type": "image_url", "image_url": {"url": item["data_url"]}})
        elif mime_type == "application/pdf":
            document_parts.append(
                f"[PDF attachment: {item.get('name') or 'document.pdf'}]\n{item.get('extracted_text') or '[No extractable text]'}"
            )
    combined_text = "\n\n".join(part for part in [str(text or ""), *document_parts] if part)
    if not image_parts:
        return combined_text
    return [{"type": "text", "text": combined_text or "Please inspect the attached image(s)."}, *image_parts]


def _validate_one(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"attachment {index + 1} is invalid")
    name = str(value.get("name") or f"attachment-{index + 1}").strip()[:255]
    match = _DATA_URL.match(str(value.get("data_url") or ""))
    if not match:
        raise ValueError(f"attachment {name} must use a base64 data URL")
    mime_type = match.group(1).lower().strip()
    if mime_type not in _ALLOWED_IMAGES | {"application/pdf"}:
        raise ValueError(f"attachment type {mime_type} is not supported")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError(f"attachment {name} contains invalid base64") from exc
    limit = MAX_PDF_BYTES if mime_type == "application/pdf" else MAX_IMAGE_BYTES
    if not content or len(content) > limit:
        raise ValueError(f"attachment {name} exceeds the size limit")
    result = {"name": name, "mime_type": mime_type, "size": len(content), "data_url": value["data_url"]}
    if mime_type == "application/pdf":
        reader = PdfReader(io.BytesIO(content))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"attachment {name} exceeds {MAX_PDF_PAGES} pages")
        pages = []
        for page_number, page in enumerate(reader.pages, start=1):
            pages.append(f"--- Page {page_number} ---\n{page.extract_text() or ''}")
        result.pop("data_url", None)
        result.update(page_count=len(reader.pages), extracted_text="\n\n".join(pages)[:MAX_PDF_TEXT])
    else:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
    return result
