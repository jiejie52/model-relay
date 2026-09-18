-- Model Relay V2 additive migration.
-- Run AFTER 001_relay_schema.sql (and 002_fusion_runtime.sql when Fusion compatibility is used).
-- The migration intentionally preserves all legacy tables/IDs and makes legacy/V2
-- worker claiming mutually exclusive by execution_engine.

create extension if not exists pgcrypto;

-- ----------------------------- generic V2 data -----------------------------

create table if not exists public.relay_errors (
    id text primary key,
    tenant_id text not null,
    conversation_hash text not null,
    origin text not null,
    provider text,
    service text,
    http_status integer,
    response_headers_object_path text,
    body_object_path text,
    body_encoding text,
    byte_length bigint not null default 0,
    sha256 text not null,
    content_type text,
    content_encoding text,
    received_complete boolean not null default false,
    archive_state text not null,
    exception jsonb,
    secondary_error jsonb,
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    constraint relay_errors_archive_state_check check (
        archive_state in ('durable', 'spooled', 'unavailable', 'legacy_incomplete')
    )
);

create index if not exists relay_errors_owner_idx
    on public.relay_errors (tenant_id, conversation_hash, created_at desc);

create table if not exists public.relay_materials (
    id text primary key,
    tenant_id text not null,
    conversation_hash text not null,
    filename text not null,
    declared_mime text,
    detected_mime text,
    byte_length bigint,
    sha256 text,
    expected_sha256 text,
    expected_size bigint,
    storage_backend text not null default 'railway_s3',
    object_key text,
    status text not null default 'processing',
    phase text not null default 'awaiting_upload',
    generation integer not null default 1,
    last_error_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null,
    deleted_at timestamptz,
    constraint relay_materials_status_check check (
        status in ('processing', 'ready', 'failed', 'deleting', 'deleted')
    )
);

create index if not exists relay_materials_owner_idx
    on public.relay_materials (tenant_id, conversation_hash, created_at desc);
create index if not exists relay_materials_gc_idx
    on public.relay_materials (status, expires_at);

create table if not exists public.relay_material_ingestions (
    id text primary key,
    material_id text not null references public.relay_materials(id) on delete cascade,
    tenant_id text not null,
    conversation_hash text not null,
    generation integer not null,
    status text not null default 'processing',
    phase text not null default 'awaiting_upload',
    source_identity jsonb not null default '{}'::jsonb,
    staging_key text,
    canonical_candidate_key text,
    lease_owner text,
    lease_token uuid,
    last_error_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    constraint relay_material_ingestions_status_check check (
        status in ('processing', 'ready', 'failed', 'cancelled')
    )
);

alter table public.relay_material_ingestions add column if not exists lease_expires_at timestamptz;

create index if not exists relay_material_ingestions_material_idx
    on public.relay_material_ingestions (material_id, generation desc);

create table if not exists public.relay_session_materials (
    session_id uuid not null references public.relay_sessions(id) on delete cascade,
    material_id text not null references public.relay_materials(id),
    ordinal integer not null,
    created_at timestamptz not null default now(),
    primary key (session_id, material_id),
    unique (session_id, ordinal)
);

create index if not exists relay_session_materials_material_idx
    on public.relay_session_materials (material_id, session_id);

create table if not exists public.relay_material_bindings (
    id uuid primary key default gen_random_uuid(),
    tenant_id text not null,
    conversation_hash text not null,
    material_id text not null references public.relay_materials(id) on delete cascade,
    provider text not null,
    upstream_profile text not null,
    account_scope text not null,
    protocol text not null,
    capability_group text,
    purpose text not null,
    transform_version text not null,
    native_file_id text,
    native_uri text,
    derived_object_path text,
    status text not null default 'ready',
    expires_at timestamptz,
    generation integer not null default 1,
    transport_epoch integer not null default 0,
    wire_request_hash text,
    cache_identity text,
    lease_owner text,
    lease_token uuid,
    last_error_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (material_id, provider, upstream_profile, account_scope, purpose, transform_version)
);

create table if not exists public.relay_request_keys (
    tenant_id text not null,
    conversation_hash text not null,
    operation_scope text not null,
    idempotency_key text not null,
    request_fingerprint text not null,
    resource_type text not null,
    resource_id text not null,
    created_at timestamptz not null default now(),
    primary key (tenant_id, conversation_hash, operation_scope, idempotency_key)
);

-- --------------------------- session/job increments ------------------------

alter table public.relay_sessions add column if not exists upstream_profile text;
alter table public.relay_sessions add column if not exists account_scope text;
alter table public.relay_sessions add column if not exists protocol text;
alter table public.relay_sessions add column if not exists history_codec text;
alter table public.relay_sessions add column if not exists history_codec_version text;
alter table public.relay_sessions add column if not exists history_mode text default 'append';
alter table public.relay_sessions add column if not exists context_object_path text;
alter table public.relay_sessions add column if not exists context_hash text;
alter table public.relay_sessions add column if not exists material_set_hash text;
alter table public.relay_sessions add column if not exists active_job_id uuid;
alter table public.relay_sessions add column if not exists schema_version text default 'legacy';
alter table public.relay_sessions add column if not exists capability_profile_version text;

