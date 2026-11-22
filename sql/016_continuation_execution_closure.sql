-- Apply after 015_continuation_anchors.sql. Old anchors lack coverage proof.
begin;

alter table public.relay_continuation_anchors
    add column if not exists source_request_id uuid references public.relay_requests(id) on delete cascade,
    add column if not exists covered_turn_count integer,
    add column if not exists covered_material_bindings jsonb,
    add column if not exists connection_spec_hash text,
    add column if not exists anchor_class text,
    add column if not exists affinity_epoch bigint,
    add column if not exists protocol text,
    add column if not exists model text,
    add column if not exists invalidated_by_request_id uuid references public.relay_requests(id) on delete set null,
    add column if not exists invalidation_reason text;
alter table public.relay_continuation_anchors
    drop constraint if exists relay_continuation_anchors_state_check;
alter table public.relay_continuation_anchors
    add constraint relay_continuation_anchors_state_check
    check (state in ('active', 'superseded', 'expired', 'invalidated'));
alter table public.relay_requests
    add column if not exists sealed_continuation_binding jsonb,
    add column if not exists continuation_plan_object_id text,
    add column if not exists continuation_plan_hash text,
    add column if not exists produced_anchor_id uuid;
create unique index if not exists relay_continuation_anchor_source_v91
    on public.relay_continuation_anchors(source_request_id);
create index if not exists relay_continuation_prefix_v91
    on public.relay_continuation_anchors(session_id, head_fingerprint, covered_turn_count desc)
    where state='active' and source_request_id is not null;

alter table public.relay_continuation_anchors enable row level security;
alter table public.relay_anchor_pins enable row level security;
revoke all on public.relay_continuation_anchors, public.relay_anchor_pins from public, anon, authenticated;

create or replace function public.seal_materials_and_dispatch_v89(
    p_args jsonb,
    p_material_occurrences jsonb,
    p_historical_bindings jsonb,
    p_safety_seconds integer
) returns boolean
language plpgsql
security definer
set search_path=public
as $$
declare
    r public.relay_requests%rowtype;
    b public.provider_material_bindings%rowtype;
    m public.relay_materials%rowtype;
    item jsonb;
    occ jsonb;
    snap jsonb;
    pins jsonb;
    req uuid := (p_args->>'p_request_id')::uuid;
    owner text := p_args->>'p_fence_owner';
    epoch bigint := (p_args->>'p_fence_epoch')::bigint;
    i integer := 0;
    valid boolean;
    v_state_failure text;
    v_remaining_ttl_seconds integer;
    v_diagnostic jsonb;
