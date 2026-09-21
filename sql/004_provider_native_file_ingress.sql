-- Relay 0.4.0: provider-native-first file ingress.
-- Apply after 003_relay_v2_session_request_material.sql.
-- This migration does not move request/history/raw-response artifacts out of Supabase.

alter table public.relay_materials
    alter column object_id drop not null,
    add column if not exists target_connection_id text,
    add column if not exists durability_policy text not null default 'native_first',
    add column if not exists fallback_policy text not null default 'on_provider_unavailable',
    add column if not exists durability text not null default 'relay_backed',
    add column if not exists source_kind text,
    add column if not exists source_recoverable boolean not null default false,
    add column if not exists declared_size bigint,
    add column if not exists actual_size bigint,
    add column if not exists binding_generation bigint not null default 0;

update public.relay_materials
   set actual_size = coalesce(actual_size, size_bytes),
       durability = case when object_id is not null then 'relay_backed' else durability end,
       source_kind = coalesce(source_kind, case when source_url is not null then 'readable_url' else 'legacy' end)
 where actual_size is null or source_kind is null;

alter table public.relay_materials drop constraint if exists relay_materials_status_check;
alter table public.relay_materials add constraint relay_materials_status_check check (
    status in (
        'receiving', 'binding', 'ready_provider', 'ready', 'fallback_stored',
        'degraded', 'reupload_required', 'failed', 'deleted'
    )
);

alter table public.relay_materials drop constraint if exists relay_materials_durability_policy_check;
alter table public.relay_materials add constraint relay_materials_durability_policy_check check (
    durability_policy in ('native_first', 'relay_backed')
);

alter table public.relay_materials drop constraint if exists relay_materials_fallback_policy_check;
alter table public.relay_materials add constraint relay_materials_fallback_policy_check check (
    fallback_policy in ('never', 'on_provider_unavailable', 'always')
);

alter table public.relay_materials drop constraint if exists relay_materials_durability_check;
alter table public.relay_materials add constraint relay_materials_durability_check check (
    durability in ('provider_bound', 'relay_backed', 'source_recoverable', 'reupload_required')
);

create index if not exists relay_materials_target_connection_idx
    on public.relay_materials (target_connection_id, status, created_at desc);

alter table public.provider_material_bindings
    add column if not exists id uuid default gen_random_uuid(),
    add column if not exists provider text,
    add column if not exists account_scope_hash text,
    add column if not exists external_file_id text,
    add column if not exists external_uri text,
    add column if not exists state text,
    add column if not exists generation bigint not null default 1,
    add column if not exists last_verified_at timestamptz,
    add column if not exists cleanup_policy text,
    add column if not exists last_used_at timestamptz;

update public.provider_material_bindings
   set external_file_id = coalesce(external_file_id, provider_file_id),
       external_uri = coalesce(external_uri, file_uri),
       state = coalesce(state, processing_state, 'active'),
       account_scope_hash = coalesce(account_scope_hash, 'legacy-unknown')
 where external_file_id is null
    or external_uri is null
    or state is null
    or account_scope_hash is null;

-- 0.3.x used a provider-agnostic primary key. 0.4.0 must retain distinct
-- provider resources across API-key/account scopes and binding generations.
-- A surrogate primary key keeps PostgREST happy while the logical unique key
-- follows the patched architecture contract.
alter table public.provider_material_bindings
    alter column id set default gen_random_uuid();
update public.provider_material_bindings
   set id = gen_random_uuid()
 where id is null;
alter table public.provider_material_bindings
    alter column id set not null;
alter table public.provider_material_bindings
    drop constraint if exists provider_material_bindings_pkey;
do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conrelid = 'public.provider_material_bindings'::regclass
           and contype = 'p'
    ) then
        alter table public.provider_material_bindings
            add constraint provider_material_bindings_pkey primary key (id);
    end if;
end $$;

create unique index if not exists provider_material_bindings_identity_uq
    on public.provider_material_bindings (
        material_id, connection_id, account_scope_hash, purpose,
        representation, adapter_version, generation
    );
create index if not exists provider_material_bindings_scope_idx
    on public.provider_material_bindings (
        material_id, connection_id, account_scope_hash, generation desc
    );

create table if not exists public.material_fallback_objects (
    material_id text primary key references public.relay_materials(id) on delete cascade,
    object_id text not null references public.relay_objects(id) on delete restrict,
    storage_id text not null,
    bucket text not null,
    object_key text not null,
    sha256 text not null,
    size_bytes bigint not null,
    retention_policy text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz
);

-- Backfill old 0.3.x materials whose object_id represented the mandatory input copy.
insert into public.material_fallback_objects (
    material_id, object_id, storage_id, bucket, object_key, sha256, size_bytes,
    retention_policy, created_at
)
select m.id, o.id, o.storage_id, o.bucket, o.object_key, o.sha256, o.size_bytes,
       'legacy-backfill', coalesce(o.created_at, m.created_at)
  from public.relay_materials m
  join public.relay_objects o on o.id = m.object_id
 where m.object_id is not null
on conflict (material_id) do nothing;

create table if not exists public.material_binding_attempts (
    attempt_id text primary key,
    material_id text not null references public.relay_materials(id) on delete cascade,
    connection_id text not null,
    account_scope_hash text,
    generation bigint not null default 1,
    phase text not null,
    provider_request_id text,
    raw_response_ref text references public.relay_objects(id) on delete set null,
    raw_error_ref text references public.relay_objects(id) on delete set null,
    uncertain boolean not null default false,
    lease_epoch bigint,
    detail jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    completed_at timestamptz
);

create index if not exists material_binding_attempts_material_idx
    on public.material_binding_attempts (material_id, connection_id, created_at desc);

alter table public.relay_requests
    add column if not exists material_binding_snapshot jsonb;

alter table public.material_fallback_objects enable row level security;
alter table public.material_binding_attempts enable row level security;

revoke all on table public.material_fallback_objects, public.material_binding_attempts
    from anon, authenticated;
grant select, insert, update, delete on table public.material_fallback_objects,
    public.material_binding_attempts to service_role;
