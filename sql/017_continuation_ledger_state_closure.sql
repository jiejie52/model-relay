-- Model Relay 10.5.0. Apply after 016_continuation_execution_closure.sql.
-- Closes Anchor Ledger schema columns, three-state anchor pin lifecycle
-- (active/held/released), linear_ephemeral_cursor superseded transitions,
-- explicit anchor invalidation RPC, and 2.5 sync reconciliation parity.
begin;

alter table public.relay_continuation_anchors
    add column if not exists is_root_prefix boolean not null default true,
    add column if not exists covered_materials_digest text,
    add column if not exists updated_at timestamptz not null default clock_timestamp();

alter table public.relay_anchor_pins
    add column if not exists status text not null default 'active'
        check (status in ('active', 'held', 'released')),
    add column if not exists released_at timestamptz;

alter table public.relay_requests
    add column if not exists continuation_binding_version text,
    add column if not exists continuation_binding_hash text,
    add column if not exists parent_anchor_id uuid;

create index if not exists relay_continuation_prefix_v92
    on public.relay_continuation_anchors (session_id, head_fingerprint, covered_turn_count desc, history_version desc)
    where state = 'active' and source_request_id is not null and is_root_prefix;

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
           or coalesce(a.is_root_prefix, true) is distinct from true
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
        insert into public.relay_anchor_pins(request_id, anchor_id, status, hold_until, released_at)
        values(req, parent, 'active', null, null)
        on conflict (request_id, anchor_id) do update
            set status = 'active', hold_until = null, released_at = null;
    end if;
    update public.relay_requests
       set sealed_continuation_binding = p_continuation_binding,
           continuation_binding_version = p_continuation_binding->>'schema_version',
           continuation_binding_hash = p_continuation_binding->>'binding_hash',
           parent_anchor_id = parent
     where id = req;
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
    v_anchor_class text;
    v_anchor_id uuid;
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

    v_anchor_class := coalesce(
        nullif(p_anchor->>'anchor_class', ''),
        nullif(r.sealed_continuation_binding->>'anchor_class', ''),
        'immutable_tree_anchor'
    );
    if v_anchor_class = 'linear_ephemeral_cursor' then
        update public.relay_continuation_anchors
           set state = 'superseded',
               updated_at = clock_timestamp()
         where session_id = s.id
           and state = 'active';
    end if;

    insert into public.relay_continuation_anchors (
        id,source_request_id,tenant_id,conversation_hash,session_id,parent_anchor_id,
        history_version,head_fingerprint,turn_chain_digest,upstream_handle,
        provider,connection_id,account_scope_hash,connection_spec_hash,
        state,expires_at,covered_turn_count,covered_material_bindings,
        anchor_class,affinity_epoch,protocol,model,
        is_root_prefix,covered_materials_digest,updated_at
    ) values (
        req,req,r.tenant_id,r.conversation_hash,s.id,parent,
        r.expected_history_version+1,p_anchor->>'head_fingerprint',p_anchor->>'turn_chain_digest',p_anchor->>'upstream_handle',
        r.provider,r.connection_id,p_anchor->>'account_scope_hash',p_anchor->>'connection_spec_hash',
        'active',(p_anchor->>'expires_at')::timestamptz,(p_anchor->>'covered_turn_count')::integer,p_anchor->'covered_material_bindings',
        v_anchor_class,
        nullif(p_anchor->>'affinity_epoch', '')::bigint,
        coalesce(p_anchor->>'protocol', s.metadata->'_relay_route'->>'protocol'),
        coalesce(p_anchor->>'model', r.model),
        coalesce((p_anchor->>'is_root_prefix')::boolean, true),
        p_anchor->>'covered_materials_digest',
        clock_timestamp()
    )
    on conflict (source_request_id) do update
        set updated_at = clock_timestamp()
    returning id into v_anchor_id;

    update public.relay_requests set produced_anchor_id = v_anchor_id where id = req;

    update public.relay_anchor_pins
       set status = 'released',
           released_at = clock_timestamp()
     where request_id = req
       and status <> 'released';

    return true;
end;
$$;

create or replace function public.invalidate_continuation_anchor_v91(
    p_anchor_id uuid,
    p_session_id uuid,
    p_request_id uuid default null,
    p_reason text default 'upstream_anchor_rejected'
) returns boolean
language plpgsql security definer set search_path = public as $$
declare
    v_anchor public.relay_continuation_anchors%rowtype;
