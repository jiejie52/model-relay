"""Registered history projection capabilities, consumed by offline compilers."""

HISTORY_MATERIAL_CAPABILITIES = {
    "gemini_native": ("fileData", "inlineData"),
}


def history_material_capability(protocol):
    kinds = HISTORY_MATERIAL_CAPABILITIES.get(protocol)
    return ({"supports_native_history_material_reuse": True,
             "history_material_part_kinds": list(kinds)} if kinds else {})


def validate_history_capability(profile):
    enabled = profile.get("supports_native_history_material_reuse", False)
    kinds = profile.get("history_material_part_kinds") or []
    registered = HISTORY_MATERIAL_CAPABILITIES.get(profile.get("protocol"))
    if type(enabled) is not bool or (enabled and (not registered or tuple(kinds) != registered)) or (not enabled and kinds):
        raise ValueError("Native history material reuse requires a registered occurrence projection")
