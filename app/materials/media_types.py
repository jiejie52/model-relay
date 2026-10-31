from __future__ import annotations


DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_GENERIC_UPLOAD_MIME_TYPES = {
    "application/octet-stream",
    "application/zip",
    "binary/octet-stream",
}


def is_docx_material(*, filename: object = None, content_type: object = None) -> bool:
    """Recognize DOCX from canonical MIME, or a .docx name with generic MIME.

    A conflicting specific MIME type wins over the filename so a renamed PDF,
    image, or other material cannot receive the DOCX cache policy by extension
    alone. MIME parameters are ignored for comparison.
    """

    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    name = str(filename or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if mime == DOCX_MIME_TYPE:
        return True
    if not name.endswith(".docx"):
        return False
    return not mime or mime in _GENERIC_UPLOAD_MIME_TYPES