alter table public.relay_jobs add column if not exists execution_engine text not null default 'legacy';
alter table public.relay_jobs add column if not exists execution_phase text not null default 'queued';
alter table public.relay_jobs add column if not exists lease_token uuid;
alter table public.relay_jobs add column if not exists delivery_status text not null default 'not_sent';
alter table public.relay_jobs add column if not exists request_fingerprint text;
alter table public.relay_jobs add column if not exists wire_request_hash text;
alter table public.relay_jobs add column if not exists capability_version text;
alter table public.relay_jobs add column if not exists expected_history_version bigint;
alter table public.relay_jobs add column if not exists error_id text;
alter table public.relay_jobs add column if not exists schema_version text default 'legacy';

create index if not exists relay_jobs_engine_queue_idx
    on public.relay_jobs (execution_engine, status, created_at);
create index if not exists relay_jobs_engine_lease_idx
    on public.relay_jobs (execution_engine, status, lease_expires_at);

-- -------------------------- permissions / RLS ------------------------------

alter table public.relay_errors enable row level security;
alter table public.relay_materials enable row level security;
alter table public.relay_material_ingestions enable row level security;
alter table public.relay_session_materials enable row level security;
alter table public.relay_material_bindings enable row level security;
alter table public.relay_request_keys enable row level security;

revoke all on table public.relay_errors from anon, authenticated;
revoke all on table public.relay_materials from anon, authenticated;
revoke all on table public.relay_material_ingestions from anon, authenticated;
revoke all on table public.relay_session_materials from anon, authenticated;
revoke all on table public.relay_material_bindings from anon, authenticated;
revoke all on table public.relay_request_keys from anon, authenticated;

grant select, insert, update, delete on table public.relay_errors to service_role;
grant select, insert, update, delete on table public.relay_materials to service_role;
grant select, insert, update, delete on table public.relay_material_ingestions to service_role;
grant select, insert, update, delete on table public.relay_session_materials to service_role;
grant select, insert, update, delete on table public.relay_material_bindings to service_role;
grant select, insert, update, delete on table public.relay_request_keys to service_role;

-- ----------------------- legacy/V2 claim separation ------------------------

-- IMPORTANT: after this migration, the legacy worker can only claim legacy jobs.
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
     where j.execution_engine = 'legacy'
       and j.expires_at > now()
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

create or replace function public.claim_relay_job_v2(
    p_worker_id text,
    p_engine text default 'v2',
    p_lease_seconds integer default 120
)
returns setof public.relay_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job_id uuid;
    v_token uuid := gen_random_uuid();
begin
    select j.id
      into v_job_id
      from public.relay_jobs j
     where j.execution_engine = p_engine
       and j.expires_at > now()
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
           lease_token = v_token,
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
    p_lease_token uuid,
    p_engine text default 'v2',
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
       and execution_engine = p_engine
       and lease_owner = p_worker_id
       and lease_token = p_lease_token
       and status in ('leased', 'running');
    get diagnostics v_count = row_count;
    return v_count = 1;
end;
$$;

-- -------------------------- Material Ingress RPCs --------------------------

