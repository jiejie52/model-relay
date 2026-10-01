-- Model Relay 3.0.0 cache execution control.
-- Apply after 003_relay_v2_session_request_material.sql and 004_provider_native_file_ingress.sql.
-- Cache state augments Request execution; it never replaces Session history,
-- Request idempotency, Material identity, or provider_dispatch_state.

alter table public.relay_requests
    add column if not exists request_identity_version text,
    add column if not exists caller_intent_hash text,
    add column if not exists context_plan_object_id text references public.relay_objects(id),
    add column if not exists context_plan_hash text,
    add column if not exists cache_plan_object_id text references public.relay_objects(id),
    add column if not exists cache_plan_hash text,
    add column if not exists active_cache_binding_version integer,
    add column if not exists cache_resolution_status text,
    add column if not exists prepared_payload_hash text,
    add column if not exists cache_usage jsonb,
    add column if not exists request_executor_id text,
    add column if not exists request_executor_epoch bigint not null default 0,
    add column if not exists request_executor_lease_expires_at timestamptz;

create index if not exists relay_requests_caller_intent_idx
    on public.relay_requests (session_id, caller_intent_hash);

create table if not exists public.relay_cache_resources (
    id uuid primary key default gen_random_uuid(),
    tenant_id text not null,
    conversation_hash text not null,
    session_id uuid not null references public.relay_sessions(id) on delete cascade,
    offering_id text,
    connection_id text not null,
    scope_hash text not null,
    profile_hash text,
    content_fingerprint text not null,
    generation integer not null,
    state text not null,
    provider_handle_ref text,
    expire_time timestamptz,
    spec_object_id text references public.relay_objects(id),
    operation_epoch bigint not null default 0,
    active_creator text,
    last_used_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint relay_cache_resources_state_check check (
        state in ('planned','creating','create_unknown','ready','renewing','retiring','delete_pending','expired','invalid','deleted','orphaned','failed')
    ),
    unique(scope_hash, content_fingerprint, generation)
);

create index if not exists relay_cache_resources_lookup_idx
    on public.relay_cache_resources (scope_hash, content_fingerprint, state, expire_time desc);
create index if not exists relay_cache_resources_owner_idx
    on public.relay_cache_resources (tenant_id, conversation_hash, session_id, created_at desc);

create table if not exists public.relay_cache_operations (
    op_id uuid primary key default gen_random_uuid(),
    resource_id uuid references public.relay_cache_resources(id) on delete set null,
    scope_hash text not null,
    content_fingerprint text not null,
    operation_type text not null,
    request_id uuid references public.relay_requests(id) on delete set null,
    idempotency_key text not null,
    lease_owner text,
    lease_epoch bigint not null default 0,
    lease_expires_at timestamptz,
    state text not null,
    attempt_no integer not null default 0,
    provider_request_id text,
    raw_result jsonb,
    error_object_id text references public.relay_objects(id),
    absolute_target_expire_time timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint relay_cache_operations_type_check check (operation_type in ('create','get','renew','delete','reconcile')),
    constraint relay_cache_operations_state_check check (state in ('planned','leased','running','succeeded','failed','unknown','cancelled')),
    unique(scope_hash, content_fingerprint, operation_type, idempotency_key)
);

create index if not exists relay_cache_operations_lease_idx
    on public.relay_cache_operations (state, lease_expires_at);

create table if not exists public.relay_request_cache_bindings (
    request_id uuid not null references public.relay_requests(id) on delete cascade,
    binding_version integer not null,
    plan_hash text not null,
    resolution_object_id text references public.relay_objects(id),
    binding_object_id text references public.relay_objects(id),
    binding_hash text not null,
    final_mechanism text,
    resource_id uuid references public.relay_cache_resources(id) on delete set null,
    resource_generation integer,
    state text not null,
    prepared_payload_hash text,
    sealed_at timestamptz,
    superseded_by integer,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    primary key (request_id, binding_version),
    constraint relay_request_cache_binding_state_check check (state in ('candidate','installed','sealed','superseded'))
);

create unique index if not exists relay_request_cache_one_sealed_uq
    on public.relay_request_cache_bindings (request_id)
    where state = 'sealed';

