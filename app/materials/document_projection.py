"""Versioned, contract-authorized DOCX text bridge; Canonical Material stays intact."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

from ..core.idempotency import stable_hash
from ..providers.base import ProviderRequestError
from ..storage_paths import relay_object_path
from ..utils import utcnow

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
VERSION = "relay-docx-structured-text/1"
PARSER_REVISION = 2
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def projection_authorized(contract: dict[str, Any] | None) -> bool:
    metadata = (contract or {}).get("metadata") or {}
    projections = metadata.get("material_projections") if isinstance(metadata, dict) else None
    rule = projections.get("docx_structured_text") if isinstance(projections, dict) else None
    if not isinstance(rule, dict):
        return False
    return rule.get("verification_status") in {"verified", "published"} and rule.get("version") == VERSION


def structured_docx_text(raw: bytes) -> bytes:
    """Retain ordered paragraphs/tables and annotation structure, never OCR.

    Text bridging is ineligible for media, equations, tracked edits, fields or
    unsupported blocks. Such documents must use their original binary route;
    absence of a legal projection is not permission to discard content.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        with archive:
            files = archive.infolist()
            if (len(files) > 2048 or sum(i.file_size for i in files) > 64 * 1024 * 1024
                    or len({i.filename for i in files}) != len(files)
                    or any(i.flag_bits & 1 for i in files)):
                raise ValueError("DOCX archive exceeds deterministic projection bounds")
            if any(i.filename.startswith(("word/media/", "word/embeddings/", "word/charts/")) for i in files):
                raise ValueError("DOCX has nontext material")

            def xml(name):
                value = archive.read(name)
                if b"<!DOCTYPE" in value.upper() or b"<!ENTITY" in value.upper():
                    raise ValueError("DOCX XML declarations are not supported")
                return ET.fromstring(value)

            rels = {}
            if "word/_rels/document.xml.rels" in archive.namelist():
                rels = {r.attrib.get("Id"): r.attrib.get("Target", "")
                        for r in xml("word/_rels/document.xml.rels")}

            def paragraph(p):
                chunks = []

                def inline(node):
                    local = node.tag.rsplit("}", 1)[-1]
                    if local in {"drawing", "pict", "object", "altChunk", "ins", "del", "moveFrom", "moveTo",
                                 "fldSimple", "instrText", "oMath", "oMathPara", "sdt"}:
                        raise ValueError(f"DOCX {local} requires original representation")
                    if node.tag == W + "t":
                        return node.text or ""
                    if node.tag == W + "tab":
                        return "\t"
                    if node.tag in {W + "br", W + "cr"}:
                        return "\n"
                    if local in {"footnoteReference", "endnoteReference", "commentReference"}:
                        return f"[{local}:{node.get(W + 'id', '')}]"
                    if local in {"pPr", "rPr"}:
                        return ""
                    if local == "sym":
                        return f"[SYMBOL:{node.get(W + 'font', '')}:{node.get(W + 'char', '')}]"
                    if local in {"noBreakHyphen", "softHyphen"}:
                        return "\u2011" if local == "noBreakHyphen" else "\u00ad"
                    if local in {"separator", "continuationSeparator", "lastRenderedPageBreak"}:
                        return f"[{local}]"
                    if local in {"bookmarkStart", "bookmarkEnd", "proofErr", "commentRangeStart", "commentRangeEnd"}:
                        return "[ANCHOR:" + ET.tostring(node, encoding="unicode") + "]"
                    if local not in {"p", "r", "hyperlink"}:
                        raise ValueError(f"DOCX {local} requires original representation")
                    text = "".join(inline(child) for child in node)
                    if local == "hyperlink":
                        target = rels.get(node.get(R + "id")) or node.get(W + "anchor")
                        if target:
                            text += f" [LINK:{target}]"
                    return text

                chunks.append(inline(p))
                style = p.find(f"{W}pPr/{W}pStyle")
                numbering = p.find(f"{W}pPr/{W}numPr")
                labels = []
                if style is not None:
                    labels.append("style=" + style.get(W + "val", ""))
                if numbering is not None:
                    labels.append("numbering=" + ET.tostring(numbering, encoding="unicode"))
                # Include style/run properties as annotations rather than
                # dropping color/emphasis that may carry document meaning.
                props = [ET.tostring(e, encoding="unicode") for e in p.iter()
                         if e.tag in {W + "rPr", W + "pPr"}]
                return ("[" + ";".join(labels) + "]\n" if labels else "") + "".join(chunks) + (
                    "\n[PROPERTIES:" + "".join(props) + "]" if props else ""
                )

            def blocks(parent, prefix):
                result = []
                for index, child in enumerate(parent):
                    if child.tag == W + "p":
                        result.append(f"[PARAGRAPH:{prefix}.{index}]\n" + paragraph(child))
                    elif child.tag == W + "tbl":
                        result.append(f"[TABLE:{prefix}.{index}]")
                        for table_property in child:
                            if table_property.tag in {W + "tblPr", W + "tblGrid"}:
                                result.append("[TABLE_PROPERTIES:" + ET.tostring(table_property, encoding="unicode") + "]")
                            elif table_property.tag != W + "tr":
                                raise ValueError("DOCX table block has no certified text projection")
                        for ri, row in enumerate(child.findall(W + "tr")):
                            # A cell may be wrapped by a content control or a
                            # revision. Direct findall(tc) must never silently
                            # discard those nodes and publish a partial bridge.
                            if any(node.tag not in {W + "trPr", W + "tc"} for node in row):
                                raise ValueError("DOCX table row requires original representation")
                            row_property = row.find(W + "trPr")
                            if row_property is not None:
                                result.append("[ROW_PROPERTIES:" + ET.tostring(row_property, encoding="unicode") + "]")
                            for ci, cell in enumerate(row.findall(W + "tc")):
                                result.append(f"[CELL:{ri},{ci}]\n" + "\n".join(blocks(cell, f"{prefix}.{index}.{ri}.{ci}")))
                                props = cell.find(W + "tcPr")
                                if props is not None:
                                    result.append("[CELL_PROPERTIES:" + ET.tostring(props, encoding="unicode") + "]")
                    elif child.tag in {W + "sectPr", W + "tcPr"}:
                        result.append("[SECTION_PROPERTIES:" + ET.tostring(child, encoding="unicode") + "]")
                    else:
                        raise ValueError("DOCX block has no certified text projection")
                return result

            root = xml("word/document.xml")
            body = root.find(W + "body")
            if body is None:
                raise ValueError("DOCX body missing")
            lines = blocks(body, "body")
            # Definitions retain list labels, style meaning and all side notes.
            for name in sorted(archive.namelist()):
                if name.startswith("word/_rels/") and name.endswith(".rels"):
                    lines.append(f"[RELATIONSHIPS:{name}]\n" + ET.tostring(xml(name), encoding="unicode"))
                elif name in {"word/styles.xml", "word/numbering.xml"}:
                    lines.append(f"[DEFINITIONS:{name}]\n" + ET.tostring(xml(name), encoding="unicode"))
                elif name.startswith(("word/header", "word/footer")) and name.endswith(".xml"):
                    rel_name = "word/_rels/" + name.split("/")[-1] + ".rels"
                    rels = ({r.attrib.get("Id"): r.attrib.get("Target", "") for r in xml(rel_name)}
                            if rel_name in archive.namelist() else {})
                    lines.append(f"[SECTION:{name}]\n" + "\n".join(blocks(xml(name), name)))
                elif name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}:
                    rel_name = "word/_rels/" + name.split("/")[-1] + ".rels"
                    rels = ({r.attrib.get("Id"): r.attrib.get("Target", "") for r in xml(rel_name)}
                            if rel_name in archive.namelist() else {})
                    for note in xml(name):
                        lines.append(f"[NOTE:{name}:{json.dumps(note.attrib, sort_keys=True, ensure_ascii=False)}]\n"
                                     + "\n".join(blocks(note, name)))
            output = ("\n\n".join(lines) + "\n").encode("utf-8")
            if len(output) > 32 * 1024 * 1024:
                raise ValueError("DOCX text bridge exceeds representation budget")
            return output
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, RuntimeError) as exc:
        raise ProviderRequestError("DOCX_PROJECTION_INELIGIBLE", str(exc)) from exc


