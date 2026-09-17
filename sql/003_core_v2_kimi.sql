-- Relay Core v2 + official Kimi/Moonshot support.
-- Existing deployments: run AFTER 001_relay_schema.sql and 002_fusion_runtime.sql.
-- This migration keeps old job_id values readable/resumable and separates the
-- Core worker from the legacy Fusion application worker by execution_engine.

begin;

alter table public.relay_sessions
    add column if not exists upstream_profile_id text,
    add column if not exists protocol text,
    add column if not exists history_codec text,
    add column if not exists history_mode text not null default 'append',
    add column if not exists context_object_path text,
    add column if not exists context_identity text,
    add column if not exists active_job_id uuid,
    add column if not exists idempotency_key text,
    add column if not exists request_fingerprint text,
    add column if not exists metadata jsonb not null default '{}'::jsonb;

alter table public.relay_jobs
    add column if not exists request_fingerprint text,
    add column if not exists execution_engine text,
    add column if not exists expected_history_version bigint,
    add column if not exists protocol_snapshot text,
    add column if not exists capability_profile_version text,
    add column if not exists lease_fence bigint not null default 0,
    add column if not exists raw_error_object_path text,
    add column if not exists raw_error_meta jsonb,
    add column if not exists provider_response_id text;

-- Preserve existing sessions without rewriting their stored objects.
update public.relay_sessions
   set context_object_path = material_prefix_object_path
 where context_object_path is null
   and material_prefix_object_path is not null;

-- Old rows are routed to the correct compatibility worker. New rows explicitly
-- choose core-v2/core-legacy-v1/fusion-legacy-v1 at submission time.
update public.relay_jobs
   set execution_engine = case
        when stage in (
            'fusion_corpus_ingest',
            'material_evidence_mapping',
            'global_adjudication',
            'scoped_decision',
            'final_evidence_review',
            'direct_final_synthesis',
            'synthesis_blueprint',
            'final_draft_generation',
            'quality_review',
            'evidence_grounded_repair'
        ) then 'fusion-legacy-v1'
        else 'core-legacy-v1'
   end
 where execution_engine is null;

alter table public.relay_jobs
    alter column execution_engine set default 'core-v2';

alter table public.relay_jobs
    alter column execution_engine set not null;

-- v2 reserves a session before making an append job visible to workers.
alter table public.relay_jobs
    drop constraint if exists relay_jobs_status_check;

alter table public.relay_jobs
    add constraint relay_jobs_status_check check (
        status in (
            'prepared', 'queued', 'leased', 'running', 'succeeded',
            'failed', 'cancelled', 'expired'
        )
    );

-- Idempotency is owner scoped and fingerprints distinguish safe replay from a
-- caller accidentally reusing the same key for different payloads.
drop index if exists public.relay_jobs_idempotency_uq;
create unique index if not exists relay_jobs_owner_idempotency_uq
    on public.relay_jobs (tenant_id, conversation_hash, idempotency_key);

create unique index if not exists relay_sessions_owner_idempotency_uq
    on public.relay_sessions (tenant_id, conversation_hash, idempotency_key)
    where idempotency_key is not null;

create index if not exists relay_jobs_execution_queue_idx
    on public.relay_jobs (execution_engine, status, created_at);

create index if not exists relay_sessions_active_job_idx
    on public.relay_sessions (active_job_id)
    where active_job_id is not null;

-- Core worker claim. Lease fence increments every claim/re-claim, preventing a
-- stale worker from committing after its lease generation has been superseded.
create or replace function public.claim_relay_job_core(
    p_worker_id text,
    p_lease_seconds integer default 120
)
returns setof public.relay_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job_id uuid;
begin
    select j.id
      into v_job_id
      from public.relay_jobs j
     where j.expires_at > now()
       and j.execution_engine in ('core-v2', 'core-legacy-v1')
       and (
            j.status = 'queued'
            or (
                j.status in ('leased', 'running')
                and j.lease_expires_at is not null
                and j.lease_expires_at < now()
            )
       )
     order by j.created_at asc
     for update skip locked
     limit 1;

    if v_job_id is null then
        return;
    end if;

    return query
    update public.relay_jobs
       set status = 'leased',
           lease_owner = p_worker_id,
           lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           heartbeat_at = now(),
           attempt_count = attempt_count + 1,
           lease_fence = lease_fence + 1
     where id = v_job_id
     returning *;
end;
$$;

revoke all on function public.claim_relay_job_core(text, integer)
    from public, anon, authenticated;
grant execute on function public.claim_relay_job_core(text, integer)
    to service_role;

