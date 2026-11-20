-- Relay v2 migration: Session / Request / optional Job / Material.
-- Apply after 001_relay_schema.sql (and 002_fusion_runtime.sql if legacy Fusion is used).

create table if not exists public.relay_objects (
    id text primary key,
    tenant_id text not null,
    conversation_hash text not null,
    storage_id text not null,
    bucket text not null,
    object_key text not null,
    object_version text,
    sha256 text not null,
    size_bytes bigint not null,
    content_type text not null,
    created_at timestamptz not null default now()
);

create index if not exists relay_objects_owner_idx
    on public.relay_objects (tenant_id, conversation_hash, created_at desc);

alter table public.relay_sessions
    add column if not exists connection_id text,
    add column if not exists context_policy text not null default 'conversation',
    add column if not exists material_manifest jsonb not null default '[]'::jsonb,
    add column if not exists history_object_id text,
    add column if not exists active_request_id uuid,
    add column if not exists execution_pool text not null default 'railway-default',
    add column if not exists protocol_version text not null default 'v1',
    add column if not exists idempotency_key text,
    add column if not exists session_hash text,
    add column if not exists metadata jsonb not null default '{}'::jsonb;

create unique index if not exists relay_sessions_v2_idempotency_uq
    on public.relay_sessions (tenant_id, idempotency_key)
    where idempotency_key is not null;

create table if not exists public.relay_materials (
    id text primary key,
    tenant_id text not null,
    conversation_hash text not null,
    status text not null,
    idempotency_key text not null,
    filename text not null,
    content_type text not null,
    size_bytes bigint not null,
    sha256 text not null,
    object_id text not null references public.relay_objects(id),
    source_ref text,
    source_url text,
    parent_material_id text references public.relay_materials(id) on delete set null,
    ordinal integer,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    constraint relay_materials_status_check check (status in ('ready', 'failed', 'deleted'))
);

create unique index if not exists relay_materials_idempotency_uq
    on public.relay_materials (tenant_id, idempotency_key);
create index if not exists relay_materials_owner_idx
    on public.relay_materials (tenant_id, conversation_hash, created_at desc);

create table if not exists public.relay_requests (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references public.relay_sessions(id) on delete cascade,
    tenant_id text not null,
    conversation_hash text not null,
    idempotency_key text not null,
    request_hash text not null,
    execution_mode text not null,
    status text not null,
    request_object_id text not null references public.relay_objects(id),
    result_object_id text references public.relay_objects(id),
    output_object_id text references public.relay_objects(id),
    job_id uuid,
    provider text not null,
    connection_id text not null,
    model text not null,
    execution_pool text not null,
    expected_history_version bigint not null,
    completed_history_version bigint,
    provider_dispatch_state text not null default 'not_sent',
    provider_response_id text,
    provisional_result_object_id text references public.relay_objects(id),
    provisional_output_object_id text references public.relay_objects(id),
    provisional_history_object_id text references public.relay_objects(id),
    provisional_compact_result jsonb,
    compact_result jsonb,
    error jsonb,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    constraint relay_requests_execution_mode_check check (execution_mode in ('sync', 'async')),
    constraint relay_requests_status_check check (
        status in ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled', 'indeterminate')
    ),
    constraint relay_requests_dispatch_check check (
        provider_dispatch_state in ('not_sent', 'dispatch_started', 'result_stored', 'committed')
    )
);

create unique index if not exists relay_requests_idempotency_uq
    on public.relay_requests (session_id, idempotency_key);
create index if not exists relay_requests_owner_idx
    on public.relay_requests (tenant_id, conversation_hash, created_at desc);
create index if not exists relay_requests_session_idx
    on public.relay_requests (session_id, created_at desc);

alter table public.relay_jobs
    add column if not exists request_id uuid references public.relay_requests(id) on delete set null,
    add column if not exists execution_pool text,
    add column if not exists protocol_version text,
    add column if not exists lease_epoch bigint not null default 0;

