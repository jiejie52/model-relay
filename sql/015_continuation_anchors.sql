-- Model Relay 9.0.0. Apply after 014_provider_binding_state_contract.sql.
-- Continuation anchors and pin leasing for stateful conversation turn continuation.
begin;

create table if not exists public.relay_continuation_anchors (
    id uuid primary key default gen_random_uuid(),
    tenant_id text not null,
    conversation_hash text not null,
    session_id uuid not null references public.relay_sessions(id) on delete cascade,
    parent_anchor_id uuid references public.relay_continuation_anchors(id) on delete set null,
    history_version integer not null,
    head_fingerprint text,
    turn_chain_digest text,
    upstream_handle text,
    provider text,
    connection_id text,
    account_scope_hash text,
    state text not null default 'active' check (state in ('active', 'invalidated', 'expired')),
    expires_at timestamptz,
    created_at timestamptz not null default timezone('utc'::text, now()),
    metadata jsonb not null default '{}'::jsonb
);

comment on table public.relay_continuation_anchors is
    'Stateful provider continuation anchors tracking server-side conversation context across turns.';

create index if not exists relay_continuation_anchors_tenant_conv_idx
    on public.relay_continuation_anchors (tenant_id, conversation_hash);

create index if not exists relay_continuation_anchors_session_history_idx
    on public.relay_continuation_anchors (session_id, history_version);

create index if not exists relay_continuation_anchors_lookup_idx
    on public.relay_continuation_anchors (tenant_id, conversation_hash, session_id, history_version, state);

create index if not exists relay_continuation_anchors_state_idx
    on public.relay_continuation_anchors (state);

create index if not exists relay_continuation_anchors_expires_at_idx
    on public.relay_continuation_anchors (expires_at);

create table if not exists public.relay_anchor_pins (
    request_id uuid not null references public.relay_requests(id) on delete cascade,
    anchor_id uuid not null references public.relay_continuation_anchors(id) on delete cascade,
    hold_until timestamptz,
    created_at timestamptz not null default timezone('utc'::text, now()),
    primary key (request_id, anchor_id)
);

comment on table public.relay_anchor_pins is
    'Active request holds preventing recycling or mutation of continuation anchors during execution dispatch.';

create index if not exists relay_anchor_pins_anchor_idx
    on public.relay_anchor_pins (anchor_id);