-- Legacy Fusion worker claim. The Core worker never sees these rows.
create or replace function public.claim_relay_job_fusion_legacy(
    p_worker_id text,
    p_lease_seconds integer default 120
)
returns setof public.relay_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job_id uuid;
begin
    select j.id
      into v_job_id
      from public.relay_jobs j
     where j.expires_at > now()
       and j.execution_engine = 'fusion-legacy-v1'
       and (
            j.status = 'queued'
            or (
                j.status in ('leased', 'running')
                and j.lease_expires_at is not null
                and j.lease_expires_at < now()
            )
       )
     order by j.created_at asc
     for update skip locked
     limit 1;

    if v_job_id is null then
        return;
    end if;

    return query
    update public.relay_jobs
       set status = 'leased',
           lease_owner = p_worker_id,
           lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           heartbeat_at = now(),
           attempt_count = attempt_count + 1,
           lease_fence = lease_fence + 1
     where id = v_job_id
     returning *;
end;
$$;

revoke all on function public.claim_relay_job_fusion_legacy(text, integer)
    from public, anon, authenticated;
grant execute on function public.claim_relay_job_fusion_legacy(text, integer)
    to service_role;

create or replace function public.renew_relay_job_lease_v2(
    p_job_id uuid,
    p_worker_id text,
    p_lease_fence bigint,
    p_lease_seconds integer default 120
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.relay_jobs
       set lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           heartbeat_at = now()
     where id = p_job_id
       and lease_owner = p_worker_id
       and lease_fence = p_lease_fence
       and status in ('leased', 'running');
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.renew_relay_job_lease_v2(uuid, text, bigint, integer)
    from public, anon, authenticated;
grant execute on function public.renew_relay_job_lease_v2(uuid, text, bigint, integer)
    to service_role;

-- Reserve the single append slot before a job becomes queued. Idempotent replay
-- of the same job_id can reacquire its own slot.
create or replace function public.reserve_relay_session_job(
    p_session_id uuid,
    p_job_id uuid,
    p_expected_history_version bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.relay_sessions
       set active_job_id = p_job_id,
           updated_at = now()
     where id = p_session_id
       and history_mode = 'append'
       and history_version = p_expected_history_version
       and (active_job_id is null or active_job_id = p_job_id)
       and expires_at > now();
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.reserve_relay_session_job(uuid, uuid, bigint)
    from public, anon, authenticated;
grant execute on function public.reserve_relay_session_job(uuid, uuid, bigint)
    to service_role;

create or replace function public.release_relay_session_job(
    p_session_id uuid,
    p_job_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.relay_sessions
       set active_job_id = null,
           updated_at = now()
     where id = p_session_id
       and active_job_id = p_job_id;
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.release_relay_session_job(uuid, uuid)
    from public, anon, authenticated;
grant execute on function public.release_relay_session_job(uuid, uuid)
    to service_role;

-- Close session history and job success in one database transaction. All guards
-- are checked before either row is mutated.
create or replace function public.commit_relay_session_and_job_success(
    p_session_id uuid,
    p_job_id uuid,
    p_expected_history_version bigint,
    p_history_object_path text,
    p_provider text,
    p_model text,
    p_lease_owner text,
    p_lease_fence bigint,
    p_raw_response_object_path text,
    p_response_output_object_path text,
    p_compact_result jsonb,
    p_provider_response_id text default null
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_history_version bigint;
    v_active_job_id uuid;
    v_session_provider text;
    v_job_status text;
    v_job_lease_owner text;
    v_job_lease_fence bigint;
begin
    select s.history_version, s.active_job_id, s.provider
      into v_history_version, v_active_job_id, v_session_provider
      from public.relay_sessions s
     where s.id = p_session_id
     for update;

    if not found
       or v_history_version <> p_expected_history_version
       or v_active_job_id is distinct from p_job_id
       or v_session_provider <> p_provider then
        return false;
    end if;

    select j.status, j.lease_owner, j.lease_fence
      into v_job_status, v_job_lease_owner, v_job_lease_fence
      from public.relay_jobs j
     where j.id = p_job_id
       and j.relay_session_id = p_session_id
     for update;

    if not found
       or v_job_status not in ('leased', 'running')
       or v_job_lease_owner is distinct from p_lease_owner
       or v_job_lease_fence <> p_lease_fence then
        return false;
    end if;

    update public.relay_sessions
       set history_object_path = p_history_object_path,
           history_version = history_version + 1,
           model = p_model,
           active_job_id = null,
           updated_at = now()
     where id = p_session_id;

    update public.relay_jobs
       set status = 'succeeded',
           raw_response_object_path = p_raw_response_object_path,
           response_output_object_path = p_response_output_object_path,
           compact_result = p_compact_result,
           provider_response_id = p_provider_response_id,
           completed_at = now(),
           heartbeat_at = now(),
           error_code = null,
           error_message = null,
           raw_error_object_path = null,
           raw_error_meta = null
     where id = p_job_id;

    return true;
end;
$$;

revoke all on function public.commit_relay_session_and_job_success(
    uuid, uuid, bigint, text, text, text, text, bigint, text, text, jsonb, text
) from public, anon, authenticated;
grant execute on function public.commit_relay_session_and_job_success(
    uuid, uuid, bigint, text, text, text, text, bigint, text, text, jsonb, text
) to service_role;

commit;
