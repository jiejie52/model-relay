-- Model Relay 10.6.0. Apply after 017_continuation_ledger_state_closure.sql.
-- Closes mutable_session_container taint invalidation across indeterminate outcomes,
-- post-validation result_stored failures, post-dispatch cancellations, and interleaved
-- non-cumulative or stateless history advances, plus linear seal continuity checks.
begin;

create index if not exists relay_requests_session_taint_v92_idx
    on public.relay_requests (session_id, created_at desc)
    where status in ('indeterminate', 'failed', 'cancelled');

create or replace function public.seal_continuation_context_v91(
    p_args jsonb, p_material_occurrences jsonb, p_historical_bindings jsonb,
    p_safety_seconds integer, p_continuation_binding jsonb, p_anchor_safety_seconds integer
) returns boolean language plpgsql security definer set search_path=public as $$
declare
    r public.relay_requests%rowtype;
    s public.relay_sessions%rowtype;
    a public.relay_continuation_anchors%rowtype;
    req uuid := (p_args->>'p_request_id')::uuid;
    parent uuid := nullif(p_continuation_binding->>'active_anchor_id', '')::uuid;
    owner text := p_args->>'p_fence_owner';
    epoch bigint := (p_args->>'p_fence_epoch')::bigint;
    route jsonb;
    topology jsonb;
    mode text := p_continuation_binding->>'effective_mode';
    v_binding_anchor_class text := nullif(btrim(p_continuation_binding->>'anchor_class'), '');
    v_session_tainted boolean := false;
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

    if mode <> 'stateless_full_replay' and v_binding_anchor_class = 'mutable_session_container' then
        v_session_tainted := exists (
            select 1
              from public.relay_requests pr
             where pr.session_id = s.id
               and pr.id <> req
               and (
                   pr.status = 'indeterminate'
                   or (
                       pr.status in ('failed', 'cancelled')
                       and pr.provider_dispatch_state in ('dispatch_started', 'result_stored')
                       and pr.sealed_continuation_binding->>'anchor_class' = 'mutable_session_container'
                       and coalesce(pr.sealed_continuation_binding->'store_upstream_state' = 'true'::jsonb, false)
                   )
               )
        );
        if (mode = 'stateful_root_init' and coalesce(r.expected_history_version, 0) > 0)
           or v_session_tainted then
            raise exception 'CONTINUATION_PARENT_NO_LONGER_USABLE';
        end if;
    end if;

    if parent is not null then
        select * into a from public.relay_continuation_anchors where id=parent for update;
        if not found then
            raise exception 'CONTINUATION_PARENT_NO_LONGER_USABLE';
        end if;
        if a.anchor_class = 'mutable_session_container' and not v_session_tainted then
            v_session_tainted := exists (
                select 1
                  from public.relay_requests pr
                 where pr.session_id = s.id
                   and pr.id <> req
                   and (
                       pr.status = 'indeterminate'
                       or (
                           pr.status in ('failed', 'cancelled')
                           and pr.provider_dispatch_state in ('dispatch_started', 'result_stored')
                           and pr.sealed_continuation_binding->>'anchor_class' = 'mutable_session_container'
                           and coalesce(pr.sealed_continuation_binding->'store_upstream_state' = 'true'::jsonb, false)
                       )
                   )
            );
        end if;
        if a.state<>'active' or a.source_request_id is null
           or coalesce(a.is_root_prefix, true) is distinct from true
           or a.expires_at is null or a.expires_at<=clock_timestamp()+make_interval(secs=>p_anchor_safety_seconds)
           or a.history_version>r.expected_history_version
           or (
               v_binding_anchor_class is not null
               and a.anchor_class is not null
               and a.anchor_class is distinct from v_binding_anchor_class
           )
           or (
               coalesce(a.anchor_class, 'immutable_tree_anchor') in ('linear_ephemeral_cursor', 'mutable_session_container')
               and (
                   a.history_version is distinct from s.history_version
                   or a.history_version is distinct from r.expected_history_version
                   or a.covered_turn_count is distinct from r.expected_history_version
               )
           )
           or (a.anchor_class = 'mutable_session_container' and v_session_tainted)
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
    parent uuid := nullif(p_anchor->>'parent_anchor_id', '')::uuid;
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
    if v_anchor_class in ('linear_ephemeral_cursor', 'mutable_session_container') then
        update public.relay_continuation_anchors
           set state = 'superseded',
               invalidated_by_request_id = case
                   when state = 'invalidated'
                    and invalidated_by_request_id = req
                    and invalidation_reason = 'history_advanced_without_continuation_anchor'
                   then null
                   else invalidated_by_request_id
               end,
               invalidation_reason = case
                   when state = 'invalidated'
                    and invalidated_by_request_id = req
                    and invalidation_reason = 'history_advanced_without_continuation_anchor'
                   then null
                   else invalidation_reason
               end,
               updated_at = clock_timestamp()
         where session_id = s.id
           and source_request_id is distinct from req
           and (
               state = 'active'
               or (
                   state = 'invalidated'
                   and invalidated_by_request_id = req
                   and invalidation_reason = 'history_advanced_without_continuation_anchor'
               )
           );
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

