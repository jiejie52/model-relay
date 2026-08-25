-- Model Relay schema for Supabase Postgres.
-- Run this once in Supabase Dashboard -> SQL Editor.

create extension if not exists pgcrypto;

create table if not exists public.relay_sessions (
    id uuid primary key default gen_random_uuid(),
    tenant_id text not null,
    conversation_hash text not null,
    provider text not null,
    model text not null,
    prompt_cache_key text,
    material_prefix_object_path text,
    history_object_path text,
    signed_url_expires_at timestamptz,
    history_version bigint not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null
);

create table if not exists public.relay_jobs (
    id uuid primary key default gen_random_uuid(),
    tenant_id text not null,
    conversation_hash text not null,
    relay_session_id uuid references public.relay_sessions(id) on delete set null,
    stage text not null,
    provider text not null,
    status text not null,
    model text not null,
    think_level text,
    request_object_path text not null,
    raw_response_object_path text,
    response_output_object_path text,
    compact_result jsonb,
    idempotency_key text not null,
    lease_owner text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempt_count integer not null default 0,
    error_code text,
    error_message text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    expires_at timestamptz not null,
    constraint relay_jobs_status_check check (
        status in (
            'queued', 'leased', 'running', 'succeeded',
            'failed', 'cancelled', 'expired'
        )
    )
);

create unique index if not exists relay_jobs_idempotency_uq
    on public.relay_jobs (tenant_id, idempotency_key);

create index if not exists relay_jobs_queue_idx
    on public.relay_jobs (status, created_at);

create index if not exists relay_jobs_lease_idx
    on public.relay_jobs (status, lease_expires_at);

create index if not exists relay_jobs_session_idx
    on public.relay_jobs (relay_session_id);

create index if not exists relay_sessions_conversation_idx
    on public.relay_sessions (tenant_id, conversation_hash, created_at desc);

alter table public.relay_jobs enable row level security;
alter table public.relay_sessions enable row level security;

revoke all on table public.relay_jobs from anon, authenticated;
revoke all on table public.relay_sessions from anon, authenticated;
grant select, insert, update, delete on table public.relay_jobs to service_role;
grant select, insert, update, delete on table public.relay_sessions to service_role;

-- Atomically claim one available job. FOR UPDATE SKIP LOCKED allows multiple
-- Railway worker replicas without claiming the same row at the same time.
create or replace function public.claim_relay_job(
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
           attempt_count = attempt_count + 1
     where id = v_job_id
     returning *;
end;
$$;

revoke all on function public.claim_relay_job(text, integer) from public, anon, authenticated;
grant execute on function public.claim_relay_job(text, integer) to service_role;

create or replace function public.renew_relay_job_lease(
    p_job_id uuid,
    p_worker_id text,
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
       and status in ('leased', 'running');

    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.renew_relay_job_lease(uuid, text, integer) from public, anon, authenticated;
grant execute on function public.renew_relay_job_lease(uuid, text, integer) to service_role;

-- Optimistic session-history commit. A second simultaneous continuation cannot
-- silently overwrite history generated from an older version.
create or replace function public.commit_relay_session_history(
    p_session_id uuid,
    p_expected_history_version bigint,
    p_history_object_path text,
    p_provider text,
    p_model text
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
       set history_object_path = p_history_object_path,
           history_version = history_version + 1,
           provider = p_provider,
           model = p_model,
           updated_at = now()
     where id = p_session_id
       and history_version = p_expected_history_version;

    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.commit_relay_session_history(uuid, bigint, text, text, text)
    from public, anon, authenticated;
grant execute on function public.commit_relay_session_history(uuid, bigint, text, text, text)
    to service_role;