create or replace function public.reserve_relay_material_v2(
    p_material_id text,
    p_ingestion_id text,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_request_fingerprint text,
    p_filename text,
    p_declared_mime text,
    p_expected_sha256 text,
    p_expected_size bigint,
    p_expires_at timestamptz,
    p_source_identity jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_key public.relay_request_keys%rowtype;
    v_material public.relay_materials%rowtype;
begin
    select * into v_key
      from public.relay_request_keys
     where tenant_id = p_tenant_id
       and conversation_hash = p_conversation_hash
       and operation_scope = 'material:create'
       and idempotency_key = p_idempotency_key;

    if found then
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_material from public.relay_materials where id = v_key.resource_id;
        return jsonb_build_object('outcome','reused','material',to_jsonb(v_material));
    end if;

    begin
        insert into public.relay_request_keys(
            tenant_id, conversation_hash, operation_scope, idempotency_key,
            request_fingerprint, resource_type, resource_id
        ) values (
            p_tenant_id, p_conversation_hash, 'material:create', p_idempotency_key,
            p_request_fingerprint, 'material', p_material_id
        );
    exception when unique_violation then
        select * into v_key
          from public.relay_request_keys
         where tenant_id = p_tenant_id
           and conversation_hash = p_conversation_hash
           and operation_scope = 'material:create'
           and idempotency_key = p_idempotency_key;
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_material from public.relay_materials where id = v_key.resource_id;
        return jsonb_build_object('outcome','reused','material',to_jsonb(v_material));
    end;

    insert into public.relay_materials(
        id, tenant_id, conversation_hash, filename, declared_mime,
        expected_sha256, expected_size, status, phase, generation, expires_at
    ) values (
        p_material_id, p_tenant_id, p_conversation_hash, p_filename, p_declared_mime,
        p_expected_sha256, p_expected_size, 'processing', 'awaiting_upload', 1, p_expires_at
    ) returning * into v_material;

    insert into public.relay_material_ingestions(
        id, material_id, tenant_id, conversation_hash, generation, status, phase, source_identity
    ) values (
        p_ingestion_id, p_material_id, p_tenant_id, p_conversation_hash, 1,
        'processing', 'awaiting_upload', coalesce(p_source_identity, '{}'::jsonb)
    );

    return jsonb_build_object('outcome','created','material',to_jsonb(v_material),'ingestion_id',p_ingestion_id);
end;
$$;

create or replace function public.publish_relay_material_ready_v2(
    p_material_id text,
    p_ingestion_id text,
    p_tenant_id text,
    p_conversation_hash text,
    p_generation integer,
    p_object_key text,
    p_sha256 text,
    p_byte_length bigint,
    p_detected_mime text,
    p_lease_token uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_material public.relay_materials%rowtype;
    v_ingestion public.relay_material_ingestions%rowtype;
begin
    select * into v_material
      from public.relay_materials
     where id = p_material_id
       and tenant_id = p_tenant_id
       and conversation_hash = p_conversation_hash
     for update;
    if not found then
        return jsonb_build_object('outcome','not_found');
    end if;
    if v_material.status = 'ready' then
        return jsonb_build_object('outcome','reused','material',to_jsonb(v_material));
    end if;
    if v_material.status <> 'processing' or v_material.generation <> p_generation then
        return jsonb_build_object('outcome','state_conflict','material',to_jsonb(v_material));
    end if;

    select * into v_ingestion from public.relay_material_ingestions
     where id=p_ingestion_id and material_id=p_material_id
       and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and generation=p_generation
     for update;
    if not found then return jsonb_build_object('outcome','ingestion_not_found'); end if;
    if v_ingestion.lease_token is not null then
        if p_lease_token is null or v_ingestion.lease_token <> p_lease_token
           or (v_ingestion.lease_expires_at is not null and v_ingestion.lease_expires_at < now()) then
            return jsonb_build_object('outcome','lease_lost');
        end if;
    end if;

    if v_material.expected_sha256 is not null and lower(v_material.expected_sha256) <> lower(p_sha256) then
        update public.relay_materials set status='failed', phase='verifying', updated_at=now()
         where id=p_material_id;
        update public.relay_material_ingestions set status='failed', phase='verifying', completed_at=now(), updated_at=now(),
               lease_owner=null, lease_token=null, lease_expires_at=null
         where id=p_ingestion_id;
        return jsonb_build_object('outcome','integrity_mismatch','field','sha256');
    end if;
    if v_material.expected_size is not null and v_material.expected_size <> p_byte_length then
        update public.relay_materials set status='failed', phase='verifying', updated_at=now()
         where id=p_material_id;
        update public.relay_material_ingestions set status='failed', phase='verifying', completed_at=now(), updated_at=now(),
               lease_owner=null, lease_token=null, lease_expires_at=null
         where id=p_ingestion_id;
        return jsonb_build_object('outcome','integrity_mismatch','field','size');
    end if;

    update public.relay_materials
       set object_key=p_object_key,
           sha256=p_sha256,
           byte_length=p_byte_length,
           detected_mime=p_detected_mime,
           status='ready',
           phase='ready',
           updated_at=now()
     where id=p_material_id
     returning * into v_material;

    update public.relay_material_ingestions
       set status='ready', phase='ready', canonical_candidate_key=p_object_key,
           completed_at=now(), updated_at=now(), lease_owner=null, lease_token=null, lease_expires_at=null
     where id=p_ingestion_id and material_id=p_material_id;

    return jsonb_build_object('outcome','ready','material',to_jsonb(v_material));
end;
$$;

create or replace function public.fail_relay_material_ingestion_v2(
    p_material_id text,
    p_ingestion_id text,
    p_tenant_id text,
    p_conversation_hash text,
    p_error_id text,
    p_phase text,
    p_lease_token uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_ingestion public.relay_material_ingestions%rowtype;
begin
    select * into v_ingestion from public.relay_material_ingestions
     where id=p_ingestion_id and material_id=p_material_id
       and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
     for update;
    if not found then return jsonb_build_object('outcome','not_found'); end if;
    if v_ingestion.lease_token is not null then
        if p_lease_token is null or v_ingestion.lease_token <> p_lease_token
           or (v_ingestion.lease_expires_at is not null and v_ingestion.lease_expires_at < now()) then
            return jsonb_build_object('outcome','lease_lost');
        end if;
    end if;
    update public.relay_material_ingestions
       set status='failed', phase=p_phase, last_error_id=p_error_id, completed_at=now(), updated_at=now(),
           lease_owner=null, lease_token=null, lease_expires_at=null
     where id=p_ingestion_id;
    update public.relay_materials
       set status='failed', phase=p_phase, last_error_id=p_error_id, updated_at=now()
     where id=p_material_id and status='processing' and generation=v_ingestion.generation;
    return jsonb_build_object('outcome','failed');
end;
$$;

-- --------------------------- Session / Job RPCs ----------------------------

create or replace function public.create_relay_session_v2(
    p_session_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_request_fingerprint text,
    p_provider text,
    p_upstream_profile text,
    p_account_scope text,
    p_protocol text,
    p_history_codec text,
    p_history_codec_version text,
    p_history_mode text,
    p_model text,
    p_context_object_path text,
    p_context_hash text,
    p_material_set_hash text,
    p_material_ids text[],
    p_capability_profile_version text,
    p_expires_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_key public.relay_request_keys%rowtype;
    v_session public.relay_sessions%rowtype;
    v_expected integer := coalesce(array_length(p_material_ids,1),0);
    v_ready integer;
begin
    select * into v_key
      from public.relay_request_keys
     where tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and operation_scope='session:create' and idempotency_key=p_idempotency_key;
    if found then
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_session from public.relay_sessions where id=v_key.resource_id::uuid;
        return jsonb_build_object('outcome','reused','session',to_jsonb(v_session));
    end if;

    if v_expected > 0 then
        select count(*) into v_ready
          from public.relay_materials
         where id = any(p_material_ids)
           and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
           and status='ready' and expires_at > now();
        if v_ready <> v_expected then
            return jsonb_build_object('outcome','material_not_ready');
        end if;
    end if;

    begin
        insert into public.relay_request_keys(
            tenant_id, conversation_hash, operation_scope, idempotency_key,
            request_fingerprint, resource_type, resource_id
        ) values (
            p_tenant_id,p_conversation_hash,'session:create',p_idempotency_key,
            p_request_fingerprint,'session',p_session_id::text
        );
    exception when unique_violation then
        select * into v_key
          from public.relay_request_keys
         where tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
           and operation_scope='session:create' and idempotency_key=p_idempotency_key;
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_session from public.relay_sessions where id=v_key.resource_id::uuid;
        return jsonb_build_object('outcome','reused','session',to_jsonb(v_session));
    end;

    insert into public.relay_sessions(
        id, tenant_id, conversation_hash, provider, model, prompt_cache_key,
        material_prefix_object_path, history_object_path, signed_url_expires_at,
        history_version, expires_at, upstream_profile, account_scope, protocol,
        history_codec, history_codec_version, history_mode, context_object_path,
        context_hash, material_set_hash, active_job_id, schema_version,
        capability_profile_version
    ) values (
        p_session_id,p_tenant_id,p_conversation_hash,p_provider,p_model,null,
        null,null,null,0,p_expires_at,p_upstream_profile,p_account_scope,p_protocol,
        p_history_codec,p_history_codec_version,p_history_mode,p_context_object_path,
        p_context_hash,p_material_set_hash,null,'relay-session/2.0',p_capability_profile_version
    ) returning * into v_session;

    if v_expected > 0 then
        insert into public.relay_session_materials(session_id,material_id,ordinal)
        select p_session_id, x.material_id, x.ordinality::integer - 1
          from unnest(p_material_ids) with ordinality as x(material_id, ordinality);
    end if;

    return jsonb_build_object('outcome','created','session',to_jsonb(v_session));
end;
$$;

create or replace function public.submit_relay_session_job_v2(
    p_job_id uuid,
    p_session_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_job_idempotency_key text,
    p_request_fingerprint text,
    p_request_object_path text,
    p_provider text,
    p_model text,
    p_expected_history_version bigint,
    p_capability_version text,
    p_expires_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_scope text := 'session_job:' || p_session_id::text;
    v_key public.relay_request_keys%rowtype;
    v_session public.relay_sessions%rowtype;
    v_job public.relay_jobs%rowtype;
    v_active public.relay_jobs%rowtype;
begin
    -- Idempotency check is deliberately first: a retry of an accepted request
    -- returns its original resource even if the session version has since advanced.
    select * into v_key
      from public.relay_request_keys
     where tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and operation_scope=v_scope and idempotency_key=p_idempotency_key;
    if found then
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_job from public.relay_jobs where id=v_key.resource_id::uuid;
        return jsonb_build_object('outcome','reused','job',to_jsonb(v_job));
    end if;

    select * into v_session
      from public.relay_sessions
     where id=p_session_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
     for update;
    if not found then
        return jsonb_build_object('outcome','session_not_found');
    end if;
    if v_session.schema_version <> 'relay-session/2.0' then
        return jsonb_build_object('outcome','session_legacy');
    end if;
    if v_session.provider <> p_provider or v_session.upstream_profile is null then
        return jsonb_build_object('outcome','provider_conflict');
    end if;
    if v_session.expires_at <= now() then
        return jsonb_build_object('outcome','session_expired');
    end if;
    if v_session.history_version <> p_expected_history_version then
        return jsonb_build_object('outcome','history_conflict','history_version',v_session.history_version);
    end if;

    if v_session.active_job_id is not null then
        select * into v_active from public.relay_jobs where id=v_session.active_job_id;
        if found and v_active.status not in ('succeeded','failed','cancelled','expired') then
            return jsonb_build_object('outcome','session_busy','active_job_id',v_session.active_job_id);
        end if;
        update public.relay_sessions set active_job_id=null where id=p_session_id;
    end if;

    begin
        insert into public.relay_request_keys(
            tenant_id, conversation_hash, operation_scope, idempotency_key,
            request_fingerprint, resource_type, resource_id
        ) values (
            p_tenant_id,p_conversation_hash,v_scope,p_idempotency_key,
            p_request_fingerprint,'job',p_job_id::text
        );
    exception when unique_violation then
        select * into v_key
          from public.relay_request_keys
         where tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
           and operation_scope=v_scope and idempotency_key=p_idempotency_key;
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict','resource_id',v_key.resource_id);
        end if;
        select * into v_job from public.relay_jobs where id=v_key.resource_id::uuid;
        return jsonb_build_object('outcome','reused','job',to_jsonb(v_job));
    end;

    insert into public.relay_jobs(
        id,tenant_id,conversation_hash,relay_session_id,stage,provider,status,model,
        think_level,request_object_path,compact_result,idempotency_key,attempt_count,
        expires_at,execution_engine,execution_phase,delivery_status,request_fingerprint,
        capability_version,expected_history_version,schema_version
    ) values (
        p_job_id,p_tenant_id,p_conversation_hash,p_session_id,'inference',p_provider,'queued',p_model,
        null,p_request_object_path,null,p_job_idempotency_key,0,p_expires_at,
        'v2','queued','not_sent',p_request_fingerprint,p_capability_version,
        p_expected_history_version,'relay-job/2.0'
    ) returning * into v_job;

    update public.relay_sessions set active_job_id=p_job_id, updated_at=now() where id=p_session_id;
    return jsonb_build_object('outcome','created','job',to_jsonb(v_job));
end;
$$;

create or replace function public.commit_relay_job_result_v2(
    p_job_id uuid,
    p_session_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_worker_id text,
    p_lease_token uuid,
    p_engine text,
    p_expected_history_version bigint,
    p_history_object_path text,
    p_raw_response_object_path text,
    p_response_output_object_path text,
    p_compact_result jsonb,
    p_wire_request_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job public.relay_jobs%rowtype;
    v_session public.relay_sessions%rowtype;
begin
    select * into v_job from public.relay_jobs
     where id=p_job_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and execution_engine=p_engine and lease_owner=p_worker_id and lease_token=p_lease_token
     for update;
    if not found then
        return jsonb_build_object('outcome','lease_lost');
    end if;
    if v_job.status='cancelled' then
        return jsonb_build_object('outcome','cancelled');
    end if;

    select * into v_session from public.relay_sessions
     where id=p_session_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
     for update;
    if not found then
        return jsonb_build_object('outcome','session_not_found');
    end if;
    if v_session.active_job_id is distinct from p_job_id then
        return jsonb_build_object('outcome','session_fence_lost');
    end if;
    if v_session.history_version <> p_expected_history_version then
        return jsonb_build_object('outcome','history_conflict');
    end if;

    if p_history_object_path is not null then
        update public.relay_sessions
           set history_object_path=p_history_object_path,
               history_version=history_version+1,
               model=v_job.model,
               active_job_id=null,
               updated_at=now()
         where id=p_session_id;
    else
        update public.relay_sessions
           set model=v_job.model, active_job_id=null, updated_at=now()
         where id=p_session_id;
    end if;

    update public.relay_jobs
       set status='succeeded', execution_phase='committed', delivery_status='committed',
           raw_response_object_path=p_raw_response_object_path,
           response_output_object_path=p_response_output_object_path,
           compact_result=p_compact_result, wire_request_hash=p_wire_request_hash,
           completed_at=now(), heartbeat_at=now(), error_code=null, error_message=null,
           error_id=null
     where id=p_job_id
     returning * into v_job;
    return jsonb_build_object('outcome','committed','job',to_jsonb(v_job));
end;
$$;

create or replace function public.record_relay_job_failure_v2(
    p_job_id uuid,
    p_session_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_worker_id text,
    p_lease_token uuid,
    p_engine text,
    p_error_id text,
    p_error_code text,
    p_error_message text,
    p_execution_phase text,
    p_delivery_status text,
    p_raw_response_object_path text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job public.relay_jobs%rowtype;
begin
    select * into v_job from public.relay_jobs
     where id=p_job_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and execution_engine=p_engine and lease_owner=p_worker_id and lease_token=p_lease_token
     for update;
    if not found then
        return jsonb_build_object('outcome','lease_lost');
    end if;
    if v_job.status='cancelled' then
        return jsonb_build_object('outcome','cancelled');
    end if;

    update public.relay_jobs
       set status='failed', execution_phase=p_execution_phase, delivery_status=p_delivery_status,
           error_id=p_error_id, error_code=p_error_code, error_message=p_error_message,
           raw_response_object_path=coalesce(p_raw_response_object_path,raw_response_object_path),
           completed_at=now(), heartbeat_at=now()
     where id=p_job_id
     returning * into v_job;

    if p_session_id is not null then
        update public.relay_sessions set active_job_id=null, updated_at=now()
         where id=p_session_id and active_job_id=p_job_id;
    end if;
    return jsonb_build_object('outcome','failed','job',to_jsonb(v_job));
end;
$$;

create or replace function public.cancel_relay_job_v2(
    p_job_id uuid,
    p_session_id uuid,
    p_tenant_id text,
    p_conversation_hash text,
    p_engine text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job public.relay_jobs%rowtype;
begin
    select * into v_job from public.relay_jobs
     where id=p_job_id and relay_session_id=p_session_id
       and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and execution_engine=p_engine
     for update;
    if not found then return jsonb_build_object('outcome','not_found'); end if;
    if v_job.status not in ('succeeded','failed','cancelled','expired') then
        update public.relay_jobs
           set status='cancelled', execution_phase='cancelled', completed_at=now(),
               error_code='CANCELLED_BY_CLIENT', error_message='Job cancelled by client'
         where id=p_job_id returning * into v_job;
        update public.relay_sessions set active_job_id=null, updated_at=now()
         where id=p_session_id and active_job_id=p_job_id;
    end if;
    return jsonb_build_object('outcome','ok','job',to_jsonb(v_job));
end;
$$;

create or replace function public.request_relay_material_delete_v2(
    p_material_id text,
    p_tenant_id text,
    p_conversation_hash text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_material public.relay_materials%rowtype;
    v_refs bigint;
begin
    select * into v_material from public.relay_materials
     where id=p_material_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
     for update;
    if not found then return jsonb_build_object('outcome','not_found'); end if;
    if v_material.status='deleted' then return jsonb_build_object('outcome','deleted','material',to_jsonb(v_material)); end if;

    select count(*) into v_refs
      from public.relay_session_materials sm
      join public.relay_sessions s on s.id=sm.session_id
     where sm.material_id=p_material_id and s.expires_at > now();
    if v_refs > 0 then return jsonb_build_object('outcome','referenced','reference_count',v_refs); end if;

    update public.relay_materials set status='deleting', phase='deleting', updated_at=now()
     where id=p_material_id returning * into v_material;
    return jsonb_build_object('outcome','deleting','material',to_jsonb(v_material));
end;
$$;

-- Function grants.
revoke all on function public.claim_relay_job_v2(text,text,integer) from public,anon,authenticated;
revoke all on function public.renew_relay_job_lease_v2(uuid,text,uuid,text,integer) from public,anon,authenticated;
revoke all on function public.reserve_relay_material_v2(text,text,text,text,text,text,text,text,text,bigint,timestamptz,jsonb) from public,anon,authenticated;
revoke all on function public.publish_relay_material_ready_v2(text,text,text,text,integer,text,text,bigint,text,uuid) from public,anon,authenticated;
revoke all on function public.fail_relay_material_ingestion_v2(text,text,text,text,text,text,uuid) from public,anon,authenticated;
revoke all on function public.create_relay_session_v2(uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text[],text,timestamptz) from public,anon,authenticated;
revoke all on function public.submit_relay_session_job_v2(uuid,uuid,text,text,text,text,text,text,text,text,bigint,text,timestamptz) from public,anon,authenticated;
revoke all on function public.commit_relay_job_result_v2(uuid,uuid,text,text,text,uuid,text,bigint,text,text,text,jsonb,text) from public,anon,authenticated;
revoke all on function public.record_relay_job_failure_v2(uuid,uuid,text,text,text,uuid,text,text,text,text,text,text,text) from public,anon,authenticated;
revoke all on function public.cancel_relay_job_v2(uuid,uuid,text,text,text) from public,anon,authenticated;
revoke all on function public.request_relay_material_delete_v2(text,text,text) from public,anon,authenticated;

grant execute on function public.claim_relay_job_v2(text,text,integer) to service_role;
grant execute on function public.renew_relay_job_lease_v2(uuid,text,uuid,text,integer) to service_role;
grant execute on function public.reserve_relay_material_v2(text,text,text,text,text,text,text,text,text,bigint,timestamptz,jsonb) to service_role;
grant execute on function public.publish_relay_material_ready_v2(text,text,text,text,integer,text,text,bigint,text,uuid) to service_role;
grant execute on function public.fail_relay_material_ingestion_v2(text,text,text,text,text,text,uuid) to service_role;
grant execute on function public.create_relay_session_v2(uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text[],text,timestamptz) to service_role;
grant execute on function public.submit_relay_session_job_v2(uuid,uuid,text,text,text,text,text,text,text,text,bigint,text,timestamptz) to service_role;
grant execute on function public.commit_relay_job_result_v2(uuid,uuid,text,text,text,uuid,text,bigint,text,text,text,jsonb,text) to service_role;
grant execute on function public.record_relay_job_failure_v2(uuid,uuid,text,text,text,uuid,text,text,text,text,text,text,text) to service_role;
grant execute on function public.cancel_relay_job_v2(uuid,uuid,text,text,text) to service_role;
grant execute on function public.request_relay_material_delete_v2(text,text,text) to service_role;

-- ----------------------- URL ingestion worker support ----------------------

create or replace function public.claim_relay_material_ingestion_v2(
    p_worker_id text,
    p_lease_seconds integer default 300
)
returns setof public.relay_material_ingestions
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id text;
    v_token uuid := gen_random_uuid();
begin
    select i.id into v_id
      from public.relay_material_ingestions i
     where i.status='processing'
       and coalesce(i.source_identity->>'kind','')='url'
       and (
            i.lease_expires_at is null
            or i.lease_expires_at < now()
       )
     order by i.created_at asc
     for update skip locked
     limit 1;
    if v_id is null then return; end if;

    return query
    update public.relay_material_ingestions
       set phase='fetching', lease_owner=p_worker_id, lease_token=v_token,
           lease_expires_at=now()+make_interval(secs => greatest(p_lease_seconds,60)),
           updated_at=now()
     where id=v_id
     returning *;
end;
$$;

revoke all on function public.claim_relay_material_ingestion_v2(text,integer) from public,anon,authenticated;
grant execute on function public.claim_relay_material_ingestion_v2(text,integer) to service_role;

create or replace function public.retry_relay_material_v2(
    p_material_id text,
    p_ingestion_id text,
    p_tenant_id text,
    p_conversation_hash text,
    p_idempotency_key text,
    p_request_fingerprint text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_material public.relay_materials%rowtype;
    v_prev public.relay_material_ingestions%rowtype;
    v_key public.relay_request_keys%rowtype;
    v_scope text := 'material_retry:' || p_material_id;
    v_generation integer;
begin
    select * into v_material from public.relay_materials
     where id=p_material_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
     for update;
    if not found then return jsonb_build_object('outcome','not_found'); end if;
    if v_material.status='ready' then return jsonb_build_object('outcome','already_ready','material',to_jsonb(v_material)); end if;
    if v_material.status not in ('failed','processing') then return jsonb_build_object('outcome','state_conflict'); end if;

    select * into v_prev from public.relay_material_ingestions
     where material_id=p_material_id order by generation desc, created_at desc limit 1;
    if not found then return jsonb_build_object('outcome','ingestion_missing'); end if;
    if coalesce(v_prev.source_identity->>'kind','') <> 'url' then
        return jsonb_build_object('outcome','upload_required');
    end if;

    select * into v_key from public.relay_request_keys
     where tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and operation_scope=v_scope and idempotency_key=p_idempotency_key;
    if found then
        if v_key.request_fingerprint <> p_request_fingerprint then
            return jsonb_build_object('outcome','conflict');
        end if;
        return jsonb_build_object('outcome','reused','material',to_jsonb(v_material),'ingestion_id',v_key.resource_id);
    end if;

    v_generation := v_material.generation + 1;
    insert into public.relay_request_keys(
        tenant_id,conversation_hash,operation_scope,idempotency_key,request_fingerprint,resource_type,resource_id
    ) values (
        p_tenant_id,p_conversation_hash,v_scope,p_idempotency_key,p_request_fingerprint,'material_ingestion',p_ingestion_id
    );
    insert into public.relay_material_ingestions(
        id,material_id,tenant_id,conversation_hash,generation,status,phase,source_identity
    ) values (
        p_ingestion_id,p_material_id,p_tenant_id,p_conversation_hash,v_generation,'processing','awaiting_fetch',v_prev.source_identity
    );
    update public.relay_materials
       set status='processing',phase='awaiting_fetch',generation=v_generation,last_error_id=null,updated_at=now()
     where id=p_material_id returning * into v_material;
    return jsonb_build_object('outcome','created','material',to_jsonb(v_material),'ingestion_id',p_ingestion_id);
end;
$$;

revoke all on function public.retry_relay_material_v2(text,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.retry_relay_material_v2(text,text,text,text,text,text) to service_role;
-- ---------------------- provider binding fencing --------------------------

alter table public.relay_material_bindings add column if not exists lease_expires_at timestamptz;

create or replace function public.reserve_relay_material_binding_v2(
    p_material_id text,
    p_tenant_id text,
    p_conversation_hash text,
    p_provider text,
    p_upstream_profile text,
    p_account_scope text,
    p_protocol text,
    p_capability_group text,
    p_purpose text,
    p_transform_version text,
    p_lease_owner text,
    p_lease_seconds integer default 300
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_material public.relay_materials%rowtype;
    v_binding public.relay_material_bindings%rowtype;
    v_token uuid := gen_random_uuid();
begin
    select * into v_material from public.relay_materials
     where id=p_material_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and status='ready' and expires_at > now()
     for share;
    if not found then return jsonb_build_object('outcome','material_not_ready'); end if;

    select * into v_binding from public.relay_material_bindings
     where material_id=p_material_id and provider=p_provider
       and upstream_profile=p_upstream_profile and account_scope=p_account_scope
       and purpose=p_purpose and transform_version=p_transform_version
     for update;

    if found then
        if v_binding.lease_expires_at is not null and v_binding.lease_expires_at > now()
           and v_binding.lease_owner is distinct from p_lease_owner then
            return jsonb_build_object('outcome','busy','binding',to_jsonb(v_binding));
        end if;
        update public.relay_material_bindings
           set protocol=p_protocol, capability_group=p_capability_group,
               status='preparing', lease_owner=p_lease_owner, lease_token=v_token,
               lease_expires_at=now()+make_interval(secs => greatest(p_lease_seconds,60)),
               updated_at=now()
         where id=v_binding.id returning * into v_binding;
        return jsonb_build_object('outcome','claimed','binding',to_jsonb(v_binding));
    end if;

    begin
        insert into public.relay_material_bindings(
            tenant_id,conversation_hash,material_id,provider,upstream_profile,account_scope,
            protocol,capability_group,purpose,transform_version,status,generation,
            transport_epoch,lease_owner,lease_token,lease_expires_at
        ) values (
            p_tenant_id,p_conversation_hash,p_material_id,p_provider,p_upstream_profile,p_account_scope,
            p_protocol,p_capability_group,p_purpose,p_transform_version,'preparing',0,
            0,p_lease_owner,v_token,now()+make_interval(secs => greatest(p_lease_seconds,60))
        ) returning * into v_binding;
    exception when unique_violation then
        select * into v_binding from public.relay_material_bindings
         where material_id=p_material_id and provider=p_provider
           and upstream_profile=p_upstream_profile and account_scope=p_account_scope
           and purpose=p_purpose and transform_version=p_transform_version;
        return jsonb_build_object('outcome','busy','binding',to_jsonb(v_binding));
    end;
    return jsonb_build_object('outcome','claimed','binding',to_jsonb(v_binding));
end;
$$;

create or replace function public.complete_relay_material_binding_v2(
    p_binding_id uuid,
    p_lease_owner text,
    p_lease_token uuid,
    p_native_file_id text,
    p_native_uri text,
    p_derived_object_path text,
    p_expires_at timestamptz,
    p_transport_epoch integer,
    p_wire_request_hash text,
    p_cache_identity text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_binding public.relay_material_bindings%rowtype;
begin
    select * into v_binding from public.relay_material_bindings
     where id=p_binding_id and lease_owner=p_lease_owner and lease_token=p_lease_token
     for update;
    if not found then return jsonb_build_object('outcome','lease_lost'); end if;
    if v_binding.lease_expires_at is not null and v_binding.lease_expires_at < now() then
        return jsonb_build_object('outcome','lease_lost');
    end if;
    update public.relay_material_bindings
       set native_file_id=p_native_file_id, native_uri=p_native_uri,
           derived_object_path=p_derived_object_path, status='ready', expires_at=p_expires_at,
           generation=generation+1,
           transport_epoch=coalesce(p_transport_epoch,transport_epoch),
           wire_request_hash=p_wire_request_hash, cache_identity=p_cache_identity,
           lease_owner=null, lease_token=null, lease_expires_at=null,
           last_error_id=null, updated_at=now()
     where id=p_binding_id returning * into v_binding;
    return jsonb_build_object('outcome','ready','binding',to_jsonb(v_binding));
end;
$$;

create or replace function public.fail_relay_material_binding_v2(
    p_binding_id uuid,
    p_lease_owner text,
    p_lease_token uuid,
    p_error_id text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_binding public.relay_material_bindings%rowtype;
begin
    update public.relay_material_bindings
       set status='failed', last_error_id=p_error_id,
           lease_owner=null, lease_token=null, lease_expires_at=null, updated_at=now()
     where id=p_binding_id and lease_owner=p_lease_owner and lease_token=p_lease_token
     returning * into v_binding;
    if not found then return jsonb_build_object('outcome','lease_lost'); end if;
    return jsonb_build_object('outcome','failed','binding',to_jsonb(v_binding));
end;
$$;

revoke all on function public.reserve_relay_material_binding_v2(text,text,text,text,text,text,text,text,text,text,text,integer) from public,anon,authenticated;
revoke all on function public.complete_relay_material_binding_v2(uuid,text,uuid,text,text,text,timestamptz,integer,text,text) from public,anon,authenticated;
revoke all on function public.fail_relay_material_binding_v2(uuid,text,uuid,text) from public,anon,authenticated;
grant execute on function public.reserve_relay_material_binding_v2(text,text,text,text,text,text,text,text,text,text,text,integer) to service_role;
grant execute on function public.complete_relay_material_binding_v2(uuid,text,uuid,text,text,text,timestamptz,integer,text,text) to service_role;
grant execute on function public.fail_relay_material_binding_v2(uuid,text,uuid,text) to service_role;