create or replace function public.complete_continuation_request_v90(
    p_args jsonb,
    p_material_occurrences jsonb,
    p_continuation_anchor jsonb
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_valid boolean;
    v_req uuid := (p_args->>'p_request_id')::uuid;
    v_session_id uuid := (p_args->>'p_session_id')::uuid;
    v_anchor_id uuid;
    v_parent_anchor_id uuid;
    v_tenant_id text;
    v_conversation_hash text;
    v_history_version integer;
    v_head_fingerprint text;
    v_turn_chain_digest text;
    v_upstream_handle text;
    v_provider text;
    v_connection_id text;
    v_account_scope_hash text;
    v_state text;
    v_expires_at timestamptz;
    v_metadata jsonb;
    v_req_row public.relay_requests%rowtype;
    v_sess_row public.relay_sessions%rowtype;
    v_placement_version text;
begin
    -- 1. Execute original complete_material_request_v89 logic (validation, history advancing, committed material ledger)
    select placement_contract_version into v_placement_version from public.relay_requests where id = v_req;
    if v_placement_version = 'material-placement/2'
       or (p_material_occurrences is not null and p_material_occurrences is distinct from 'null'::jsonb and jsonb_array_length(p_material_occurrences) > 0) then
        v_valid := public.complete_material_request_v89(p_args, coalesce(p_material_occurrences, '[]'::jsonb));
    else
        v_valid := public.complete_relay_request_v66(
            p_request_id => v_req,
            p_session_id => v_session_id,
            p_expected_history_version => (p_args->>'p_expected_history_version')::bigint,
            p_history_object_id => (p_args->>'p_history_object_id')::text,
            p_result_object_id => (p_args->>'p_result_object_id')::text,
            p_output_object_id => (p_args->>'p_output_object_id')::text,
            p_compact_result => p_args->'p_compact_result',
            p_provider_response_id => (p_args->>'p_provider_response_id')::text,
            p_fence_owner => (p_args->>'p_fence_owner')::text,
            p_fence_epoch => (p_args->>'p_fence_epoch')::bigint
        );
    end if;

    if not v_valid then
        return false;
    end if;

    -- 2. If p_continuation_anchor is provided and non-empty, insert relay_continuation_anchors row
    if p_continuation_anchor is not null
       and p_continuation_anchor is distinct from 'null'::jsonb
       and p_continuation_anchor <> '{}'::jsonb then
        select * into v_req_row from public.relay_requests where id = v_req;
        select * into v_sess_row from public.relay_sessions where id = v_session_id;

        v_anchor_id := coalesce(
            nullif(p_continuation_anchor->>'anchor_id', '')::uuid,
            nullif(p_continuation_anchor->>'id', '')::uuid,
            gen_random_uuid()
        );
        v_parent_anchor_id := nullif(p_continuation_anchor->>'parent_anchor_id', '')::uuid;
        v_tenant_id := coalesce(p_continuation_anchor->>'tenant_id', v_req_row.tenant_id, v_sess_row.tenant_id);
        v_conversation_hash := coalesce(p_continuation_anchor->>'conversation_hash', v_req_row.conversation_hash, v_sess_row.conversation_hash);
        v_history_version := coalesce(
            (p_continuation_anchor->>'history_version')::integer,
            (p_args->>'p_expected_history_version')::integer + 1,
            v_sess_row.history_version
        );
        v_head_fingerprint := coalesce(p_continuation_anchor->>'head_fingerprint', p_continuation_anchor->>'fingerprint');
        v_turn_chain_digest := p_continuation_anchor->>'turn_chain_digest';
        v_upstream_handle := case
            when jsonb_typeof(p_continuation_anchor->'upstream_handle') in ('object', 'array')
                then (p_continuation_anchor->'upstream_handle')::text
            else p_continuation_anchor->>'upstream_handle'
        end;
        v_provider := coalesce(p_continuation_anchor->>'provider', v_req_row.provider, v_sess_row.provider);
        v_connection_id := coalesce(p_continuation_anchor->>'connection_id', v_req_row.connection_id, v_sess_row.connection_id);
        v_account_scope_hash := p_continuation_anchor->>'account_scope_hash';
        v_state := coalesce(p_continuation_anchor->>'state', 'active');
        v_expires_at := nullif(p_continuation_anchor->>'expires_at', '')::timestamptz;
        v_metadata := coalesce(p_continuation_anchor->'metadata', '{}'::jsonb);

        -- Invalidate conflicting anchors if requested or if parent anchor specified
        if v_parent_anchor_id is not null then
            update public.relay_continuation_anchors
               set state = 'invalidated'
             where session_id = v_session_id
               and id <> v_anchor_id
               and state = 'active'
               and (
                   parent_anchor_id = v_parent_anchor_id
                   or history_version >= v_history_version
               );
        else
            update public.relay_continuation_anchors
               set state = 'invalidated'
             where session_id = v_session_id
               and id <> v_anchor_id
               and state = 'active'
               and history_version = v_history_version;
        end if;

        insert into public.relay_continuation_anchors (
            id,
            tenant_id,
            conversation_hash,
            session_id,
            parent_anchor_id,
            history_version,
            head_fingerprint,
            turn_chain_digest,
            upstream_handle,
            provider,
            connection_id,
            account_scope_hash,
            state,
            expires_at,
            created_at,
            metadata
        ) values (
            v_anchor_id,
            v_tenant_id,
            v_conversation_hash,
            v_session_id,
            v_parent_anchor_id,
            v_history_version,
            v_head_fingerprint,
            v_turn_chain_digest,
            v_upstream_handle,
            v_provider,
            v_connection_id,
            v_account_scope_hash,
            v_state,
            v_expires_at,
            timezone('utc'::text, now()),
            v_metadata
        )
        on conflict (id) do update set
            state = excluded.state,
            expires_at = excluded.expires_at,
            metadata = excluded.metadata;
    end if;

    return true;
end;
$$;

comment on function public.complete_continuation_request_v90(jsonb, jsonb, jsonb) is
    'Atomically verifies execution fence, completes request history advancing, and registers newly produced continuation anchor.';

create or replace function public.seal_continuation_and_dispatch_v90(
    p_request_id uuid,
    p_anchor_id uuid,
    p_fence_owner text,
    p_fence_epoch integer,
    p_hold_seconds integer default 300
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_req public.relay_requests%rowtype;
    v_anchor public.relay_continuation_anchors%rowtype;
    v_hold_until timestamptz;
begin
    -- 1. Validate execution fence in not_sent state
    if not public.validate_request_execution_fence_v71(p_request_id, p_fence_owner, p_fence_epoch::bigint, array['not_sent']) then
        return false;
    end if;

    select * into v_req from public.relay_requests where id = p_request_id for update;
    if not found or v_req.provider_dispatch_state <> 'not_sent'
       or v_req.status in ('cancelled', 'failed', 'succeeded', 'indeterminate') then
        return false;
    end if;

    -- 2. Confirm continuation anchor is active and not expired
    if p_anchor_id is not null then
        select * into v_anchor from public.relay_continuation_anchors where id = p_anchor_id for update;
        if not found then
            raise exception 'CONTINUATION_ANCHOR_NOT_FOUND';
        end if;
        if v_anchor.state <> 'active' then
            raise exception 'CONTINUATION_ANCHOR_INACTIVE';
        end if;
        if v_anchor.expires_at is not null and v_anchor.expires_at <= clock_timestamp() then
            raise exception 'CONTINUATION_ANCHOR_EXPIRED';
        end if;
        if v_anchor.session_id <> v_req.session_id or v_anchor.tenant_id <> v_req.tenant_id then
            raise exception 'CONTINUATION_ANCHOR_SCOPE_MISMATCH';
        end if;

        -- 3. Insert pin lock into relay_anchor_pins
        v_hold_until := clock_timestamp() + make_interval(secs => coalesce(nullif(p_hold_seconds, 0), 300));
        insert into public.relay_anchor_pins (
            request_id,
            anchor_id,
            hold_until,
            created_at
        ) values (
            p_request_id,
            p_anchor_id,
            v_hold_until,
            timezone('utc'::text, now())
        )
        on conflict (request_id, anchor_id) do update
            set hold_until = excluded.hold_until;
    end if;

    -- 4. Transition request state to dispatch_started
    update public.relay_requests
       set provider_dispatch_state = 'dispatch_started',
           started_at = coalesce(started_at, clock_timestamp())
     where id = p_request_id;

    if not public.validate_request_execution_fence_v71(p_request_id, p_fence_owner, p_fence_epoch::bigint, array['dispatch_started']) then
        raise exception 'EXECUTION_FENCE_LOST';
    end if;

    return true;
end;
$$;

comment on function public.seal_continuation_and_dispatch_v90(uuid, uuid, text, integer, integer) is
    'Atomically verifies request execution fence, confirms active unexpired continuation anchor, pins reference hold, and marks dispatch_started.';

create or replace function public.release_continuation_pins_v90(
    p_request_id uuid
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    delete from public.relay_anchor_pins where request_id = p_request_id;
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

comment on function public.release_continuation_pins_v90(uuid) is
    'Releases active continuation anchor pins held for the specified request.';

revoke all on function public.complete_continuation_request_v90(jsonb, jsonb, jsonb) from public, anon, authenticated;
revoke all on function public.seal_continuation_and_dispatch_v90(uuid, uuid, text, integer, integer) from public, anon, authenticated;
revoke all on function public.release_continuation_pins_v90(uuid) from public, anon, authenticated;

grant execute on function public.complete_continuation_request_v90(jsonb, jsonb, jsonb) to service_role;
grant execute on function public.seal_continuation_and_dispatch_v90(uuid, uuid, text, integer, integer) to service_role;
grant execute on function public.release_continuation_pins_v90(uuid) to service_role;

grant select, insert, update, delete on table public.relay_continuation_anchors to service_role;
grant select, insert, update, delete on table public.relay_anchor_pins to service_role;

notify pgrst, 'reload schema';
commit;