update public.relay_jobs
   set execution_pool = coalesce(execution_pool, 'railway-default'),
       protocol_version = coalesce(protocol_version, 'v1')
 where execution_pool is null or protocol_version is null;

alter table public.relay_jobs
    alter column execution_pool set default 'railway-default',
    alter column protocol_version set default 'v1';

create unique index if not exists relay_jobs_request_uq
    on public.relay_jobs (request_id)
    where request_id is not null;
create index if not exists relay_jobs_pool_queue_idx
    on public.relay_jobs (execution_pool, status, created_at);

alter table public.relay_jobs drop constraint if exists relay_jobs_status_check;
alter table public.relay_jobs add constraint relay_jobs_status_check check (
    status in (
        'queued', 'leased', 'running', 'succeeded',
        'failed', 'cancelled', 'expired', 'indeterminate'
    )
);

create table if not exists public.provider_material_bindings (
    material_id text not null references public.relay_materials(id) on delete cascade,
    connection_id text not null,
    purpose text not null,
    representation text not null,
    adapter_version text not null,
    provider_file_id text,
    file_uri text,
    expires_at timestamptz,
    processing_state text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (material_id, connection_id, purpose, representation, adapter_version)
);

create table if not exists public.relay_execution_attempts (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references public.relay_requests(id) on delete cascade,
    job_id uuid references public.relay_jobs(id) on delete set null,
    lease_epoch bigint,
    worker_id text,
    deployment_id text,
    phase text not null,
    provider_request_id text,
    detail jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

alter table public.relay_objects enable row level security;
alter table public.relay_materials enable row level security;
alter table public.relay_requests enable row level security;
alter table public.provider_material_bindings enable row level security;
alter table public.relay_execution_attempts enable row level security;

revoke all on table public.relay_objects, public.relay_materials, public.relay_requests,
    public.provider_material_bindings, public.relay_execution_attempts from anon, authenticated;
grant select, insert, update, delete on table public.relay_objects, public.relay_materials,
    public.relay_requests, public.provider_material_bindings, public.relay_execution_attempts to service_role;

-- Accept a Request and reserve the Session in one transaction.  Async requests
-- create their optional relay_jobs row in the same transaction, closing the
-- "Request exists but Job was never created" crash window.
create or replace function public.accept_relay_request(
    p_session_id uuid,
    p_request_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_request_hash text,
    p_execution_mode text,
    p_request_object_id text,
    p_provider text,
    p_connection_id text,
    p_model text,
    p_execution_pool text,
    p_metadata jsonb,
    p_job_id uuid,
    p_job_expires_at timestamptz
)
returns setof public.relay_requests
language plpgsql
security definer
set search_path = public
as $$
declare
    v_session public.relay_sessions%rowtype;
    v_existing public.relay_requests%rowtype;
    v_request_path text;
begin
    select * into v_session
      from public.relay_sessions
     where id = p_session_id
       and tenant_id = p_tenant_id
       and conversation_hash = p_conversation_hash
       and expires_at > now()
     for update;

    if not found then
        raise exception 'SESSION_NOT_FOUND';
    end if;

    select * into v_existing
      from public.relay_requests
     where session_id = p_session_id
       and idempotency_key = p_idempotency_key;

    if found then
        if v_existing.request_hash <> p_request_hash then
            raise exception 'IDEMPOTENCY_CONFLICT';
        end if;
        return next v_existing;
        return;
    end if;

    if v_session.active_request_id is not null then
        raise exception 'SESSION_BUSY';
    end if;

    if p_execution_mode not in ('sync', 'async') then
        raise exception 'INVALID_EXECUTION_MODE';
    end if;

    insert into public.relay_requests (
        id, session_id, tenant_id, conversation_hash, idempotency_key,
        request_hash, execution_mode, status, request_object_id,
        provider, connection_id, model, execution_pool,
        expected_history_version, job_id, metadata, started_at
    ) values (
        p_request_id, p_session_id, p_tenant_id, p_conversation_hash,
        p_idempotency_key, p_request_hash, p_execution_mode,
        case when p_execution_mode = 'async' then 'queued' else 'running' end,
        p_request_object_id, p_provider, p_connection_id, p_model,
        p_execution_pool, v_session.history_version, p_job_id,
        coalesce(p_metadata, '{}'::jsonb),
        case when p_execution_mode = 'sync' then now() else null end
    );

    if p_execution_mode = 'async' then
        if p_job_id is null then
            raise exception 'ASYNC_JOB_ID_REQUIRED';
        end if;
        select object_key into v_request_path from public.relay_objects where id = p_request_object_id;
        insert into public.relay_jobs (
            id, tenant_id, conversation_hash, relay_session_id, stage,
            provider, status, model, think_level, request_object_path,
            compact_result, idempotency_key, attempt_count, expires_at,
            request_id, execution_pool, protocol_version, lease_epoch
        ) values (
            p_job_id, p_tenant_id, p_conversation_hash, p_session_id, 'v2_request',
            p_provider, 'queued', p_model, null, v_request_path,
            null, 'v2:' || p_session_id::text || ':' || p_idempotency_key,
            0, p_job_expires_at, p_request_id, p_execution_pool, 'v2', 0
        );
    end if;

    update public.relay_sessions
       set active_request_id = p_request_id,
           updated_at = now()
     where id = p_session_id;

    return query select * from public.relay_requests where id = p_request_id;
end;
$$;

-- Atomically publish a successful result and advance Session history/version.
-- Fencing is enforced for async requests when lease_owner/lease_epoch are given.
create or replace function public.complete_relay_request(
    p_request_id uuid,
    p_session_id uuid,
    p_expected_history_version bigint,
    p_history_object_id text,
    p_result_object_id text,
    p_output_object_id text,
    p_compact_result jsonb,
    p_provider_response_id text,
    p_lease_owner text default null,
    p_lease_epoch bigint default null
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_request public.relay_requests%rowtype;
    v_job public.relay_jobs%rowtype;
    v_next_version bigint;
begin
    select * into v_request from public.relay_requests
     where id = p_request_id and session_id = p_session_id for update;
    if not found or v_request.status in ('cancelled', 'succeeded', 'failed', 'indeterminate') then
        return false;
    end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id = v_request.job_id for update;
        if not found then return false; end if;
        if p_lease_owner is null or p_lease_epoch is null
           or v_job.lease_owner <> p_lease_owner
           or v_job.lease_epoch <> p_lease_epoch
           or v_job.status not in ('leased', 'running') then
            return false;
        end if;
    end if;

    update public.relay_sessions
       set history_object_id = coalesce(p_history_object_id, history_object_id),
           history_version = history_version + 1,
           active_request_id = null,
           updated_at = now()
     where id = p_session_id
       and active_request_id = p_request_id
       and history_version = p_expected_history_version;
    if not found then
        return false;
    end if;

    v_next_version := p_expected_history_version + 1;
    update public.relay_requests
       set status = 'succeeded',
           provider_dispatch_state = 'committed',
           result_object_id = p_result_object_id,
           output_object_id = p_output_object_id,
           compact_result = p_compact_result,
           provider_response_id = p_provider_response_id,
           completed_history_version = v_next_version,
           completed_at = now(),
           error = null
     where id = p_request_id;

    if v_request.job_id is not null then
        update public.relay_jobs
           set status = 'succeeded',
               compact_result = p_compact_result,
               completed_at = now(),
               heartbeat_at = now(),
               error_code = null,
               error_message = null
         where id = v_request.job_id;
    end if;
    return true;
end;
$$;

create or replace function public.fail_relay_request(
    p_request_id uuid,
    p_session_id uuid,
    p_status text,
    p_error jsonb,
    p_release_session boolean,
    p_lease_owner text default null,
    p_lease_epoch bigint default null
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_request public.relay_requests%rowtype;
    v_job public.relay_jobs%rowtype;
begin
    if p_status not in ('failed', 'indeterminate') then return false; end if;
    select * into v_request from public.relay_requests
     where id = p_request_id and session_id = p_session_id for update;
    if not found or v_request.status in ('succeeded', 'cancelled') then return false; end if;

    if v_request.job_id is not null and p_lease_owner is not null then
        select * into v_job from public.relay_jobs where id = v_request.job_id for update;
        if not found or p_lease_epoch is null
           or v_job.lease_owner <> p_lease_owner or v_job.lease_epoch <> p_lease_epoch then
            return false;
        end if;
    end if;

    update public.relay_requests
       set status = p_status, error = p_error, completed_at = now()
     where id = p_request_id;

    if v_request.job_id is not null then
        update public.relay_jobs
           set status = p_status, completed_at = now(), heartbeat_at = now(),
               error_code = null, error_message = null
         where id = v_request.job_id;
    end if;

    if p_release_session then
        update public.relay_sessions
           set active_request_id = null, updated_at = now()
         where id = p_session_id and active_request_id = p_request_id;
    end if;
    return true;
end;
$$;

create or replace function public.cancel_relay_request(
    p_request_id uuid,
    p_session_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job_id uuid;
begin
    update public.relay_requests
       set status = 'cancelled', completed_at = now()
     where id = p_request_id and session_id = p_session_id
       and status not in ('succeeded', 'failed', 'cancelled')
     returning job_id into v_job_id;
    if not found then return false; end if;

    if v_job_id is not null then
        update public.relay_jobs
           set status = 'cancelled', completed_at = now()
         where id = v_job_id and status not in ('succeeded', 'failed', 'cancelled', 'expired');
    end if;
    update public.relay_sessions
       set active_request_id = null, updated_at = now()
     where id = p_session_id and active_request_id = p_request_id;
    return true;
end;
$$;

-- Before claiming new work, conservatively convert expired v2 executions whose
-- provider dispatch had started but no result was durably stored to indeterminate.
-- A lease timeout is not evidence that the upstream call never happened.
create or replace function public.claim_relay_job_v2(
    p_worker_id text,
    p_execution_pools text[],
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
    update public.relay_requests r
       set status = 'indeterminate',
           error = jsonb_build_object(
               'source', 'relay_recovery',
               'exception_type', 'ExecutionIndeterminate',
               'message', 'Worker lease expired after provider dispatch started; Relay did not replay the provider call'
           ),
           completed_at = now()
      from public.relay_jobs j
     where j.request_id = r.id
       and j.status in ('leased', 'running')
       and j.lease_expires_at < now()
       and r.provider_dispatch_state = 'dispatch_started';

    update public.relay_jobs j
       set status = 'indeterminate', completed_at = now()
      from public.relay_requests r
     where j.request_id = r.id
       and j.status in ('leased', 'running')
       and j.lease_expires_at < now()
       and r.status = 'indeterminate';

    select j.id into v_job_id
      from public.relay_jobs j
      left join public.relay_requests r on r.id = j.request_id
     where j.expires_at > now()
       and j.execution_pool = any(p_execution_pools)
       and (
            j.status = 'queued'
            or (
                j.status in ('leased', 'running')
                and j.lease_expires_at is not null
                and j.lease_expires_at < now()
                and (
                    j.request_id is null
                    or r.provider_dispatch_state in ('not_sent', 'result_stored')
                )
            )
       )
     order by j.created_at asc
     for update of j skip locked
     limit 1;

    if v_job_id is null then return; end if;

    return query
    update public.relay_jobs
       set status = 'leased',
           lease_owner = p_worker_id,
           lease_epoch = lease_epoch + 1,
           lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           heartbeat_at = now(),
           attempt_count = attempt_count + 1
     where id = v_job_id
     returning *;
end;
$$;

create or replace function public.renew_relay_job_lease_v2(
    p_job_id uuid,
    p_worker_id text,
    p_lease_epoch bigint,
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
       and lease_epoch = p_lease_epoch
       and status in ('leased', 'running');
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

revoke all on function public.accept_relay_request(uuid, uuid, text, text, text, text, text, text, text, text, text, text, jsonb, uuid, timestamptz) from public, anon, authenticated;
grant execute on function public.accept_relay_request(uuid, uuid, text, text, text, text, text, text, text, text, text, text, jsonb, uuid, timestamptz) to service_role;
revoke all on function public.complete_relay_request(uuid, uuid, bigint, text, text, text, jsonb, text, text, bigint) from public, anon, authenticated;
grant execute on function public.complete_relay_request(uuid, uuid, bigint, text, text, text, jsonb, text, text, bigint) to service_role;
revoke all on function public.fail_relay_request(uuid, uuid, text, jsonb, boolean, text, bigint) from public, anon, authenticated;
grant execute on function public.fail_relay_request(uuid, uuid, text, jsonb, boolean, text, bigint) to service_role;
revoke all on function public.cancel_relay_request(uuid, uuid) from public, anon, authenticated;
grant execute on function public.cancel_relay_request(uuid, uuid) to service_role;
revoke all on function public.claim_relay_job_v2(text, text[], integer) from public, anon, authenticated;
grant execute on function public.claim_relay_job_v2(text, text[], integer) to service_role;
revoke all on function public.renew_relay_job_lease_v2(uuid, text, bigint, integer) from public, anon, authenticated;
grant execute on function public.renew_relay_job_lease_v2(uuid, text, bigint, integer) to service_role;

-- Query-time reconciliation closes the sync-process-crash window even if no
-- Worker is involved. Unknown provider execution remains indeterminate and the
-- Session stays reserved until the caller explicitly reconciles/cancels it.
create or replace function public.reconcile_relay_request(
    p_request_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_sync_timeout_seconds integer default 120
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_request public.relay_requests%rowtype;
    v_job public.relay_jobs%rowtype;
begin
    select * into v_request from public.relay_requests
     where id = p_request_id
       and tenant_id = p_tenant_id
       and conversation_hash = p_conversation_hash
     for update;
    if not found then return false; end if;

    if v_request.status = 'running'
       and v_request.execution_mode = 'sync'
       and v_request.provider_dispatch_state = 'dispatch_started'
       and v_request.started_at is not null
       and v_request.started_at < now() - make_interval(secs => greatest(p_sync_timeout_seconds, 1)) then
        update public.relay_requests
           set status = 'indeterminate',
               error = jsonb_build_object(
                   'source', 'relay_recovery',
                   'exception_type', 'ExecutionIndeterminate',
                   'message', 'Inline executor disappeared after provider dispatch started; Relay did not replay the provider call'
               ),
               completed_at = now()
         where id = p_request_id;
        return true;
    end if;

    if v_request.job_id is not null and v_request.provider_dispatch_state = 'dispatch_started' then
        select * into v_job from public.relay_jobs where id = v_request.job_id for update;
        if found and v_job.status in ('leased', 'running')
           and v_job.lease_expires_at is not null and v_job.lease_expires_at < now() then
            update public.relay_requests
               set status = 'indeterminate',
                   error = jsonb_build_object(
                       'source', 'relay_recovery',
                       'exception_type', 'ExecutionIndeterminate',
                       'message', 'Worker lease expired after provider dispatch started; Relay did not replay the provider call'
                   ),
                   completed_at = now()
             where id = p_request_id;
            update public.relay_jobs set status = 'indeterminate', completed_at = now()
             where id = v_request.job_id;
            return true;
        end if;
    end if;
    return false;
end;
$$;

revoke all on function public.reconcile_relay_request(uuid, text, text, integer) from public, anon, authenticated;
grant execute on function public.reconcile_relay_request(uuid, text, text, integer) to service_role;
