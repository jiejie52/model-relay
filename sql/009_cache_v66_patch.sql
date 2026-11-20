-- Model Relay 6.6.0. Apply after 008_cache_v65_upgrade.sql.
alter table public.relay_sessions
    add column if not exists supply_group_id text,
    add column if not exists route_pin_state text not null default 'pinned'
        check (route_pin_state in ('pending_first_request_pin','pinned')),
    add column if not exists history_mode text not null default 'cumulative'
        check (history_mode in ('cumulative','ephemeral_turn')),
    add column if not exists sticky_scope text not null default 'session'
        check (sticky_scope in ('session','conversation_family')),
    add column if not exists frozen_instructions text,
    add column if not exists system_semantic_hash text,
    add column if not exists first_request_id uuid;

alter table public.relay_requests
    add column if not exists history_mode text not null default 'cumulative',
    add column if not exists cache_decision_reason text,
    add column if not exists payload_stripped_bytes bigint not null default 0,
    add column if not exists observed_cache_audit jsonb;

create or replace function public.accept_relay_request_v66(
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
    p_job_expires_at timestamptz,
    p_route_pin jsonb
)
returns setof public.relay_requests
language plpgsql
security definer
set search_path = public
as $$
declare
    s public.relay_sessions%rowtype;
    prior public.relay_requests%rowtype;
    selected jsonb;
    frozen_group jsonb;
begin
    select * into s from public.relay_sessions
     where id=p_session_id and tenant_id=p_tenant_id and conversation_hash=p_conversation_hash
       and expires_at > now() for update;
    if not found then raise exception 'SESSION_NOT_FOUND'; end if;
    select * into prior from public.relay_requests
     where session_id=p_session_id and idempotency_key=p_idempotency_key;
    if found then
        if prior.caller_intent_hash is distinct from p_caller_intent_hash then
            raise exception 'IDEMPOTENCY_CONFLICT';
        end if;
        return next prior;
        return;
    end if;
    if s.active_request_id is not null then raise exception 'SESSION_BUSY'; end if;
    selected := p_route_pin->'route';
    if selected is null or selected->>'connection_id' is distinct from p_connection_id
       or selected->>'provider' is distinct from p_provider
       or selected->>'model' is distinct from p_model
       or selected->>'execution_pool' is distinct from p_execution_pool then
        raise exception 'ROUTE_PIN_CONFLICT';
    end if;

    if s.route_pin_state='pending_first_request_pin' then
        frozen_group := s.metadata->'_relay_route_group';
        if s.first_request_id is not null or s.history_version<>0
           or frozen_group->>'group_hash' is distinct from p_route_pin->>'group_hash'
           or selected->>'supply_group_id' is distinct from s.supply_group_id
           or not exists(select 1 from jsonb_array_elements(frozen_group->'candidates') candidate
                          where candidate=selected) then
            raise exception 'ROUTE_GROUP_MISMATCH';
        end if;
        update public.relay_sessions
           set connection_id=p_connection_id, route_pin_state='pinned',
               session_affinity_token=p_route_pin->>'affinity_token',
               metadata=jsonb_set(metadata,'{_relay_route}',selected)
         where id=s.id;
    elsif s.connection_id is distinct from p_connection_id
          or s.metadata->'_relay_route' is distinct from selected then
        raise exception 'ROUTE_PIN_CONFLICT';
    end if;

    if s.first_request_id is null then
        update public.relay_sessions
           set first_request_id=p_request_id, frozen_instructions=p_route_pin->>'instructions',
               system_semantic_hash=p_route_pin->>'system_semantic_hash',
               session_affinity_token=p_route_pin->>'affinity_token'
         where id=s.id;
    end if;
    -- Pinning and acceptance share this transaction. Validation failures roll both back.
    perform 1 from public.accept_relay_request_v3(
    p_session_id,
    p_request_id,
    p_tenant_id,
    p_conversation_hash,
    p_idempotency_key,
    p_request_hash,
    p_caller_intent_hash,
    p_request_identity_version,
    p_execution_mode,
    p_request_object_id,
    p_context_plan_object_id,
    p_context_plan_hash,
    p_cache_plan_object_id,
    p_cache_plan_hash,
    p_cache_resolution_status,
    p_provider,
    p_connection_id,
    p_model,
    p_execution_pool,
    p_expected_history_version,
    p_metadata,
    p_job_id,
    p_job_expires_at
    );
    update public.relay_requests set history_mode=s.history_mode,
        cache_decision_reason=p_metadata->'_relay_cache'->>'decision_reason'
     where id=p_request_id;
    return query select * from public.relay_requests where id=p_request_id;
end;
$$;
revoke all on function public.accept_relay_request_v66(uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,bigint,jsonb,uuid,timestamptz,jsonb) from public, anon, authenticated;
grant execute on function public.accept_relay_request_v66(uuid,uuid,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,text,bigint,jsonb,uuid,timestamptz,jsonb) to service_role;

create or replace function public.complete_relay_request_v66(
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
    v_ephemeral boolean;
begin
    select * into v_request from public.relay_requests
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.provider_dispatch_state <> 'result_stored'
       or v_request.status in ('cancelled','succeeded','failed','indeterminate') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner is distinct from p_fence_owner or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=now() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null or v_request.request_executor_lease_expires_at <= now() then return false; end if;
    end if;

    v_ephemeral := v_request.history_mode='ephemeral_turn';
    update public.relay_sessions
       set history_object_id=case when v_ephemeral then history_object_id else coalesce(p_history_object_id,history_object_id) end,
           history_version=history_version+case when v_ephemeral then 0 else 1 end,
           active_request_id=null,
           updated_at=now()
     where id=p_session_id
       and active_request_id=p_request_id
       and history_version=p_expected_history_version;
    if not found then return false; end if;

    v_next_version := p_expected_history_version+case when v_ephemeral then 0 else 1 end;
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
revoke all on function public.complete_relay_request_v66(uuid,uuid,bigint,text,text,text,jsonb,text,text,bigint) from public, anon, authenticated;
grant execute on function public.complete_relay_request_v66(uuid,uuid,bigint,text,text,text,jsonb,text,text,bigint) to service_role;

create or replace function public.store_relay_result_v66(
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
        if not found or v_job.lease_owner is distinct from p_fence_owner or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=now() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null or v_request.request_executor_lease_expires_at <= now() then return false; end if;
    end if;
    update public.relay_requests
       set provider_dispatch_state='result_stored',
           provisional_result_object_id=p_result_object_id,
           provisional_output_object_id=p_output_object_id,
           provisional_history_object_id=p_history_object_id,
           provisional_compact_result=p_compact_result,
           provider_response_id=p_provider_response_id,
           cache_usage=p_cache_usage,
           observed_cache_audit=p_cache_usage->'observed_cache_audit',
           payload_stripped_bytes=coalesce((p_cache_usage->>'payload_stripped_bytes')::bigint,0),
           cache_decision_reason=coalesce(p_cache_usage->>'cache_decision_reason',cache_decision_reason)
     where id=p_request_id;
    return true;
end;
$$;
revoke all on function public.store_relay_result_v66(uuid,text,text,text,jsonb,text,jsonb,text,bigint) from public, anon, authenticated;
grant execute on function public.store_relay_result_v66(uuid,text,text,text,jsonb,text,jsonb,text,bigint) to service_role;
