# Model Relay 0.5.8 Deployment

## Scope

0.5.8 is a code-only compatibility release over the 0.5.7 runtime package. It fixes Dify requests for `kimi-k2.7-code` that send canonical `think_level=low/high/max`.

## Behavior

- `kimi-k2.7-code*`: accepts `auto/low/high/max` at Relay admission. Explicit `low/high/max` are retained as requested intent and normalized to effective `auto`, because K2.7 Code is always-thinking and no adjustable native effort contract is relied on here.
- `kimi-k3*`: remains native `auto/low/high/max`; explicit values map to top-level `reasoning_effort`.
- Other `kimi-*`: remain fail-closed for explicit effort unless a model-specific capability profile enables it.

## Deployment

1. Deploy this package with the same environment variables as the previous runtime release.
2. No SQL migration is required.
3. Restart both API and Worker processes from the same image/package.
4. Create a new Kimi Session after deployment because the frozen capability revision changed.
5. Verify `/health` reports version `0.5.8-kimi-k27-effort-compat` and capability revision `relay-model-options/2026-09-23.3`.
6. A Dify request using model `kimi-k2.7-code` and `think_level=low` should pass Relay option validation and proceed to Provider execution.

## Runtime identities

- Capability revision: `relay-model-options/2026-09-23.3`
- Moonshot adapter: `moonshot-chat/4`