create table if not exists public.relay_cache_pins (
    resource_id uuid not null references public.relay_cache_resources(id) on delete cascade,
    request_id uuid not null references public.relay_requests(id) on delete cascade,
    binding_version integer not null,
    execution_fence text not null,
    status text not null,
    hold_until timestamptz,
    released_at timestamptz,
    created_at timestamptz not null default now(),
    primary key (resource_id, request_id, binding_version),
    constraint relay_cache_pins_status_check check (status in ('active','held','released'))
);

create index if not exists relay_cache_pins_active_idx
    on public.relay_cache_pins (status, hold_until);

create table if not exists public.relay_cache_tasks (
    id uuid primary key default gen_random_uuid(),
    task_type text not null,
    resource_id uuid references public.relay_cache_resources(id) on delete cascade,
    due_at timestamptz not null,
    pool text not null default 'cache-maintenance',
    status text not null default 'queued',
    lease_owner text,
    lease_epoch bigint not null default 0,
    lease_expires_at timestamptz,
    retry_budget integer not null default 5,
    target_generation integer,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint relay_cache_tasks_type_check check (task_type in ('renew','reconcile','delete')),
    constraint relay_cache_tasks_status_check check (status in ('queued','leased','running','succeeded','failed','cancelled'))
);

create index if not exists relay_cache_tasks_due_idx
    on public.relay_cache_tasks (status, due_at, pool);

alter table public.relay_cache_resources enable row level security;
alter table public.relay_cache_operations enable row level security;
alter table public.relay_request_cache_bindings enable row level security;
alter table public.relay_cache_pins enable row level security;
alter table public.relay_cache_tasks enable row level security;

revoke all on table public.relay_cache_resources, public.relay_cache_operations,
    public.relay_request_cache_bindings, public.relay_cache_pins, public.relay_cache_tasks
    from anon, authenticated;
grant select, insert, update, delete on table public.relay_cache_resources,
    public.relay_cache_operations, public.relay_request_cache_bindings,
    public.relay_cache_pins, public.relay_cache_tasks to service_role;