begin
    if p_anchor_id is null or p_session_id is null then
        return false;
    end if;

    select * into v_anchor
      from public.relay_continuation_anchors
     where id = p_anchor_id
       and session_id = p_session_id
     for update;

    if not found then
        return false;
    end if;

    if v_anchor.state = 'invalidated' then
        return true;
    end if;

    update public.relay_continuation_anchors
       set state = 'invalidated',
           invalidated_by_request_id = coalesce(p_request_id, invalidated_by_request_id),
           invalidation_reason = coalesce(nullif(btrim(p_reason), ''), invalidation_reason, 'upstream_anchor_rejected'),
           updated_at = clock_timestamp()
     where id = p_anchor_id
       and session_id = p_session_id
       and state = 'active';

    return true;
end;
$$;

create or replace function public.release_continuation_pins_v90(
    p_request_id uuid
) returns integer
language plpgsql security definer set search_path = public as $$
declare
    v_count integer;
begin
    update public.relay_anchor_pins
       set status = 'released',
           released_at = clock_timestamp()
     where request_id = p_request_id
       and status <> 'released';
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

create or replace function public.release_continuation_after_request_v91()
returns trigger language plpgsql security definer set search_path=public as $$
declare parent uuid := (new.sealed_continuation_binding->>'active_anchor_id')::uuid;
begin
    if parent is null then return new; end if;
    if new.provider_dispatch_state = 'committed'
       or (new.status = 'failed' and not coalesce((new.error->>'unknown_completion')::boolean, false)) then
        if new.status = 'failed' and new.error->>'_relay_invalid_continuation_anchor' = parent::text then
            update public.relay_continuation_anchors
               set state = 'invalidated',
                   invalidated_by_request_id = new.id,
                   invalidation_reason = coalesce(new.error->>'_relay_invalid_continuation_reason', 'upstream_anchor_rejected'),
                   updated_at = clock_timestamp()
             where id = parent and session_id = new.session_id and state = 'active';
        end if;
        update public.relay_anchor_pins
           set status = 'released',
               released_at = clock_timestamp()
         where request_id = new.id
           and anchor_id = parent
           and status <> 'released';
    elsif new.status = 'indeterminate'
       or (new.status = 'failed' and coalesce((new.error->>'unknown_completion')::boolean, false)) then
        update public.relay_anchor_pins
           set status = 'held',
               hold_until = coalesce(hold_until, clock_timestamp() + interval '24 hours')
         where request_id = new.id
           and anchor_id = parent
           and status = 'active';
    end if;
    return new;
end;
$$;

create or replace function public.protect_continuation_pin_v91()
returns trigger language plpgsql security definer set search_path=public as $$
begin
    if exists(
        select 1 from public.relay_anchor_pins
         where anchor_id = old.id
           and status in ('active', 'held')
    ) then
        raise exception 'CONTINUATION_ANCHOR_PINNED';
    end if;
    return old;
end;
$$;

create or replace function public.reconcile_relay_request(
    p_request_id uuid,p_tenant_id text,p_conversation_hash text,p_sync_timeout_seconds integer default 120
) returns boolean language plpgsql security definer set search_path=public as $$
declare r public.relay_requests%rowtype; j public.relay_jobs%rowtype; expired boolean;
begin
    select * into r from public.relay_requests where id=p_request_id and tenant_id=p_tenant_id
        and conversation_hash=p_conversation_hash for update;
    if not found or r.status not in ('queued','leased','running') or r.provider_dispatch_state<>'dispatch_started' then return false; end if;
    if r.job_id is not null then
        select * into j from public.relay_jobs where id=r.job_id for update;
        expired:=not found or j.status not in ('leased','running') or j.lease_expires_at is null
                 or j.lease_expires_at<=clock_timestamp();
    elsif r.request_identity_version in ('relay-request/2.3','relay-request/2.4','relay-request/2.5') then
        expired:=r.request_executor_lease_expires_at is null or r.request_executor_lease_expires_at<=clock_timestamp();
    else
        expired:=r.started_at is not null and r.started_at<clock_timestamp()-make_interval(secs=>greatest(p_sync_timeout_seconds,1));
    end if;
    if not expired then return false; end if;
    update public.relay_requests set status='indeterminate',completed_at=clock_timestamp(),
        error=jsonb_build_object('source','relay_recovery','exception_type','ExecutionIndeterminate',
        'message','Execution authority expired after the dispatch seal; Provider inference is not replayed') where id=r.id;
    if r.job_id is not null then update public.relay_jobs set status='indeterminate',completed_at=clock_timestamp() where id=r.job_id; end if;
    update public.relay_cache_pins set status='held',hold_until=null,released_at=null where request_id=r.id and status<>'released';
    update public.relay_anchor_pins set status='held',hold_until=null,released_at=null where request_id=r.id and status<>'released';
    return true;
end;
$$;

revoke all on function public.invalidate_continuation_anchor_v91(uuid, uuid, uuid, text) from public, anon, authenticated;
grant execute on function public.invalidate_continuation_anchor_v91(uuid, uuid, uuid, text) to service_role;

notify pgrst, 'reload schema';
commit;