begin
    if not public.validate_request_execution_fence_v71(req, owner, epoch, array['not_sent']) then
        return false;
    end if;

    select * into r from public.relay_requests where id=req for update;
    if r.placement_contract_version is distinct from 'material-placement/2'
       or jsonb_typeof(p_material_occurrences) is distinct from 'array'
       or jsonb_typeof(p_historical_bindings) is distinct from 'array'
       or p_safety_seconds is null or p_safety_seconds < 30 or p_safety_seconds > 3600
       or p_safety_seconds is distinct from (r.material_placement_plan->>'binding_safety_seconds')::integer
       or jsonb_array_length(p_material_occurrences) <> jsonb_array_length(r.material_binding_snapshot) then
        raise exception 'MATERIAL_PLACEMENT_CONTRACT_INVALID';
    end if;

    for occ in select value from jsonb_array_elements(p_material_occurrences) loop
        snap := r.material_binding_snapshot->i;
        if not (occ->'binding' @> (snap-'inline_data'))
           or occ->>'material_id' is distinct from snap->>'material_id'
           or occ->>'source_request_id' is distinct from req::text
           or (occ->>'part_index')::integer is distinct from i
           or occ->>'material_id' is distinct from r.material_placement_plan->'current_effective_material_ids'->>i then
            raise exception 'CURRENT_TURN_MATERIAL_BINDING_MISMATCH';
        end if;
        i := i + 1;
    end loop;

    -- The ledger stays complete. Resource pins protect the visible history only.
    if r.material_placement_plan->>'context_policy' in ('explicit','session_materials_only')
       and jsonb_array_length(p_historical_bindings) <> 0 then
        raise exception 'HIDDEN_HISTORY_MATERIAL_PIN';
    end if;
    for occ in select value from jsonb_array_elements(r.material_placement_plan->'verified_history_occurrences')
        where coalesce(r.material_placement_plan->>'context_policy','conversation') = 'conversation'
    loop
        if not exists (
            select 1 from jsonb_array_elements(p_historical_bindings) x
            where x->>'material_id' = occ->>'material_id'
              and x->>'binding_generation' = occ->>'binding_generation'
              and (x->>'external_uri') is not distinct from (occ->'binding'->>'external_uri')
        ) then
            raise exception 'HISTORICAL_MATERIAL_UNPINNED';
        end if;
    end loop;

    pins := coalesce(p_historical_bindings, '[]'::jsonb)
        || coalesce((select jsonb_agg(x->'binding') from jsonb_array_elements(p_material_occurrences) x), '[]'::jsonb);

    -- Lock material and binding rows in stable order before checking and pinning.
    perform 1
      from public.relay_materials
     where id in (select x->>'material_id' from jsonb_array_elements(pins) x)
     order by id
     for update;
    perform 1
      from public.provider_material_bindings
     where id::text in (select x->>'provider_binding_id' from jsonb_array_elements(pins) x)
     order by id
     for update;

    for item in select value from jsonb_array_elements(pins) loop
        select * into m from public.relay_materials where id=item->>'material_id';
        if not found
           or m.tenant_id <> r.tenant_id
           or m.conversation_hash <> r.conversation_hash
           or m.sha256 is distinct from item->>'content_sha256'
           or m.status in ('deleted', 'failed', 'reupload_required')
           or item->>'connection_id' is distinct from r.connection_id
           or item->>'account_scope_hash' is distinct from r.material_placement_plan->'identity'->>'account_scope_id' then
            raise exception using
                message = 'MATERIAL_BINDING_SCOPE_MISMATCH',
                detail = jsonb_build_object(
                    'request_id', req::text,
                    'session_id', r.session_id::text,
                    'material_id', item->>'material_id',
                    'connection_id', r.connection_id,
                    'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                    'validation_failure_reason', 'BINDING_SCOPE_MISMATCH',
                    'failure_stage', 'atomic_dispatch_seal_sql'
                )::text;
        end if;

        if item->>'representation' is distinct from 'gemini_inline_data' then
            select * into b
              from public.provider_material_bindings
             where id::text = item->>'provider_binding_id';

            if not found then
                v_diagnostic := jsonb_build_object(
                    'request_id', req::text,
                    'session_id', r.session_id::text,
                    'material_id', item->>'material_id',
                    'connection_id', r.connection_id,
                    'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                    'failure_stage', 'atomic_dispatch_seal_sql'
                );
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_NOT_FOUND'
                    ))::text;
            end if;

            v_remaining_ttl_seconds := case
                when b.expires_at is not null
                    then floor(extract(epoch from (b.expires_at - clock_timestamp())))::integer
                else null
            end;
            v_diagnostic := jsonb_build_object(
                'request_id', req::text,
                'session_id', r.session_id::text,
                'material_id', item->>'material_id',
                'connection_id', r.connection_id,
                'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                'actual_generation', b.generation,
                'binding_state', b.state,
                'processing_state', b.processing_state,
                'remaining_ttl_seconds', v_remaining_ttl_seconds,
                'failure_stage', 'atomic_dispatch_seal_sql'
            );

            if b.material_id is distinct from m.id
               or b.connection_id is distinct from r.connection_id
               or b.account_scope_hash is distinct from item->>'account_scope_hash' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_SCOPE_MISMATCH'
                    ))::text;
            end if;

            if b.representation is distinct from item->>'representation' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_REPRESENTATION_MISMATCH'
                    ))::text;
            end if;

            if b.generation is distinct from (item->>'binding_generation')::bigint then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_GENERATION_MISMATCH'
                    ))::text;
            end if;

            v_state_failure := public.provider_material_binding_state_failure_v891(
                b.state, b.processing_state
            );
            if v_state_failure is not null then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', v_state_failure
                    ))::text;
            end if;

            if b.expires_at is null
               or b.expires_at <= clock_timestamp() + make_interval(secs=>p_safety_seconds) then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_EXPIRED'
                    ))::text;
            end if;

            if coalesce(b.external_uri, b.file_uri) is distinct from item->>'external_uri' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_URI_MISMATCH'
                    ))::text;
            end if;
        end if;
    end loop;

    for item in select value from jsonb_array_elements(pins) loop
        insert into public.relay_material_pins(
            request_id, binding_key, material_id, provider_binding_id,
            binding_generation, binding_snapshot, status
        ) values (
            req,
            (item->>'material_id') || ':' || (item->>'binding_generation') || ':' || coalesce(item->>'provider_binding_id', 'inline'),
            item->>'material_id',
            nullif(item->>'provider_binding_id', '')::uuid,
            (item->>'binding_generation')::bigint,
            item,
            'active'
        )
        on conflict(request_id, binding_key) do update
            set status='active', released_at=null;
    end loop;

    valid := public.seal_cache_and_dispatch_v3(
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_binding_version => (p_args->>'p_binding_version')::integer,
        p_binding_hash => (p_args->>'p_binding_hash')::text,
        p_payload_hash => (p_args->>'p_payload_hash')::text,
        p_fence_owner => (p_args->>'p_fence_owner')::text,
        p_fence_epoch => (p_args->>'p_fence_epoch')::bigint
    );
    if not valid then
        raise exception 'MATERIAL_DISPATCH_SEAL_REJECTED';
    end if;

    update public.relay_requests
       set material_seal_occurrences=p_material_occurrences
     where id=req;
    if not public.validate_request_execution_fence_v71(req, owner, epoch, array['dispatch_started']) then
        raise exception 'EXECUTION_FENCE_LOST';
    end if;
    return true;
