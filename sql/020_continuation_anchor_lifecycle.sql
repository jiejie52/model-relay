-- Model Relay 10.7.0. Forward-only migration after 019_continuation_schema_repair.sql.
-- 1. Enforces max_active_anchors_per_session quota eviction on commit while
--    protecting pinned anchors (relay_anchor_pins status in ('active', 'held')).
-- 2. Adds index and reap_continuation_anchors_v1 RPC for periodic expiry
--    marking and safe leaf-first pruning of unpinned terminal anchors.
begin;

create index if not exists idx_continuation_anchors_terminal_reap_v1
    on public.relay_continuation_anchors (state, updated_at)
    where state in ('expired', 'superseded', 'invalidated');

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
    v_max_active integer;
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

    -- Enforce per-session max_active_anchors_per_session quota while keeping the
    -- newest committed anchor and skipping any pinned anchors in relay_anchor_pins.
    v_max_active := greatest(
        1,
        coalesce(
            nullif(p_anchor->>'max_active_anchors_per_session', '')::integer,
            128
        )
    );
    with ranked as (
        select a.id,
               row_number() over (
                   order by
                       case when a.id = v_anchor_id then 0 else 1 end asc,
                       a.updated_at desc,
                       a.history_version desc,
                       a.created_at desc,
                       a.id asc
               ) as rn
          from public.relay_continuation_anchors a
         where a.session_id = s.id
           and a.state = 'active'
    )
    update public.relay_continuation_anchors a
       set state = 'superseded',
           invalidation_reason = coalesce(nullif(a.invalidation_reason, ''), 'session_active_anchor_quota_evicted'),
           updated_at = clock_timestamp()
      from ranked r_evict
     where a.id = r_evict.id
       and a.id <> v_anchor_id
       and r_evict.rn > v_max_active
       and a.state = 'active'
       and not exists (
           select 1 from public.relay_anchor_pins p
            where p.anchor_id = a.id
              and p.status in ('active', 'held')
       );

    return true;
end;
$$;

create or replace function public.reap_continuation_anchors_v1(
    p_delete_grace_seconds integer default 86400,
    p_batch_limit integer default 500
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_grace integer := greatest(0, coalesce(p_delete_grace_seconds, 86400));
    v_limit integer := least(5000, greatest(1, coalesce(p_batch_limit, 500)));
    v_expired_count integer := 0;
    v_deleted_count integer := 0;
begin
    with expired_targets as (
        select a.id
          from public.relay_continuation_anchors a
         where a.state = 'active'
           and a.expires_at is not null
           and a.expires_at <= clock_timestamp()
         order by a.expires_at asc, a.id asc
         limit v_limit
         for update skip locked
    ),
    updated as (
        update public.relay_continuation_anchors a
           set state = 'expired',
               invalidation_reason = coalesce(nullif(a.invalidation_reason, ''), 'anchor_expired'),
               updated_at = clock_timestamp()
          from expired_targets t
         where a.id = t.id
         returning a.id
    )
    select count(*)::integer into v_expired_count from updated;

    with delete_targets as (
        select a.id
          from public.relay_continuation_anchors a
         where a.state in ('expired', 'superseded', 'invalidated')
           and a.updated_at <= clock_timestamp() - make_interval(secs => v_grace)
           and not exists (
               select 1 from public.relay_anchor_pins p
                where p.anchor_id = a.id
                  and p.status in ('active', 'held')
           )
           and not exists (
               select 1 from public.relay_continuation_anchors child
                where child.parent_anchor_id = a.id
           )
           and not exists (
               select 1 from public.relay_requests rq
                where (rq.parent_anchor_id = a.id or rq.produced_anchor_id = a.id)
                  and rq.status in ('queued', 'leased', 'running', 'indeterminate')
           )
         order by a.updated_at asc, a.id asc
         limit v_limit
         for update skip locked
    ),
    deleted as (
        delete from public.relay_continuation_anchors a
         using delete_targets d
         where a.id = d.id
         returning a.id
    )
    select count(*)::integer into v_deleted_count from deleted;

    return jsonb_build_object(
        'expired_count', coalesce(v_expired_count, 0),
        'deleted_count', coalesce(v_deleted_count, 0)
    );
end;
$$;

revoke all on function public.complete_continuation_context_v91(jsonb, jsonb, jsonb),
    public.reap_continuation_anchors_v1(integer, integer)
    from public, anon, authenticated;

grant execute on function public.complete_continuation_context_v91(jsonb, jsonb, jsonb),
    public.reap_continuation_anchors_v1(integer, integer)
    to service_role;

notify pgrst, 'reload schema';
commit;