-- Accept a cache-aware Request. The history version used to build ContextPlan
-- must still be current when the Request is accepted; otherwise the caller must
-- rebuild the plan before any Provider-side cache side effect occurs.
create or replace function public.accept_relay_request_v3(
    p_session_id uuid,
    p_request_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_request_hash text,
    p_caller_intent_hash text,
    p_request_identity_version text,
    p_execution_mode text,
    p_request_object_id text,
    p_context_plan_object_id text,
    p_context_plan_hash text,
    p_cache_plan_object_id text,
    p_cache_plan_hash text,
    p_cache_resolution_status text,
    p_provider text,
    p_connection_id text,
    p_model text,
    p_execution_pool text,
    p_expected_history_version bigint,
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
    if not found then raise exception 'SESSION_NOT_FOUND'; end if;

    select * into v_existing
      from public.relay_requests
     where session_id = p_session_id and idempotency_key = p_idempotency_key;
    if found then
        if v_existing.request_hash <> p_request_hash then
            raise exception 'IDEMPOTENCY_CONFLICT';
        end if;
        return next v_existing;
        return;
    end if;

    if v_session.active_request_id is not null then raise exception 'SESSION_BUSY'; end if;
    if v_session.history_version <> p_expected_history_version then
        raise exception 'HISTORY_VERSION_CHANGED';
    end if;
    if p_execution_mode not in ('sync','async') then raise exception 'INVALID_EXECUTION_MODE'; end if;

    insert into public.relay_requests (
        id, session_id, tenant_id, conversation_hash, idempotency_key,
        request_hash, request_identity_version, caller_intent_hash,
        execution_mode, status, request_object_id,
        context_plan_object_id, context_plan_hash,
        cache_plan_object_id, cache_plan_hash, cache_resolution_status,
        provider, connection_id, model, execution_pool,
        expected_history_version, job_id, metadata, started_at
    ) values (
        p_request_id, p_session_id, p_tenant_id, p_conversation_hash,
        p_idempotency_key, p_request_hash, p_request_identity_version, p_caller_intent_hash,
        p_execution_mode, case when p_execution_mode='async' then 'queued' else 'running' end,
        p_request_object_id, p_context_plan_object_id, p_context_plan_hash,
        p_cache_plan_object_id, p_cache_plan_hash, p_cache_resolution_status,
        p_provider, p_connection_id, p_model, p_execution_pool,
        v_session.history_version, p_job_id, coalesce(p_metadata,'{}'::jsonb),
        case when p_execution_mode='sync' then now() else null end
    );

    if p_execution_mode = 'async' then
        if p_job_id is null then raise exception 'ASYNC_JOB_ID_REQUIRED'; end if;
        select object_key into v_request_path from public.relay_objects where id=p_request_object_id;
        insert into public.relay_jobs (
            id, tenant_id, conversation_hash, relay_session_id, stage,
            provider, status, model, think_level, request_object_path,
            compact_result, idempotency_key, attempt_count, expires_at,
            request_id, execution_pool, protocol_version, lease_epoch
        ) values (
            p_job_id, p_tenant_id, p_conversation_hash, p_session_id, 'v3_request',
            p_provider, 'queued', p_model, null, v_request_path,
            null, 'v3:'||p_session_id::text||':'||p_idempotency_key,
            0, p_job_expires_at, p_request_id, p_execution_pool, 'v3', 0
        );
    end if;

    update public.relay_sessions set active_request_id=p_request_id, updated_at=now()
     where id=p_session_id;
    return query select * from public.relay_requests where id=p_request_id;
end;
$$;

-- Sync Requests need the same stale-executor protection async Requests get from
-- relay_jobs.lease_epoch, without creating a Job.
create or replace function public.acquire_sync_request_fence(
    p_request_id uuid,
    p_executor_id text,
    p_lease_seconds integer default 120
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
    v_epoch bigint;
begin
    update public.relay_requests
       set request_executor_id = p_executor_id,
           request_executor_epoch = request_executor_epoch + 1,
           request_executor_lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds,30))
     where id = p_request_id
       and execution_mode = 'sync'
       and status = 'running'
       and provider_dispatch_state = 'not_sent'
       and (
            request_executor_id is null
            or request_executor_lease_expires_at is null
            or request_executor_lease_expires_at < now()
            or request_executor_id = p_executor_id
       )
     returning request_executor_epoch into v_epoch;
    return v_epoch;
end;
$$;

create or replace function public.renew_sync_request_fence(
    p_request_id uuid,
    p_executor_id text,
    p_executor_epoch bigint,
    p_lease_seconds integer default 120
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare v_count integer;
begin
    update public.relay_requests
       set request_executor_lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds,30))
     where id=p_request_id
       and request_executor_id=p_executor_id
       and request_executor_epoch=p_executor_epoch
       and status='running';
    get diagnostics v_count = row_count;
    return v_count=1;
end;
$$;