end;
$$;

create or replace function public.seal_continuation_context_v91(
    p_args jsonb, p_material_occurrences jsonb, p_historical_bindings jsonb,
    p_safety_seconds integer, p_continuation_binding jsonb, p_anchor_safety_seconds integer
) returns boolean language plpgsql security definer set search_path=public as $$
declare
    r public.relay_requests%rowtype;
    s public.relay_sessions%rowtype;
    a public.relay_continuation_anchors%rowtype;
    req uuid := (p_args->>'p_request_id')::uuid;
    parent uuid := (p_continuation_binding->>'active_anchor_id')::uuid;
    owner text := p_args->>'p_fence_owner';
    epoch bigint := (p_args->>'p_fence_epoch')::bigint;
    route jsonb;
    topology jsonb;
    mode text := p_continuation_binding->>'effective_mode';
    valid boolean;
begin
    if not public.validate_request_execution_fence_v71(req,owner,epoch,array['not_sent']) then
        return false;
    end if;
    select * into r from public.relay_requests where id=req for update;
    select * into s from public.relay_sessions where id=r.session_id for update;
    route := s.metadata->'_relay_route';
    topology := r.metadata->'_relay_continuation'->'topology';
    if p_continuation_binding->>'schema_version' is distinct from 'relay-continuation-binding/2'
       or mode is null or mode not in ('stateless_full_replay','stateful_root_init','stateful_anchor_delta')
       or p_anchor_safety_seconds is null or p_anchor_safety_seconds not between 1 and 3600
       or (mode='stateful_anchor_delta') is distinct from (parent is not null)
       or (p_continuation_binding->'store_upstream_state') is distinct from to_jsonb(mode<>'stateless_full_replay')
       or (parent is null and p_continuation_binding->>'upstream_handle' is not null)
       or (mode<>'stateless_full_replay' and (
            topology->'reusable_on_commit' is distinct from 'true'::jsonb
            or topology->>'context_policy' is distinct from 'conversation'
            or coalesce(topology->>'history_mode', 'cumulative') is distinct from 'cumulative'
            or topology->>'parent_head_fingerprint' is distinct from p_continuation_binding->>'parent_head_fingerprint'
            or s.context_policy is distinct from 'conversation'
            or coalesce(s.history_mode, 'cumulative') is distinct from 'cumulative'
            or coalesce(r.material_placement_plan->>'context_policy','conversation')<>'conversation')) then
        raise exception 'CONTINUATION_BINDING_INVALID';
    end if;

    if parent is not null then
        select * into a from public.relay_continuation_anchors where id=parent for update;
        if not found or a.state<>'active' or a.source_request_id is null
           or a.expires_at is null or a.expires_at<=clock_timestamp()+make_interval(secs=>p_anchor_safety_seconds)
           or a.history_version>r.expected_history_version
           or a.session_id is distinct from s.id or a.tenant_id is distinct from r.tenant_id
           or a.conversation_hash is distinct from r.conversation_hash or a.provider is distinct from r.provider
           or a.connection_id is distinct from r.connection_id
           or a.connection_spec_hash is distinct from route->>'connection_spec_hash'
           or a.account_scope_hash is distinct from route->>'account_scope_id'
           or a.head_fingerprint is distinct from p_continuation_binding->>'parent_head_fingerprint'
           or a.turn_chain_digest is distinct from p_continuation_binding->>'covered_turn_chain_hash'
           or a.turn_chain_digest is distinct from (topology->'turn_prefix_chain'->>(a.covered_turn_count-1))
           or a.upstream_handle is distinct from p_continuation_binding->>'upstream_handle'
           or a.covered_turn_count is distinct from (p_continuation_binding->>'covered_turn_count')::integer then
            raise exception 'CONTINUATION_PARENT_NO_LONGER_USABLE';
        end if;
        -- The material sealer rechecks these exact pinned references against live rows.
        if exists (
            select 1 from jsonb_array_elements(a.covered_material_bindings) ref
            where not exists (
                select 1 from jsonb_array_elements(coalesce(p_historical_bindings,'[]'::jsonb)) binding
                where binding->>'material_id'=ref->>'material_id'
                  and binding->>'binding_generation'=ref->>'binding_generation'
                  and binding->>'provider_binding_id' is not distinct from ref->>'provider_binding_id'
                  and binding->>'external_uri' is not distinct from ref->>'external_uri'
            )
        ) then raise exception 'CONTINUATION_PARENT_NO_LONGER_USABLE'; end if;
    end if;

    if r.placement_contract_version='material-placement/2' then
        valid := public.seal_materials_and_dispatch_v89(
            p_args,p_material_occurrences,p_historical_bindings,p_safety_seconds);
    else
        valid := public.seal_cache_and_dispatch_v3(req,(p_args->>'p_binding_version')::integer,
            p_args->>'p_binding_hash',p_args->>'p_payload_hash',owner,epoch);
    end if;
    if not valid then return false; end if;

    if parent is not null then
        if a.expires_at<=clock_timestamp()+make_interval(secs=>p_anchor_safety_seconds) then
            raise exception 'CONTINUATION_PARENT_NO_LONGER_USABLE';
        end if;
        insert into public.relay_anchor_pins(request_id,anchor_id,hold_until) values(req,parent,null);
    end if;
    update public.relay_requests set sealed_continuation_binding=p_continuation_binding where id=req;
    if not public.validate_request_execution_fence_v71(req,owner,epoch,array['dispatch_started']) then
        raise exception 'EXECUTION_FENCE_LOST';
    end if;
    return true;