async def prepare_document_binding(*, repo, fallback_store, material, fallback, connection_id,
                                   account_scope_hash, tenant_id, conversation_hash, session_id):
    identity = {"version": VERSION, "parser_revision": PARSER_REVISION,
                "material_id": str(material["id"]), "source_sha256": material.get("sha256"),
                "tenant": tenant_id, "conversation": conversation_hash, "session": session_id,
                "connection": connection_id, "account": account_scope_hash}
    if not identity["source_sha256"] or not session_id:
        raise ProviderRequestError("MATERIAL_INTEGRITY_MISMATCH", "DOCX projection requires canonical content and Session identity")
    object_id = "obj_docxproj_" + stable_hash(identity)
    obj = await repo.get_object(object_id, tenant_id=tenant_id, conversation_hash=conversation_hash)
    data = None
    if obj:
        from .resolver import MaterialResolver
        data = await MaterialResolver(repo, fallback_store.storage, fallback_store.settings).read_object(obj)
        if hashlib.sha256(data).hexdigest() != obj.get("sha256"):
            raise ProviderRequestError("MATERIAL_INTEGRITY_MISMATCH", "DOCX bridge artifact checksum mismatch")
    if data is None:
        raw = await fallback_store.read(fallback)
        if hashlib.sha256(raw).hexdigest() != material["sha256"]:
            raise ProviderRequestError("MATERIAL_INTEGRITY_MISMATCH", "DOCX source checksum mismatch")
        data = structured_docx_text(raw)
        path = relay_object_path(fallback_store.settings, tenant_id, conversation_hash, object_id, "document.txt")
        location = await fallback_store.storage.get(fallback_store.settings.default_storage_id).put_bytes(
            path, data, content_type="text/plain; charset=utf-8")
        obj = {"id": object_id, "tenant_id": tenant_id, "conversation_hash": conversation_hash,
               "storage_id": location.storage_id, "bucket": location.bucket, "object_key": location.key,
               "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
               "content_type": "text/plain", "created_at": utcnow().isoformat()}
        await repo.upsert_material_projection_object(obj)
    return {"material_id": str(material["id"]), "binding_generation": 1,
            "binding_kind": "relay_structured_text", "representation": "relay_structured_text",
            "connection_id": connection_id, "account_scope_hash": account_scope_hash,
            "content_sha256": material["sha256"], "content_type": material["content_type"],
            "filename": material.get("filename"), "object_id": fallback["object_id"],
            "projection_object_id": object_id, "projection_sha256": obj["sha256"],
            "projection_size_bytes": len(data), "projection_version": VERSION,
            "projection_parser_revision": PARSER_REVISION,
            "projected_content_type": "text/plain", "metadata": {"is_docx_text_projection": True}}