-- Common fence predicate is repeated inside the security-definer functions to
-- keep the critical check and write in one transaction.
create or replace function public.install_request_material_binding_v3(
    p_request_id uuid,
    p_snapshot jsonb,
    p_fence_owner text,
    p_fence_epoch bigint
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
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent' then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;
    if v_request.material_binding_snapshot is null then
        update public.relay_requests set material_binding_snapshot=p_snapshot where id=p_request_id;
    elsif v_request.material_binding_snapshot <> p_snapshot then
        return false;
    end if;
    return true;
end;
$$;

create or replace function public.install_cache_binding_v3(
    p_request_id uuid,
    p_binding_version integer,
    p_plan_hash text,
    p_binding_hash text,
    p_final_mechanism text,
    p_resource_id uuid,
    p_resource_generation integer,
    p_metadata jsonb,
    p_fence_owner text,
    p_fence_epoch bigint
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
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent' or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.cache_plan_hash is distinct from p_plan_hash then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;

    insert into public.relay_request_cache_bindings(
        request_id,binding_version,plan_hash,binding_hash,final_mechanism,
        resource_id,resource_generation,state,metadata
    ) values(
        p_request_id,p_binding_version,p_plan_hash,p_binding_hash,p_final_mechanism,
        p_resource_id,p_resource_generation,'installed',coalesce(p_metadata,'{}'::jsonb)
    ) on conflict (request_id,binding_version) do nothing;

    update public.relay_requests set active_cache_binding_version=p_binding_version where id=p_request_id;
    return true;
end;
$$;

create or replace function public.seal_cache_and_dispatch_v3(
    p_request_id uuid,
    p_binding_version integer,
    p_binding_hash text,
    p_payload_hash text,
    p_fence_owner text,
    p_fence_epoch bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_request public.relay_requests%rowtype;
    v_binding public.relay_request_cache_bindings%rowtype;
    v_job public.relay_jobs%rowtype;
begin
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent'
       or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    select * into v_binding from public.relay_request_cache_bindings
     where request_id=p_request_id and binding_version=p_binding_version for update;
    if not found or v_binding.state <> 'installed' or v_binding.binding_hash<>p_binding_hash then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;

    if v_binding.resource_id is not null then
        if not exists(select 1 from public.relay_cache_resources r
                      where r.id=v_binding.resource_id and r.state='ready'
                        and (r.expire_time is null or r.expire_time > now())) then
            return false;
        end if;
        insert into public.relay_cache_pins(resource_id,request_id,binding_version,execution_fence,status)
        values(v_binding.resource_id,p_request_id,p_binding_version,p_fence_owner||':'||p_fence_epoch::text,'active')
        on conflict (resource_id,request_id,binding_version) do update set status='active', released_at=null;
        update public.relay_cache_resources set last_used_at=now() where id=v_binding.resource_id;
    end if;

    update public.relay_request_cache_bindings
       set state='sealed', prepared_payload_hash=p_payload_hash, sealed_at=now()
     where request_id=p_request_id and binding_version=p_binding_version;
    update public.relay_requests
       set prepared_payload_hash=p_payload_hash,
           provider_dispatch_state='dispatch_started',
           started_at=coalesce(started_at,now())
     where id=p_request_id;
    return true;
end;
$$;

create or replace function public.release_cache_pins_v3(
    p_request_id uuid,
    p_hold_until timestamptz default null
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare v_count integer;
begin
    update public.relay_cache_pins
       set status=case when p_hold_until is null then 'released' else 'held' end,
           hold_until=p_hold_until,
           released_at=case when p_hold_until is null then now() else null end
     where request_id=p_request_id and status<>'released';
    get diagnostics v_count=row_count;
    return v_count;
end;
$$;

create or replace function public.claim_cache_operation_v3(
    p_request_id uuid,
    p_scope_hash text,
    p_content_fingerprint text,
    p_operation_type text,
    p_idempotency_key text,
    p_lease_owner text,
    p_lease_seconds integer default 60
)
returns setof public.relay_cache_operations
language plpgsql
security definer
set search_path = public
as $$
declare v_op_id uuid;
begin
    insert into public.relay_cache_operations(
        scope_hash,content_fingerprint,operation_type,request_id,idempotency_key,
        lease_owner,lease_epoch,lease_expires_at,state,attempt_no
    ) values(
        p_scope_hash,p_content_fingerprint,p_operation_type,p_request_id,p_idempotency_key,
        p_lease_owner,1,now()+make_interval(secs=>greatest(p_lease_seconds,15)),'leased',1
    ) on conflict(scope_hash,content_fingerprint,operation_type,idempotency_key) do nothing;

    select op_id into v_op_id from public.relay_cache_operations
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint
       and operation_type=p_operation_type and idempotency_key=p_idempotency_key for update;
    if v_op_id is null then return; end if;

    update public.relay_cache_operations
       set request_id=p_request_id,
           lease_owner=p_lease_owner,
           lease_epoch=lease_epoch+case when lease_owner is distinct from p_lease_owner or lease_expires_at<now() then 1 else 0 end,
           lease_expires_at=now()+make_interval(secs=>greatest(p_lease_seconds,15)),
           state='leased', attempt_no=attempt_no+case when state<>'leased' then 1 else 0 end,
           updated_at=now()
     where op_id=v_op_id
       and (state='planned' or (state in ('leased','running') and lease_expires_at<now()) or lease_owner=p_lease_owner);

    return query select * from public.relay_cache_operations where op_id=v_op_id and lease_owner=p_lease_owner;
end;
$$;

create or replace function public.publish_cache_resource_v3(
    p_operation_id uuid,
    p_lease_owner text,
    p_lease_epoch bigint,
    p_tenant_id text,
    p_conversation_hash text,
    p_session_id uuid,
    p_offering_id text,
    p_connection_id text,
    p_scope_hash text,
    p_profile_hash text,
    p_content_fingerprint text,
    p_provider_handle_ref text,
    p_expire_time timestamptz,
    p_raw_result jsonb
)
returns setof public.relay_cache_resources
language plpgsql
security definer
set search_path = public
as $$
declare
    v_op public.relay_cache_operations%rowtype;
    v_generation integer;
    v_resource_id uuid;
begin
    select * into v_op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or v_op.lease_owner<>p_lease_owner or v_op.lease_epoch<>p_lease_epoch
       or v_op.state not in ('leased','running') then return; end if;

    select coalesce(max(generation),0)+1 into v_generation from public.relay_cache_resources
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint;
    insert into public.relay_cache_resources(
        tenant_id,conversation_hash,session_id,offering_id,connection_id,
        scope_hash,profile_hash,content_fingerprint,generation,state,
        provider_handle_ref,expire_time,operation_epoch,active_creator,last_used_at,metadata
    ) values(
        p_tenant_id,p_conversation_hash,p_session_id,p_offering_id,p_connection_id,
        p_scope_hash,p_profile_hash,p_content_fingerprint,v_generation,'ready',
        p_provider_handle_ref,p_expire_time,p_lease_epoch,p_lease_owner,now(),
        jsonb_build_object('create_operation_id',p_operation_id)
    ) returning id into v_resource_id;
    update public.relay_cache_operations set state='succeeded',resource_id=v_resource_id,raw_result=p_raw_result,updated_at=now()
     where op_id=p_operation_id;
    return query select * from public.relay_cache_resources where id=v_resource_id;
end;
$$;


-- Persist a Provider result only if the same Request execution fence that sealed
-- dispatch still owns the Request. Raw Object Storage writes may precede this
-- function; stale executors can therefore leave collectible orphan objects but
-- cannot publish them as the Request's result.
create or replace function public.store_relay_result_v3(
    p_request_id uuid,
    p_result_object_id text,
    p_output_object_id text,
    p_history_object_id text,
    p_compact_result jsonb,
    p_provider_response_id text,
    p_cache_usage jsonb,
    p_fence_owner text,
    p_fence_epoch bigint
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
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'dispatch_started'
       or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;
    update public.relay_requests
       set provider_dispatch_state='result_stored',
           provisional_result_object_id=p_result_object_id,
           provisional_output_object_id=p_output_object_id,
           provisional_history_object_id=p_history_object_id,
           provisional_compact_result=p_compact_result,
           provider_response_id=p_provider_response_id,
           cache_usage=p_cache_usage
     where id=p_request_id;
    return true;
end;
$$;

create or replace function public.complete_relay_request_v3(
    p_request_id uuid,
    p_session_id uuid,
    p_expected_history_version bigint,
    p_history_object_id text,
    p_result_object_id text,
    p_output_object_id text,
    p_compact_result jsonb,
    p_provider_response_id text,
    p_fence_owner text,
    p_fence_epoch bigint
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
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.provider_dispatch_state <> 'result_stored'
       or v_request.status in ('cancelled','succeeded','failed','indeterminate') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;

    update public.relay_sessions
       set history_object_id=coalesce(p_history_object_id,history_object_id),
           history_version=history_version+1,
           active_request_id=null,
           updated_at=now()
     where id=p_session_id
       and active_request_id=p_request_id
       and history_version=p_expected_history_version;
    if not found then return false; end if;

    v_next_version := p_expected_history_version+1;
    update public.relay_requests
       set status='succeeded', provider_dispatch_state='committed',
           result_object_id=p_result_object_id, output_object_id=p_output_object_id,
           compact_result=p_compact_result, provider_response_id=p_provider_response_id,
           completed_history_version=v_next_version, completed_at=now(), error=null
     where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status='succeeded', compact_result=p_compact_result, completed_at=now(),
               heartbeat_at=now(), error_code=null, error_message=null
         where id=v_request.job_id;
    end if;
    update public.relay_cache_pins
       set status='released', released_at=now(), hold_until=null
     where request_id=p_request_id and status<>'released';
    return true;
end;
$$;

create or replace function public.fail_relay_request_v3(
    p_request_id uuid,
    p_session_id uuid,
    p_status text,
    p_error jsonb,
    p_release_session boolean,
    p_fence_owner text,
    p_fence_epoch bigint,
    p_hold_pins_until timestamptz default null
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
    if p_status not in ('failed','indeterminate') then return false; end if;
    select * into v_request from public.relay_requests
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.status in ('succeeded','cancelled') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch then return false; end if;
    end if;

    update public.relay_requests set status=p_status,error=p_error,completed_at=now() where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status=p_status,completed_at=now(),heartbeat_at=now(),error_code=null,error_message=null
         where id=v_request.job_id;
    end if;
    if p_release_session then
        update public.relay_sessions set active_request_id=null,updated_at=now()
         where id=p_session_id and active_request_id=p_request_id;
    end if;
    update public.relay_cache_pins
       set status=case when p_hold_pins_until is null then 'released' else 'held' end,
           hold_until=p_hold_pins_until,
           released_at=case when p_hold_pins_until is null then now() else null end
     where request_id=p_request_id and status<>'released';
    return true;
end;
$$;

revoke all on function public.store_relay_result_v3(uuid,text,text,text,jsonb,text,jsonb,text,bigint) from public, anon, authenticated;
grant execute on function public.store_relay_result_v3(uuid,text,text,text,jsonb,text,jsonb,text,bigint) to service_role;
revoke all on function public.complete_relay_request_v3(uuid,uuid,bigint,text,text,text,jsonb,text,text,bigint) from public, anon, authenticated;
grant execute on function public.complete_relay_request_v3(uuid,uuid,bigint,text,text,text,jsonb,text,text,bigint) to service_role;
revoke all on function public.fail_relay_request_v3(uuid,uuid,text,jsonb,boolean,text,bigint,timestamptz) from public, anon, authenticated;
grant execute on function public.fail_relay_request_v3(uuid,uuid,text,jsonb,boolean,text,bigint,timestamptz) to service_role;

revoke all on function public.accept_relay_request_v3(uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,bigint,jsonb,uuid,timestamptz) from public, anon, authenticated;
grant execute on function public.accept_relay_request_v3(uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,bigint,jsonb,uuid,timestamptz) to service_role;
revoke all on function public.acquire_sync_request_fence(uuid,text,integer) from public, anon, authenticated;
grant execute on function public.acquire_sync_request_fence(uuid,text,integer) to service_role;
revoke all on function public.renew_sync_request_fence(uuid,text,bigint,integer) from public, anon, authenticated;
grant execute on function public.renew_sync_request_fence(uuid,text,bigint,integer) to service_role;
revoke all on function public.install_request_material_binding_v3(uuid,jsonb,text,bigint) from public, anon, authenticated;
grant execute on function public.install_request_material_binding_v3(uuid,jsonb,text,bigint) to service_role;
revoke all on function public.install_cache_binding_v3(uuid,integer,text,text,text,uuid,integer,jsonb,text,bigint) from public, anon, authenticated;
grant execute on function public.install_cache_binding_v3(uuid,integer,text,text,text,uuid,integer,jsonb,text,bigint) to service_role;
revoke all on function public.seal_cache_and_dispatch_v3(uuid,integer,text,text,text,bigint) from public, anon, authenticated;
grant execute on function public.seal_cache_and_dispatch_v3(uuid,integer,text,text,text,bigint) to service_role;
revoke all on function public.release_cache_pins_v3(uuid,timestamptz) from public, anon, authenticated;
grant execute on function public.release_cache_pins_v3(uuid,timestamptz) to service_role;
revoke all on function public.claim_cache_operation_v3(uuid,text,text,text,text,text,integer) from public, anon, authenticated;
grant execute on function public.claim_cache_operation_v3(uuid,text,text,text,text,text,integer) to service_role;
revoke all on function public.publish_cache_resource_v3(uuid,text,bigint,text,text,uuid,text,text,text,text,text,text,timestamptz,jsonb) from public, anon, authenticated;
grant execute on function public.publish_cache_resource_v3(uuid,text,bigint,text,text,uuid,text,text,text,text,text,text,timestamptz,jsonb) to service_role;