end;
$$;

create or replace function public.complete_continuation_context_v91(
    p_args jsonb, p_material_occurrences jsonb, p_anchor jsonb
) returns boolean language plpgsql security definer set search_path=public as $$
declare
    r public.relay_requests%rowtype;
    s public.relay_sessions%rowtype;
    req uuid := (p_args->>'p_request_id')::uuid;
    owner text := p_args->>'p_fence_owner';
    epoch bigint := (p_args->>'p_fence_epoch')::bigint;
    parent uuid := (p_anchor->>'parent_anchor_id')::uuid;
    valid boolean;
begin
    select * into r from public.relay_requests where id=req for update;
    if r.status='succeeded' and r.session_id=(p_args->>'p_session_id')::uuid
       and exists(select 1 from public.relay_continuation_anchors where source_request_id=req) then
        return true;
    end if;
    if not public.validate_request_execution_fence_v71(req,owner,epoch,array['result_stored']) then
        return false;
    end if;
    select * into s from public.relay_sessions where id=r.session_id for update;
    if r.session_id is distinct from (p_args->>'p_session_id')::uuid
       or r.sealed_continuation_binding->'store_upstream_state' is distinct from 'true'::jsonb
       or r.sealed_continuation_binding->>'active_anchor_id' is distinct from parent::text
       or r.sealed_continuation_binding->>'parent_head_fingerprint' is distinct from p_anchor->>'head_fingerprint'
       or s.context_policy is distinct from 'conversation' or coalesce(s.history_mode, 'cumulative') is distinct from 'cumulative'
       or s.history_version is distinct from r.expected_history_version
       or p_args->>'p_history_object_id' is null
       or nullif(btrim(p_anchor->>'upstream_handle'),'') is null
       or p_anchor->>'upstream_handle' is distinct from r.provider_response_id
       or r.provider_response_id is distinct from p_args->>'p_provider_response_id'
       or (p_anchor->>'covered_turn_count')::integer is distinct from r.expected_history_version+1
       or p_anchor->>'connection_spec_hash' is distinct from s.metadata->'_relay_route'->>'connection_spec_hash'
       or p_anchor->>'account_scope_hash' is distinct from s.metadata->'_relay_route'->>'account_scope_id'
       or coalesce(p_anchor->>'head_fingerprint','') !~ '^[0-9a-f]{64}$'
       or coalesce(p_anchor->>'turn_chain_digest','') !~ '^[0-9a-f]{64}$'
       or jsonb_typeof(p_anchor->'covered_material_bindings') is distinct from 'array'
       or p_anchor->>'expires_at' is null then
        raise exception 'CONTINUATION_ANCHOR_COMMIT_INVALID';
    end if;

    if r.placement_contract_version='material-placement/2' then
        valid := public.complete_material_request_v89(p_args,p_material_occurrences);
    else
        valid := public.complete_relay_request_v66(req,s.id,r.expected_history_version,
            p_args->>'p_history_object_id',p_args->>'p_result_object_id',p_args->>'p_output_object_id',
            p_args->'p_compact_result',p_args->>'p_provider_response_id',owner,epoch);
    end if;
    if not valid then return false; end if;

    insert into public.relay_continuation_anchors (
        id,source_request_id,tenant_id,conversation_hash,session_id,parent_anchor_id,
        history_version,head_fingerprint,turn_chain_digest,upstream_handle,
        provider,connection_id,account_scope_hash,connection_spec_hash,
        state,expires_at,covered_turn_count,covered_material_bindings,
        anchor_class,affinity_epoch,protocol,model
    ) values (
        req,req,r.tenant_id,r.conversation_hash,s.id,parent,
        r.expected_history_version+1,p_anchor->>'head_fingerprint',p_anchor->>'turn_chain_digest',p_anchor->>'upstream_handle',
        r.provider,r.connection_id,p_anchor->>'account_scope_hash',p_anchor->>'connection_spec_hash',
        'active',(p_anchor->>'expires_at')::timestamptz,(p_anchor->>'covered_turn_count')::integer,p_anchor->'covered_material_bindings',
        coalesce(p_anchor->>'anchor_class', 'immutable_tree_anchor'),
        nullif(p_anchor->>'affinity_epoch', '')::bigint,
        coalesce(p_anchor->>'protocol', s.metadata->'_relay_route'->>'protocol'),
        coalesce(p_anchor->>'model', r.model)
    );
    update public.relay_requests set produced_anchor_id = req where id = req;
    return true;
