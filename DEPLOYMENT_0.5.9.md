# Model Relay 0.5.9 Deployment

## Scope

0.5.9 changes `kimi-k2.7-code*` from "caller depth compatibility" to an explicit always-thinking execution contract.

## Behavior

- `kimi-k2.7-code*`: any caller `think_level` value is accepted. Relay preserves it as `requested_think_level` for diagnostics, but freezes effective `think_level=on`.
- `MoonshotChatAdapter`: for `kimi-k2.7-code*`, sends native `thinking: {"type":"enabled"}` on every v2.2 request and never sends `reasoning_effort` for this model.
- `kimi-k3*`: unchanged; `auto/low/high/max` remain model-selectable and explicit levels map to `reasoning_effort`.
- Other `kimi-*`: unchanged and remain governed by their model-specific capability profiles.

## Deployment

1. Deploy this package to API and Worker from the same image/source.
2. No SQL migration is required.
3. Restart API and Worker.
4. Create a new Kimi Session because the capability revision changed.
5. Verify `/health` reports `0.5.9-kimi-k27-thinking-on` and capability revision `relay-model-options/2026-09-23.4`.
6. For `kimi-k2.7-code`, test multiple Dify values such as `low`, `medium`, `high`, `max`, and `auto`; all should pass Relay admission and produce the same native Thinking ON execution semantics.

## Runtime identities

- Capability revision: `relay-model-options/2026-09-23.4`
- Moonshot adapter: `moonshot-chat/5`
- API version: `0.5.9-kimi-k27-thinking-on`
