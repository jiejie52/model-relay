# V20.25.23 Fusion Async Resume / Callback Contract

- Adds durable callback registration on `relay_jobs`.
- Adds callback leasing/retry outbox via `claim_relay_callback`.
- Adds a callback dispatcher loop to `relay-worker` without changing existing model-job leasing.
- Dify callback target and API key are Railway-only configuration; job payload cannot choose a URL.
- Resume commands are derived from the server-known Fusion stage; arbitrary callback commands are rejected by construction.
- Corpus ingest compact result now includes deterministic route metrics so a result-only callback can continue analysis without replaying uploaded files.
- Existing `/v1/jobs`, `/v1/dify/relay`, compact result, Fusion Artifact, and Provider contracts remain compatible.