end;
$$;

create or replace function public.release_continuation_after_request_v91()
returns trigger language plpgsql security definer set search_path=public as $$
declare parent uuid := (new.sealed_continuation_binding->>'active_anchor_id')::uuid;
begin
    if parent is null then return new; end if;
    if new.provider_dispatch_state='committed'
       or (new.status='failed' and not coalesce((new.error->>'unknown_completion')::boolean,false)) then
        if new.status='failed' and new.error->>'_relay_invalid_continuation_anchor'=parent::text then
            update public.relay_continuation_anchors
               set state = 'invalidated',
                   invalidated_by_request_id = new.id,
                   invalidation_reason = coalesce(new.error->>'_relay_invalid_continuation_reason', 'upstream_anchor_rejected')
             where id = parent and session_id = new.session_id and state = 'active';
        end if;
        delete from public.relay_anchor_pins where request_id=new.id and anchor_id=parent;
    end if;
    -- Unknown completion or cancellation retains the pin until reconciliation.
    return new;
end;
$$;

create or replace function public.protect_continuation_pin_v91()
returns trigger language plpgsql security definer set search_path=public as $$
begin
    if exists(select 1 from public.relay_anchor_pins where anchor_id=old.id) then
        raise exception 'CONTINUATION_ANCHOR_PINNED';
    end if;
    return old;
