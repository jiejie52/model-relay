# Fusion Compatibility Deployment

Fusion is no longer part of Relay Core. This file only describes how to keep legacy Fusion jobs alive during migration.

## Database

For an existing installation, execute:

```text
sql/003_core_v2_kimi.sql
```

The migration assigns historical Fusion stages to `execution_engine=fusion-legacy-v1` and all other historical jobs to `core-legacy-v1`.

## Processes

Always run the API and Core Worker:

```bash
uvicorn app.api:app --host 0.0.0.0 --port $PORT
python -m app.worker
```

Run the legacy Fusion worker only while Fusion compatibility traffic/jobs still exist:

```bash
python -m app.application.fusion_worker
```

Core Worker claims only `core-v2/core-legacy-v1`; Fusion Worker claims only `fusion-legacy-v1`.

## Retirement

Do not retire the Fusion compatibility worker merely because new traffic has switched to `/v2`. First verify there are no non-terminal `fusion-legacy-v1` rows and no caller still relies on the old Fusion compatibility operations.
