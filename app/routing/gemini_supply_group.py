"""Compatibility exports of static governance and request-time supply resolution."""
from app.control_plane.supply_group import extend_gemini_supplies
from app.execution_plane.supply_resolver import context_settings, resolve_supply_group, materialize_supply_group