end;
$$;

create or replace function public.sync_continuation_plan_columns_v91()
returns trigger language plpgsql security definer set search_path=public as $$
begin
    if new.continuation_plan_object_id is null then
        new.continuation_plan_object_id := new.metadata->'_relay_continuation'->>'plan_object_id';
    end if;
    if new.continuation_plan_hash is null then
        new.continuation_plan_hash := new.metadata->'_relay_continuation'->>'plan_hash';
    end if;
    return new;
end;
$$;

drop trigger if exists sync_continuation_plan_columns_v91 on public.relay_requests;
create trigger sync_continuation_plan_columns_v91
    before insert on public.relay_requests
    for each row execute function public.sync_continuation_plan_columns_v91();

drop trigger if exists release_continuation_after_request_v91 on public.relay_requests;
create trigger release_continuation_after_request_v91
    after update of status,provider_dispatch_state on public.relay_requests
    for each row execute function public.release_continuation_after_request_v91();

drop trigger if exists protect_continuation_pin_v91 on public.relay_continuation_anchors;
create trigger protect_continuation_pin_v91 before delete on public.relay_continuation_anchors
    for each row execute function public.protect_continuation_pin_v91();

revoke all on function public.seal_continuation_context_v91(jsonb,jsonb,jsonb,integer,jsonb,integer),
    public.complete_continuation_context_v91(jsonb,jsonb,jsonb),
    public.release_continuation_after_request_v91(),public.protect_continuation_pin_v91(),
    public.sync_continuation_plan_columns_v91()
    from public, anon, authenticated;
grant execute on function public.seal_continuation_context_v91(jsonb,jsonb,jsonb,integer,jsonb,integer),
    public.complete_continuation_context_v91(jsonb,jsonb,jsonb) to service_role;

notify pgrst, 'reload schema';
commit;
