from __future__ import annotations
from typing import Any

def material_cache_class(content_type: Any) -> str:
    mime = str(content_type or "").split(";", 1)[0].lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx_content"
    if mime == "application/pdf":
        return "pdf_document"
    if mime.startswith("video/"):
        return "video_media"
    return "material_other"