create or replace function public.retire_linear_anchors_on_history_advance_v92()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    if new.history_version > old.history_version then
        update public.relay_continuation_anchors
           set state = 'invalidated',
               invalidated_by_request_id = coalesce(invalidated_by_request_id, old.active_request_id),
               invalidation_reason = coalesce(
                   nullif(invalidation_reason, ''),
                   'history_advanced_without_continuation_anchor'
               ),
               updated_at = clock_timestamp()
         where session_id = new.id
           and state = 'active'
           and anchor_class in ('linear_ephemeral_cursor', 'mutable_session_container')
           and history_version < new.history_version;
    end if;
    return new;
end;
$$;

drop trigger if exists retire_linear_anchors_on_history_advance_v92 on public.relay_sessions;
create trigger retire_linear_anchors_on_history_advance_v92
    after update of history_version on public.relay_sessions
    for each row execute function public.retire_linear_anchors_on_history_advance_v92();

create or replace function public.release_continuation_after_request_v91()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    parent uuid := nullif(new.sealed_continuation_binding->>'active_anchor_id', '')::uuid;
    v_binding_anchor_class text := nullif(btrim(new.sealed_continuation_binding->>'anchor_class'), '');
    v_store_upstream boolean := coalesce(new.sealed_continuation_binding->'store_upstream_state' = 'true'::jsonb, false);
    v_parent_anchor_class text;
    v_is_mutable_container boolean := false;
    v_crossed_dispatch boolean := new.provider_dispatch_state in ('dispatch_started', 'result_stored');
    v_unknown_completion boolean := case
        when jsonb_typeof(new.error->'unknown_completion') = 'boolean' then (new.error->>'unknown_completion')::boolean
        else lower(coalesce(new.error->>'unknown_completion', '')) = 'true'
    end;
    v_taint_reason text;
begin
    if parent is not null then
        select anchor_class into v_parent_anchor_class
          from public.relay_continuation_anchors
         where id = parent and session_id = new.session_id;
    end if;

    v_is_mutable_container := (
        v_binding_anchor_class = 'mutable_session_container'
        or v_parent_anchor_class = 'mutable_session_container'
    );

    if v_is_mutable_container
       and v_crossed_dispatch
       and (parent is not null or v_store_upstream)
       and new.status in ('failed', 'indeterminate', 'cancelled') then
        v_taint_reason := case
            when new.status = 'indeterminate' then 'mutable_container_indeterminate'
            when new.status = 'cancelled' then 'mutable_container_cancelled_after_dispatch'
            when new.provider_dispatch_state = 'result_stored' then 'mutable_container_post_validation_failed'
            else 'mutable_container_failed_after_dispatch'
        end;
        update public.relay_continuation_anchors
           set state = 'invalidated',
               invalidated_by_request_id = new.id,
               invalidation_reason = v_taint_reason,
               updated_at = clock_timestamp()
         where session_id = new.session_id
           and state = 'active'
           and (anchor_class = 'mutable_session_container' or id = parent);
    end if;

    if parent is null then
        return new;
    end if;

    if new.provider_dispatch_state = 'committed'
       or (new.status = 'failed' and not v_unknown_completion)
       or (new.status = 'cancelled' and not v_crossed_dispatch) then
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
       or (new.status = 'failed' and v_unknown_completion)
       or (new.status = 'cancelled' and v_crossed_dispatch) then
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

revoke all on function public.seal_continuation_context_v91(jsonb,jsonb,jsonb,integer,jsonb,integer),
    public.complete_continuation_context_v91(jsonb,jsonb,jsonb),
    public.release_continuation_after_request_v91(),
    public.retire_linear_anchors_on_history_advance_v92()
    from public, anon, authenticated;
grant execute on function public.seal_continuation_context_v91(jsonb,jsonb,jsonb,integer,jsonb,integer),
    public.complete_continuation_context_v91(jsonb,jsonb,jsonb) to service_role;

notify pgrst, 'reload schema';
commit;
