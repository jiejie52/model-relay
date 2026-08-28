-- Provider-neutral Fusion Runtime persistence.
-- Apply after 001_relay_schema.sql.

create table if not exists public.fusion_corpora (
    id text primary key,
    tenant_id text not null,
    conversation_hash text not null,
    version integer not null default 1,
    corpus_hash text not null,
    manifest_object_path text not null,
    material_count integer not null default 0,
    candidate_count integer not null default 0,
    effective_tokens bigint not null default 0,
    status text not null default 'ready',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null
);

create table if not exists public.fusion_materials (
    id text primary key,
    fusion_corpus_id text not null references public.fusion_corpora(id) on delete cascade,
    material_id text not null,
    role text not null,
    candidate_id text,
    filename text not null,
    document_id text,
    ir_key text,
    content_hash text,
    original_object_path text,
    full_text_object_path text,
    projection_object_path text not null,
    parse_status text,
    warning_level text,
    effective_tokens bigint not null default 0,
    visual_count integer not null default 0,
    table_count integer not null default 0,
    created_at timestamptz not null default now(),
    unique (fusion_corpus_id, material_id)
);

create table if not exists public.fusion_artifacts (
    id text primary key,
    fusion_corpus_id text not null references public.fusion_corpora(id) on delete cascade,
    artifact_type text not null,
    artifact_version integer not null default 1,
    parent_artifact_ids jsonb not null default '[]'::jsonb,
    decision_version integer,
    provider text,
    model text,
    think_level text,
    request_object_path text,
    response_object_path text not null,
    compact_result jsonb,
    schema_version text not null default 'fusion-runtime/1.0',
    status text not null default 'succeeded',
    created_at timestamptz not null default now(),
    unique (fusion_corpus_id, artifact_type, artifact_version)
);

create index if not exists fusion_corpora_owner_idx
    on public.fusion_corpora (tenant_id, conversation_hash, created_at desc);
create index if not exists fusion_materials_corpus_idx
    on public.fusion_materials (fusion_corpus_id, material_id);
create index if not exists fusion_artifacts_corpus_idx
    on public.fusion_artifacts (fusion_corpus_id, artifact_type, artifact_version desc);

alter table public.fusion_corpora enable row level security;
alter table public.fusion_materials enable row level security;
alter table public.fusion_artifacts enable row level security;

revoke all on table public.fusion_corpora from anon, authenticated;
revoke all on table public.fusion_materials from anon, authenticated;
revoke all on table public.fusion_artifacts from anon, authenticated;

grant select, insert, update, delete on table public.fusion_corpora to service_role;
grant select, insert, update, delete on table public.fusion_materials to service_role;
grant select, insert, update, delete on table public.fusion_artifacts to service_role;
