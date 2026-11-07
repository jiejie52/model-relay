from __future__ import annotations

import copy
import json
import re
from typing import Any

from .base import ProviderRequestError
from ..core.idempotency import stable_hash
from ..materials.gemini_transport import project_gemini_input_content_type

PROJECTION_VERSION = "gemini-cachedcontent-prefix/1"
_HANDLE_RE = re.compile(r"^cachedContents/[A-Za-z0-9._~-]+$")


def validate_cached_content_handle(value: Any) -> str:
    handle = str(value or "")
    if not _HANDLE_RE.fullmatch(handle):
        raise ProviderRequestError(
            "CACHE_BINDING_INVALID",
            "Gemini CachedContent handle is not an allowed relative resource name",
        )
    return handle


def project_system_instruction(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    value = snapshot.get("instructions")
    if value in (None, ""):
        return None
    return {"parts": [{"text": str(value)}]}


def project_history_contents(
    history: list[dict[str, Any]],
    *,
    start_entry: int = 0,
    end_entry: int | None = None,
) -> list[dict[str, Any]]:
    """Project persisted logical history back to Gemini native Content objects.

    Entry indexes, rather than raw Content indexes, are used as cache-prefix
    boundaries.  A history entry contains the user Content and the corresponding
    model Content; keeping that pair atomic prevents a cache boundary from
    splitting a committed turn.
    """
    result: list[dict[str, Any]] = []
    selected = history[max(0, int(start_entry)) : end_entry]
    for turn in selected:
        transport = turn.get("transport_history") if isinstance(turn, dict) else None
        if not isinstance(transport, dict) or transport.get("kind") != "gemini_native":
            continue
        user_content = transport.get("user_content")
        model_content = transport.get("model_content")
        if isinstance(user_content, dict):
            result.append(copy.deepcopy(user_content))
        if isinstance(model_content, dict):
            result.append(copy.deepcopy(model_content))
    return result


def project_material_part(binding: dict[str, Any], *, material_id: str | None = None) -> dict[str, Any]:
    representation = str(binding.get("representation") or binding.get("binding_kind") or "")
    if representation == "gemini_inline_data":
        encoded = binding.get("inline_data")
        if not isinstance(encoded, str) or not encoded:
            label = material_id or str(binding.get("material_id") or "material")
            raise ProviderRequestError(
                "MATERIAL_BINDING_INVALID",
                f"Gemini inline binding has no payload for {label}",
            )
        return {
            "inlineData": {
                "mimeType": project_gemini_input_content_type(binding.get("content_type")),
                "data": encoded,
            }
        }

    file_uri = binding.get("external_uri")
    if not file_uri:
        label = material_id or str(binding.get("material_id") or "material")
        raise ProviderRequestError(
            "MATERIAL_BINDING_INVALID",
            f"Gemini binding has no file URI for {label}",
        )
    return {
        "fileData": {
            "mimeType": project_gemini_input_content_type(binding.get("content_type")),
            "fileUri": str(file_uri),
        }
    }


def project_current_user_content(
    snapshot: dict[str, Any],
    *,
    material_ids: list[str],
    material_bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    by_id = {str(x.get("material_id")): x for x in material_bindings}
    for material_id in material_ids:
        binding = by_id.get(str(material_id))
        if not binding:
            raise ProviderRequestError(
                "MATERIAL_BINDING_MISSING",
                f"Frozen Gemini binding missing for {material_id}",
            )
        parts.append(project_material_part(binding, material_id=str(material_id)))

    value = snapshot.get("input")
    if isinstance(value, str):
        query_text = value
    else:
        query_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parts.append({"text": query_text})
    return {"role": "user", "parts": parts}


def prefix_payload(
    *,
    snapshot: dict[str, Any],
    history: list[dict[str, Any]],
    history_entries: int,
) -> dict[str, Any]:
    contents = project_history_contents(history, end_entry=max(0, int(history_entries)))
    value: dict[str, Any] = {}
    if contents:
        value["contents"] = contents
    instruction = project_system_instruction(snapshot)
    if instruction is not None:
        value["systemInstruction"] = instruction
    return value


def prefix_fingerprint(
    *,
    snapshot: dict[str, Any],
    history: list[dict[str, Any]],
    history_entries: int,
) -> str:
    return stable_hash(
        {
            "projection_version": PROJECTION_VERSION,
            "model": str(snapshot.get("model") or ""),
            "prefix": prefix_payload(
                snapshot=snapshot,
                history=history,
                history_entries=history_entries,
            ),
        }
    )


def prefix_reuse_key(snapshot: dict[str, Any]) -> str:
    """Identity for a chain in which older prefixes can safely serve newer turns.

    Session/offering/account/profile isolation is already provided by the
    Stateful manager's scope_hash.  The reuse key additionally locks the model,
    system instruction and projection version so a resource can never cross a
    semantic prefix family.
    """
    return stable_hash(
        {
            "projection_version": PROJECTION_VERSION,
            "model": str(snapshot.get("model") or ""),
            "system_instruction": project_system_instruction(snapshot),
        }
    )
