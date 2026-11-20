-- Model Relay 7.1.0. Apply AFTER 010, with API and Worker upgraded together.
-- Immutable requests are not rewritten. Execution rights expire at database wall time.
begin;

alter table public.relay_cache_operations
    add column if not exists request_fence_owner text,
    add column if not exists request_fence_epoch bigint;

create or replace function public.validate_request_execution_fence_v71(
    p_request_id uuid, p_fence_owner text, p_fence_epoch bigint,
    p_allowed_states text[] default array['not_sent','dispatch_started','result_stored']::text[]
) returns boolean language plpgsql security definer set search_path=public as $$
declare r public.relay_requests%rowtype; j public.relay_jobs%rowtype;
begin
    if p_request_id is null or nullif(btrim(p_fence_owner),'') is null
       or p_fence_epoch is null or p_fence_epoch<=0 or p_allowed_states is null then return false; end if;
    select * into r from public.relay_requests where id=p_request_id for update;
    if not found or r.status not in ('queued','leased','running')
       or r.provider_dispatch_state is null or not (r.provider_dispatch_state=any(p_allowed_states)) then return false; end if;
    if r.job_id is not null then
        select * into j from public.relay_jobs where id=r.job_id for update;
        if not found or j.request_id is distinct from r.id or j.lease_owner is distinct from p_fence_owner
           or j.lease_epoch is distinct from p_fence_epoch or j.status not in ('leased','running')
           or j.lease_expires_at is null or j.lease_expires_at<=clock_timestamp() then return false; end if;
    elsif r.request_executor_id is distinct from p_fence_owner
          or r.request_executor_epoch is distinct from p_fence_epoch
          or r.request_executor_lease_expires_at is null
          or r.request_executor_lease_expires_at<=clock_timestamp() then return false;
    end if;
    return true;
end;
$$;

create or replace function public.validate_cache_operation_fence_v71(
    p_operation_id uuid,p_lease_owner text,p_lease_epoch bigint,p_states text[]
) returns boolean language plpgsql security definer set search_path=public as $$
declare op public.relay_cache_operations%rowtype; request_ref uuid;
begin
    if p_states is null then return false; end if;
    select request_id into request_ref from public.relay_cache_operations where op_id=p_operation_id;
    if request_ref is null then return false; end if;
    select * into op from public.relay_cache_operations where op_id=p_operation_id;
    if not public.validate_request_execution_fence_v71(request_ref,op.request_fence_owner,
        op.request_fence_epoch,array['not_sent']) then return false; end if;
    select * into op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or op.request_id is distinct from request_ref or nullif(btrim(p_lease_owner),'') is null
       or p_lease_epoch is null or p_lease_epoch<=0 or op.lease_owner is distinct from p_lease_owner
       or op.lease_epoch is distinct from p_lease_epoch or not (op.state=any(p_states))
       or op.lease_expires_at is null or op.lease_expires_at<=clock_timestamp() then return false; end if;
    return public.validate_request_execution_fence_v71(request_ref,op.request_fence_owner,op.request_fence_epoch,array['not_sent']);
end;
$$;

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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent' then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < clock_timestamp() then return false; end if;
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
    v_reason text;
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent' or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.cache_plan_hash is distinct from p_plan_hash then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < clock_timestamp() then return false; end if;
    end if;

    insert into public.relay_request_cache_bindings(
        request_id,binding_version,plan_hash,binding_hash,final_mechanism,
        resource_id,resource_generation,state,metadata
    ) values(
        p_request_id,p_binding_version,p_plan_hash,p_binding_hash,p_final_mechanism,
        p_resource_id,p_resource_generation,'installed',coalesce(p_metadata,'{}'::jsonb)
    ) on conflict (request_id,binding_version) do nothing;

    v_reason := coalesce(p_metadata->>'decision_reason','selected');
    update public.relay_requests
       set active_cache_binding_version=p_binding_version,
           cache_resolution_status='finalized',
           metadata=jsonb_set(
               coalesce(metadata,'{}'::jsonb),
               '{_relay_cache}',
               coalesce(metadata->'_relay_cache','{}'::jsonb) || jsonb_build_object(
                   'effective_cache_mechanism',p_final_mechanism,
                   'resolution_status','finalized',
                   'decision_reason',v_reason
               ),
               true
           )
     where id=p_request_id;
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
    v_bindings jsonb;
    v_item jsonb;
    v_resource_id uuid;
    v_resource_generation integer;
    v_has_binding_array boolean := false;
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
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
           or v_request.request_executor_lease_expires_at < clock_timestamp() then return false; end if;
    end if;

    if p_payload_hash is null or p_payload_hash !~ '^[0-9a-f]{64}$'
       or p_binding_hash is null or p_binding_hash !~ '^[0-9a-f]{64}$'
       or v_request.material_binding_snapshot is null
       or v_request.active_cache_binding_version is distinct from p_binding_version
       or v_binding.plan_hash is distinct from v_request.cache_plan_hash then return false; end if;
    v_bindings := v_binding.metadata->'cache_bindings';
    if v_bindings is not null and jsonb_typeof(v_bindings) <> 'array' then
        return false;
    end if;
    v_has_binding_array := coalesce(jsonb_typeof(v_bindings) = 'array', false);
    if v_has_binding_array then
        if v_binding.metadata->'primary_binding'->>'mechanism' is distinct from v_binding.final_mechanism then
            return false;
        end if;
        if v_binding.resource_id is not null and not exists(
            select 1
            from jsonb_array_elements(v_bindings) as item(value)
            where item.value->>'role' = 'primary'
              and item.value->>'mechanism' = 'stateful_resource'
              and item.value->>'resource_id' = v_binding.resource_id::text
        ) then
            return false;
        end if;
    end if;

    perform 1 from public.relay_cache_resources r
     where r.id=v_binding.resource_id or exists(
       select 1 from jsonb_array_elements(case when v_has_binding_array then v_bindings else '[]'::jsonb end) item
        where item->>'resource_id'=r.id::text)
     order by r.id for update;
    -- Validate every resource before writing any pin. This keeps the pin set
    -- atomic and prevents a partially pinned Request from reaching dispatch.
    if v_has_binding_array then
        for v_item in select value from jsonb_array_elements(v_bindings)
        loop
            if coalesce(v_item->>'active', 'false') = 'true'
               and v_item->>'mechanism' = 'stateful_resource' then
                begin
                    v_resource_id := nullif(v_item->>'resource_id', '')::uuid;
                    v_resource_generation := nullif(v_item->>'resource_generation', '')::integer;
                exception when others then
                    return false;
                end;
                if v_resource_id is null or v_resource_generation is null then
                    return false;
                end if;
                if not exists(
                    select 1 from public.relay_cache_resources r
                    where r.id=v_resource_id and r.session_id=v_request.session_id
                      and r.tenant_id=v_request.tenant_id and r.conversation_hash=v_request.conversation_hash
                      and r.connection_id=v_request.connection_id
                      and coalesce(
                            nullif(to_jsonb(r)->>'generation', '')::integer,
                            nullif(to_jsonb(r)->>'resource_generation', '')::integer
                          ) = v_resource_generation
                      and r.state='ready'
                      and (
                            coalesce(
                                nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                                nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                            ) is null
                            or coalesce(
                                nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                                nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                            ) > clock_timestamp()
                          )
                ) then
                    return false;
                end if;
            end if;
        end loop;
    elsif v_binding.resource_id is not null then
        -- Backward compatibility for binding/1 scalar records.
        if not exists(
            select 1 from public.relay_cache_resources r
            where r.id=v_binding.resource_id and r.session_id=v_request.session_id
              and r.tenant_id=v_request.tenant_id and r.conversation_hash=v_request.conversation_hash
              and r.connection_id=v_request.connection_id and r.state='ready'
              and (
                    coalesce(
                        nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                        nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                    ) is null
                    or coalesce(
                        nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                        nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                    ) > clock_timestamp()
                  )
              and (
                    v_binding.resource_generation is null
                    or coalesce(
                        nullif(to_jsonb(r)->>'generation', '')::integer,
                        nullif(to_jsonb(r)->>'resource_generation', '')::integer
                    ) = v_binding.resource_generation
                  )
        ) then
            return false;
        end if;
    end if;

    if v_has_binding_array then
        for v_item in select value from jsonb_array_elements(v_bindings)
        loop
            if coalesce(v_item->>'active', 'false') = 'true'
               and v_item->>'mechanism' = 'stateful_resource' then
                v_resource_id := (v_item->>'resource_id')::uuid;
                insert into public.relay_cache_pins(
                    resource_id,request_id,binding_version,execution_fence,status
                ) values(
                    v_resource_id,p_request_id,p_binding_version,p_fence_owner||':'||p_fence_epoch::text,'active'
                ) on conflict (resource_id,request_id,binding_version)
                  do update set status='active', released_at=null;
                update public.relay_cache_resources set last_used_at=clock_timestamp() where id=v_resource_id;
            end if;
        end loop;
    elsif v_binding.resource_id is not null then
        insert into public.relay_cache_pins(resource_id,request_id,binding_version,execution_fence,status)
        values(v_binding.resource_id,p_request_id,p_binding_version,p_fence_owner||':'||p_fence_epoch::text,'active')
        on conflict (resource_id,request_id,binding_version) do update set status='active', released_at=null;
        update public.relay_cache_resources set last_used_at=clock_timestamp() where id=v_binding.resource_id;
    end if;

    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then raise exception 'EXECUTION_FENCE_LOST'; end if;
    update public.relay_request_cache_bindings
       set state='sealed', prepared_payload_hash=p_payload_hash, sealed_at=clock_timestamp()
     where request_id=p_request_id and binding_version=p_binding_version;
    update public.relay_requests
       set prepared_payload_hash=p_payload_hash,
           provider_dispatch_state='dispatch_started',
           started_at=coalesce(started_at,clock_timestamp())
     where id=p_request_id;
    return true;
end;
$$;

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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['dispatch_started']) then return false; end if;
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'dispatch_started'
       or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < clock_timestamp() then return false; end if;
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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['dispatch_started']) then return false; end if;
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'dispatch_started'
       or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner is distinct from p_fence_owner or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=clock_timestamp() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null or v_request.request_executor_lease_expires_at <= clock_timestamp() then return false; end if;
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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['result_stored']) then return false; end if;
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
           or v_request.request_executor_lease_expires_at < clock_timestamp() then return false; end if;
    end if;

    update public.relay_sessions
       set history_object_id=coalesce(p_history_object_id,history_object_id),
           history_version=history_version+1,
           active_request_id=null,
           updated_at=clock_timestamp()
     where id=p_session_id
       and active_request_id=p_request_id
       and history_version=p_expected_history_version
       and public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['result_stored']);
    if not found then return false; end if;

    v_next_version := p_expected_history_version+1;
    update public.relay_requests
       set status='succeeded', provider_dispatch_state='committed',
           result_object_id=p_result_object_id, output_object_id=p_output_object_id,
           compact_result=p_compact_result, provider_response_id=p_provider_response_id,
           completed_history_version=v_next_version, completed_at=clock_timestamp(), error=null
     where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status='succeeded', compact_result=p_compact_result, completed_at=clock_timestamp(),
               heartbeat_at=clock_timestamp(), error_code=null, error_message=null
         where id=v_request.job_id;
    end if;
    update public.relay_cache_pins
       set status='released', released_at=clock_timestamp(), hold_until=null
     where request_id=p_request_id and status<>'released';
    return true;
end;
$$;

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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['result_stored']) then return false; end if;
    select * into v_request from public.relay_requests
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.provider_dispatch_state <> 'result_stored'
       or v_request.status in ('cancelled','succeeded','failed','indeterminate') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner is distinct from p_fence_owner or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=clock_timestamp() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null or v_request.request_executor_lease_expires_at <= clock_timestamp() then return false; end if;
    end if;

    v_ephemeral := v_request.history_mode='ephemeral_turn';
    update public.relay_sessions
       set history_object_id=case when v_ephemeral then history_object_id else coalesce(p_history_object_id,history_object_id) end,
           history_version=history_version+case when v_ephemeral then 0 else 1 end,
           active_request_id=null,
           updated_at=clock_timestamp()
     where id=p_session_id
       and active_request_id=p_request_id
       and history_version=p_expected_history_version
       and public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['result_stored']);
    if not found then return false; end if;

    v_next_version := p_expected_history_version+case when v_ephemeral then 0 else 1 end;
    update public.relay_requests
       set status='succeeded', provider_dispatch_state='committed',
           result_object_id=p_result_object_id, output_object_id=p_output_object_id,
           compact_result=p_compact_result, provider_response_id=p_provider_response_id,
           completed_history_version=v_next_version, completed_at=clock_timestamp(), error=null
     where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status='succeeded', compact_result=p_compact_result, completed_at=clock_timestamp(),
               heartbeat_at=clock_timestamp(), error_code=null, error_message=null
         where id=v_request.job_id;
    end if;
    update public.relay_cache_pins
       set status='released', released_at=clock_timestamp(), hold_until=null
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
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent','dispatch_started','result_stored']) then return false; end if;
    if p_status not in ('failed','indeterminate') then return false; end if;
    select * into v_request from public.relay_requests
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.status in ('succeeded','cancelled','failed','indeterminate') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner is distinct from p_fence_owner
           or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=clock_timestamp() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner
           or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null
           or v_request.request_executor_lease_expires_at<=clock_timestamp() then return false; end if;
    end if;

    update public.relay_requests set status=p_status,error=p_error,completed_at=clock_timestamp() where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status=p_status,completed_at=clock_timestamp(),heartbeat_at=clock_timestamp(),error_code=null,error_message=null
         where id=v_request.job_id;
    end if;
    if p_release_session then
        update public.relay_sessions set active_request_id=null,updated_at=clock_timestamp()
         where id=p_session_id and active_request_id=p_request_id;
    end if;
    update public.relay_cache_pins
       set status=case when p_status='indeterminate' or p_hold_pins_until is not null then 'held' else 'released' end,
           hold_until=p_hold_pins_until,
           released_at=case when p_status<>'indeterminate' and p_hold_pins_until is null then clock_timestamp() else null end
     where request_id=p_request_id and status<>'released';
    return true;
end;
$$;

create or replace function public.renew_relay_job_lease_v2(
    p_job_id uuid,
    p_worker_id text,
    p_lease_epoch bigint,
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
       set lease_expires_at = clock_timestamp() + make_interval(secs => greatest(p_lease_seconds, 30)),
           heartbeat_at = clock_timestamp()
     where id = p_job_id
       and lease_owner = p_worker_id
       and lease_epoch = p_lease_epoch
       and status in ('leased', 'running')
       and lease_expires_at is not null and lease_expires_at>clock_timestamp();
    get diagnostics v_count = row_count;
    return v_count = 1;
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
       set request_executor_lease_expires_at = clock_timestamp() + make_interval(secs => greatest(p_lease_seconds,30))
     where id=p_request_id
       and request_executor_id=p_executor_id
       and request_executor_epoch=p_executor_epoch
       and status='running'
       and request_executor_lease_expires_at is not null and request_executor_lease_expires_at>clock_timestamp();
    get diagnostics v_count = row_count;
    return v_count=1;
end;
$$;

create or replace function public.claim_cache_operation_v71(
    p_request_id uuid,
    p_scope_hash text,
    p_content_fingerprint text,
    p_operation_type text,
    p_idempotency_key text,
    p_lease_owner text,
    p_request_fence_epoch bigint,
    p_lease_seconds integer default 60
)
returns setof public.relay_cache_operations
language plpgsql
security definer
set search_path = public
as $$
declare
    v_op public.relay_cache_operations%rowtype;
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_lease_owner,p_request_fence_epoch,array['not_sent']) then return; end if;
    insert into public.relay_cache_operations(
        scope_hash,content_fingerprint,operation_type,request_id,idempotency_key,
        lease_owner,lease_epoch,lease_expires_at,state,attempt_no,request_fence_owner,request_fence_epoch
    ) values(
        p_scope_hash,p_content_fingerprint,p_operation_type,p_request_id,p_idempotency_key,
        p_lease_owner,1,clock_timestamp()+make_interval(secs=>greatest(p_lease_seconds,15)),'leased',1,p_lease_owner,p_request_fence_epoch
    ) on conflict(scope_hash,content_fingerprint,operation_type,idempotency_key) do nothing;

    select * into v_op from public.relay_cache_operations
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint
       and operation_type=p_operation_type and idempotency_key=p_idempotency_key for update;
    if not found then return; end if;

    if not public.validate_request_execution_fence_v71(p_request_id,p_lease_owner,p_request_fence_epoch,array['not_sent']) then raise exception 'EXECUTION_FENCE_LOST'; end if;
    -- running means the Provider operation has crossed its dispatch fence. If
    -- that lease expires the external side effect is unknown and MUST NOT be
    -- replayed. A reconciler may inspect raw_result/provider request id later.
    if v_op.state='running' then
        if v_op.lease_expires_at is null or v_op.lease_expires_at < clock_timestamp() then
            update public.relay_cache_operations
               set state='unknown', lease_expires_at=null, updated_at=clock_timestamp()
             where op_id=v_op.op_id and state='running';
        end if;
        return;
    end if;

    if v_op.state in ('unknown','succeeded','cancelled') then
        return;
    end if;

    update public.relay_cache_operations
       set request_id=p_request_id, request_fence_owner=p_lease_owner,request_fence_epoch=p_request_fence_epoch,
           lease_owner=p_lease_owner,
           lease_epoch=lease_epoch+case
               when state='failed' or lease_owner is distinct from p_lease_owner or lease_expires_at<clock_timestamp() then 1
               else 0 end,
           lease_expires_at=clock_timestamp()+make_interval(secs=>greatest(p_lease_seconds,15)),
           state='leased',
           attempt_no=attempt_no+case when state in ('planned','failed') or lease_expires_at<clock_timestamp() then 1 else 0 end,
           updated_at=clock_timestamp()
     where op_id=v_op.op_id
       and (
           state in ('planned','failed')
           or (state='leased' and lease_expires_at<clock_timestamp())
           or (state='leased' and lease_owner=p_lease_owner)
       );

    return query
      select * from public.relay_cache_operations
       where op_id=v_op.op_id and lease_owner=p_lease_owner and state='leased';
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
declare r public.relay_requests%rowtype; e bigint;
begin
    select * into r from public.relay_requests where id=p_request_id;
    if r.job_id is not null then select lease_epoch into e from public.relay_jobs where id=r.job_id;
    else e:=r.request_executor_epoch; end if;
    return query select * from public.claim_cache_operation_v71(p_request_id,p_scope_hash,p_content_fingerprint,
        p_operation_type,p_idempotency_key,p_lease_owner,e,p_lease_seconds);
end;
$$;

create or replace function public.start_cache_operation_v31(
    p_operation_id uuid,
    p_lease_owner text,
    p_lease_epoch bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.validate_cache_operation_fence_v71(p_operation_id,p_lease_owner,p_lease_epoch,array['leased']) then return false; end if;
    update public.relay_cache_operations
       set state='running', updated_at=clock_timestamp()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state='leased'
       and lease_expires_at >= clock_timestamp();
    return found;
end;
$$;

create or replace function public.record_cache_operation_observation_v31(
    p_operation_id uuid,
    p_lease_owner text,
    p_lease_epoch bigint,
    p_raw_result jsonb,
    p_provider_request_id text default null
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.validate_cache_operation_fence_v71(p_operation_id,p_lease_owner,p_lease_epoch,array['running']) then return false; end if;
    update public.relay_cache_operations
       set raw_result=coalesce(p_raw_result,'{}'::jsonb),
           provider_request_id=coalesce(p_provider_request_id,provider_request_id),
           updated_at=clock_timestamp()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state='running'
       and lease_expires_at >= clock_timestamp();
    return found;
end;
$$;

create or replace function public.finish_cache_operation_v31(
    p_operation_id uuid,
    p_lease_owner text,
    p_lease_epoch bigint,
    p_state text,
    p_raw_result jsonb default '{}'::jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.validate_cache_operation_fence_v71(p_operation_id,p_lease_owner,p_lease_epoch,array['leased','running']) then return false; end if;
    if p_state not in ('failed','unknown','cancelled') then
        return false;
    end if;
    update public.relay_cache_operations
       set state=p_state,
           raw_result=coalesce(p_raw_result,'{}'::jsonb),
           lease_expires_at=null,
           updated_at=clock_timestamp()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state in ('leased','running');
    return found;
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
    if not public.validate_cache_operation_fence_v71(p_operation_id,p_lease_owner,p_lease_epoch,array['running']) then return; end if;
    select * into v_op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or v_op.lease_owner<>p_lease_owner or v_op.lease_epoch<>p_lease_epoch
       or v_op.state not in ('leased','running') then return; end if;

    if v_op.scope_hash is distinct from p_scope_hash or v_op.content_fingerprint is distinct from p_content_fingerprint
       or not exists(select 1 from public.relay_requests r where r.id=v_op.request_id
          and r.tenant_id=p_tenant_id and r.conversation_hash=p_conversation_hash
          and r.session_id=p_session_id and r.connection_id=p_connection_id
          and coalesce(r.metadata->>'offering_id',p_offering_id) is not distinct from p_offering_id)
       then return; end if;
    select coalesce(max(generation),0)+1 into v_generation from public.relay_cache_resources
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint;
    insert into public.relay_cache_resources(
        tenant_id,conversation_hash,session_id,offering_id,connection_id,
        scope_hash,profile_hash,content_fingerprint,generation,state,
        provider_handle_ref,expire_time,operation_epoch,active_creator,last_used_at,metadata
    ) values(
        p_tenant_id,p_conversation_hash,p_session_id,p_offering_id,p_connection_id,
        p_scope_hash,p_profile_hash,p_content_fingerprint,v_generation,'ready',
        p_provider_handle_ref,p_expire_time,p_lease_epoch,p_lease_owner,clock_timestamp(),
        jsonb_build_object('create_operation_id',p_operation_id)
    ) returning id into v_resource_id;
    update public.relay_cache_operations set state='succeeded',resource_id=v_resource_id,raw_result=p_raw_result,updated_at=clock_timestamp()
     where op_id=p_operation_id;
    return query select * from public.relay_cache_resources where id=v_resource_id;
end;
$$;

create or replace function public.publish_cache_resource_v31(
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
    p_reuse_key text,
    p_prefix_version bigint,
    p_token_count bigint,
    p_spec_hash text,
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
    if not public.validate_cache_operation_fence_v71(p_operation_id,p_lease_owner,p_lease_epoch,array['running']) then return; end if;
    select * into v_op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or v_op.lease_owner<>p_lease_owner or v_op.lease_epoch<>p_lease_epoch
       or v_op.state <> 'running' or v_op.lease_expires_at is null or v_op.lease_expires_at < clock_timestamp() then return; end if;
    if p_provider_handle_ref is null or p_provider_handle_ref !~ '^cachedContents/[A-Za-z0-9._~-]+$' then
        return;
    end if;

    if v_op.scope_hash is distinct from p_scope_hash or v_op.content_fingerprint is distinct from p_content_fingerprint
       or not exists(select 1 from public.relay_requests r where r.id=v_op.request_id
          and r.tenant_id=p_tenant_id and r.conversation_hash=p_conversation_hash
          and r.session_id=p_session_id and r.connection_id=p_connection_id
          and coalesce(r.metadata->>'offering_id',p_offering_id) is not distinct from p_offering_id)
       then return; end if;
    select coalesce(max(generation),0)+1 into v_generation from public.relay_cache_resources
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint;

    insert into public.relay_cache_resources(
        tenant_id,conversation_hash,session_id,offering_id,connection_id,
        scope_hash,profile_hash,content_fingerprint,reuse_key,prefix_version,
        token_count,spec_hash,generation,state,provider_handle_ref,expire_time,
        operation_epoch,active_creator,last_used_at,metadata
    ) values(
        p_tenant_id,p_conversation_hash,p_session_id,p_offering_id,p_connection_id,
        p_scope_hash,p_profile_hash,p_content_fingerprint,p_reuse_key,p_prefix_version,
        p_token_count,p_spec_hash,v_generation,'ready',p_provider_handle_ref,p_expire_time,
        p_lease_epoch,p_lease_owner,clock_timestamp(),
        jsonb_build_object(
            'create_operation_id',p_operation_id,
            'prefix_version',p_prefix_version,
            'token_count',p_token_count
        )
    ) returning id into v_resource_id;

    update public.relay_cache_operations
       set state='succeeded',resource_id=v_resource_id,raw_result=p_raw_result,
           lease_expires_at=null,updated_at=clock_timestamp()
     where op_id=p_operation_id;

    return query select * from public.relay_cache_resources where id=v_resource_id;
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
    elsif r.request_identity_version in ('relay-request/2.3','relay-request/2.4') then
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
    return true;
end;
$$;

create or replace function public.claim_relay_job_v2(
    p_worker_id text,p_execution_pools text[],p_lease_seconds integer default 120
) returns setof public.relay_jobs language plpgsql security definer set search_path=public as $$
declare stale record; v_job_id uuid;
begin
    if nullif(btrim(p_worker_id),'') is null or p_execution_pools is null then return; end if;
    -- Resolve possible sends through the same authoritative Request -> Job
    -- lock order as seal/store/commit. A null lease is also lost authority.
    for stale in select r.id,r.tenant_id,r.conversation_hash
        from public.relay_requests r join public.relay_jobs j on j.request_id=r.id
        where j.execution_pool=any(p_execution_pools) and j.status in ('leased','running')
          and (j.lease_expires_at is null or j.lease_expires_at<=clock_timestamp())
          and r.status in ('queued','leased','running') and r.provider_dispatch_state='dispatch_started'
        order by r.id
    loop
        perform public.reconcile_relay_request(stale.id,stale.tenant_id,stale.conversation_hash);
    end loop;
    select j.id into v_job_id from public.relay_jobs j
      left join public.relay_requests r on r.id=j.request_id
     where j.expires_at>clock_timestamp() and j.execution_pool=any(p_execution_pools)
       and (j.request_id is null or (r.status in ('queued','leased','running')
            and r.provider_dispatch_state in ('not_sent','result_stored')))
       and (j.status='queued' or (j.status in ('leased','running')
            and (j.lease_expires_at is null or j.lease_expires_at<=clock_timestamp())))
     order by j.created_at for update of j skip locked limit 1;
    if v_job_id is null then return; end if;
    return query update public.relay_jobs set status='leased',lease_owner=p_worker_id,
        lease_epoch=lease_epoch+1,
        lease_expires_at=clock_timestamp()+make_interval(secs=>greatest(p_lease_seconds,30)),
        heartbeat_at=clock_timestamp(),attempt_count=attempt_count+1
      where id=v_job_id returning *;
end;
$$;

create or replace function public.claim_cache_reconciliation_v71(
    p_operation_id uuid,p_request_id uuid,p_fence_owner text,p_fence_epoch bigint
) returns setof public.relay_cache_operations language plpgsql security definer set search_path=public as $$
declare op public.relay_cache_operations%rowtype; r public.relay_requests%rowtype; original_session uuid;
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return; end if;
    select * into r from public.relay_requests where id=p_request_id;
    select * into op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or op.operation_type<>'create' or op.state not in ('unknown','running')
       or op.state='running' and op.lease_expires_at>clock_timestamp()
       or coalesce(op.raw_result->>'handle','') !~ '^cachedContents/[A-Za-z0-9._~-]+$' then return; end if;
    select session_id into original_session from public.relay_requests where id=op.request_id;
    if original_session is distinct from r.session_id then return; end if;
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return; end if;
    update public.relay_cache_operations set request_id=p_request_id,
        request_fence_owner=p_fence_owner,request_fence_epoch=p_fence_epoch,
        lease_owner=p_fence_owner,lease_epoch=lease_epoch+1,
        lease_expires_at=clock_timestamp()+interval '90 seconds',state='running',
        updated_at=clock_timestamp() where op_id=p_operation_id;
    return query select * from public.relay_cache_operations where op_id=p_operation_id;
end;
$$;

create or replace function public.complete_relay_request(
    p_request_id uuid,
    p_session_id uuid,
    p_expected_history_version bigint,
    p_history_object_id text,
    p_result_object_id text,
    p_output_object_id text,
    p_compact_result jsonb,
    p_provider_response_id text,
    p_lease_owner text default null,
    p_lease_epoch bigint default null
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
    if (p_lease_owner is not null or exists(select 1 from public.relay_requests where id=p_request_id and (job_id is not null or request_identity_version in ('relay-request/2.3','relay-request/2.4')))) and not public.validate_request_execution_fence_v71(p_request_id,p_lease_owner,p_lease_epoch,array['result_stored']) then return false; end if;
    select * into v_request from public.relay_requests
     where id = p_request_id and session_id = p_session_id for update;
    if not found or v_request.status in ('cancelled', 'succeeded', 'failed', 'indeterminate') then
        return false;
    end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id = v_request.job_id for update;
        if not found then return false; end if;
        if p_lease_owner is null or p_lease_epoch is null
           or v_job.lease_owner <> p_lease_owner
           or v_job.lease_epoch <> p_lease_epoch
           or v_job.status not in ('leased', 'running') then
            return false;
        end if;
    end if;

    update public.relay_sessions
       set history_object_id = coalesce(p_history_object_id, history_object_id),
           history_version = history_version + 1,
           active_request_id = null,
           updated_at = clock_timestamp()
     where id = p_session_id
       and active_request_id = p_request_id
       and history_version = p_expected_history_version and (p_lease_owner is null and v_request.job_id is null or public.validate_request_execution_fence_v71(p_request_id,p_lease_owner,p_lease_epoch,array['result_stored']));
    if not found then
        return false;
    end if;

    v_next_version := p_expected_history_version + 1;
    update public.relay_requests
       set status = 'succeeded',
           provider_dispatch_state = 'committed',
           result_object_id = p_result_object_id,
           output_object_id = p_output_object_id,
           compact_result = p_compact_result,
           provider_response_id = p_provider_response_id,
           completed_history_version = v_next_version,
           completed_at = clock_timestamp(),
           error = null
     where id = p_request_id;

    if v_request.job_id is not null then
        update public.relay_jobs
           set status = 'succeeded',
               compact_result = p_compact_result,
               completed_at = clock_timestamp(),
               heartbeat_at = clock_timestamp(),
               error_code = null,
               error_message = null
         where id = v_request.job_id;
    end if;
    return true;
end;
$$;

create or replace function public.fail_relay_request(
    p_request_id uuid,
    p_session_id uuid,
    p_status text,
    p_error jsonb,
    p_release_session boolean,
    p_lease_owner text default null,
    p_lease_epoch bigint default null
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
    if (p_lease_owner is not null or exists(select 1 from public.relay_requests where id=p_request_id and (job_id is not null or request_identity_version in ('relay-request/2.3','relay-request/2.4')))) and not public.validate_request_execution_fence_v71(p_request_id,p_lease_owner,p_lease_epoch,array['not_sent','dispatch_started','result_stored']) then return false; end if;
    if p_status not in ('failed', 'indeterminate') then return false; end if;
    select * into v_request from public.relay_requests
     where id = p_request_id and session_id = p_session_id for update;
    if not found or v_request.status in ('succeeded', 'cancelled') then return false; end if;

    if v_request.job_id is not null and p_lease_owner is not null then
        select * into v_job from public.relay_jobs where id = v_request.job_id for update;
        if not found or p_lease_epoch is null
           or v_job.lease_owner <> p_lease_owner or v_job.lease_epoch <> p_lease_epoch then
            return false;
        end if;
    end if;

    update public.relay_requests
       set status = p_status, error = p_error, completed_at = clock_timestamp()
     where id = p_request_id;

    if v_request.job_id is not null then
        update public.relay_jobs
           set status = p_status, completed_at = clock_timestamp(), heartbeat_at = clock_timestamp(),
               error_code = null, error_message = null
         where id = v_request.job_id;
    end if;

    if p_release_session then
        update public.relay_sessions
           set active_request_id = null, updated_at = clock_timestamp()
         where id = p_session_id and active_request_id = p_request_id;
    end if;
    return true;
end;
$$;

create or replace function public.start_request_execution_v71(
    p_request_id uuid,p_fence_owner text,p_fence_epoch bigint
) returns boolean language plpgsql security definer set search_path=public as $$
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent','result_stored']) then return false; end if;
    update public.relay_requests set status='running' where id=p_request_id;
    update public.relay_jobs set status='running',started_at=coalesce(started_at,clock_timestamp())
     where request_id=p_request_id;
    return true;
end;
$$;

create or replace function public.mark_request_dispatch_v71(
    p_request_id uuid,p_fence_owner text,p_fence_epoch bigint,p_payload_hash text
) returns boolean language plpgsql security definer set search_path=public as $$
begin
    if p_payload_hash is null or p_payload_hash !~ '^[0-9a-f]{64}$'
       or not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
    update public.relay_requests set provider_dispatch_state='dispatch_started',
        prepared_payload_hash=p_payload_hash,started_at=coalesce(started_at,clock_timestamp()) where id=p_request_id;
    return true;
end;
$$;

create or replace function public.invalidate_cache_resource_v71(
    p_resource_id uuid,p_provider_handle_ref text,p_state text,
    p_request_id uuid,p_fence_owner text,p_fence_epoch bigint
) returns boolean language plpgsql security definer set search_path=public as $$
begin
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
    perform 1 from public.relay_cache_resources where id=p_resource_id for update;
    if not public.validate_request_execution_fence_v71(p_request_id,p_fence_owner,p_fence_epoch,array['not_sent']) then return false; end if;
    if not exists(select 1 from public.relay_cache_resources c join public.relay_requests r on r.id=p_request_id
       where c.id=p_resource_id and c.session_id=r.session_id and c.tenant_id=r.tenant_id
         and c.conversation_hash=r.conversation_hash and c.connection_id=r.connection_id) then return false; end if;
    return public.invalidate_cache_resource_v31(p_resource_id,p_provider_handle_ref,p_state);
end;
$$;

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
    if nullif(btrim(p_executor_id),'') is null then return null; end if;
    update public.relay_requests
       set request_executor_id = p_executor_id,
           request_executor_epoch = request_executor_epoch + 1,
           request_executor_lease_expires_at = clock_timestamp() + make_interval(secs => greatest(p_lease_seconds,30))
     where id = p_request_id
       and execution_mode = 'sync'
       and status = 'running'
       and provider_dispatch_state = 'not_sent'
       and (
            request_executor_id is null
            or request_executor_lease_expires_at is null
            or request_executor_lease_expires_at < clock_timestamp()
            or request_executor_id = p_executor_id
       )
     returning request_executor_epoch into v_epoch;
    return v_epoch;
end;
$$;

create or replace function public.acquire_sync_commit_fence(
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
    if nullif(btrim(p_executor_id),'') is null then return null; end if;
    update public.relay_requests
       set request_executor_id = p_executor_id,
           request_executor_epoch = request_executor_epoch + 1,
           request_executor_lease_expires_at = clock_timestamp() + make_interval(secs => greatest(p_lease_seconds,30))
     where id = p_request_id
       and execution_mode = 'sync'
       and job_id is null
       and status = 'running'
       and provider_dispatch_state = 'result_stored'
       and provisional_result_object_id is not null
       and (
           request_executor_id is null
           or request_executor_lease_expires_at is null
           or request_executor_lease_expires_at < clock_timestamp()
           or request_executor_id = p_executor_id
       )
     returning request_executor_epoch into v_epoch;
    return v_epoch;
end;
$$;

create or replace function public.release_sync_commit_fence_v71(
    p_request_id uuid,p_executor_id text,p_executor_epoch bigint
) returns boolean language plpgsql security definer set search_path=public as $$
begin
    update public.relay_requests set request_executor_id=null,
        request_executor_lease_expires_at=null
     where id=p_request_id and execution_mode='sync' and status='running'
       and provider_dispatch_state='result_stored'
       and request_executor_id=p_executor_id and request_executor_epoch=p_executor_epoch;
    return found;
end;
$$;

do $$ declare f record; begin
 for f in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname in ('validate_request_execution_fence_v71','validate_cache_operation_fence_v71','install_request_material_binding_v3','install_cache_binding_v3','seal_cache_and_dispatch_v3','store_relay_result_v3','store_relay_result_v66','complete_relay_request_v3','complete_relay_request_v66','fail_relay_request_v3','renew_relay_job_lease_v2','renew_sync_request_fence','claim_cache_operation_v71','claim_cache_operation_v3','start_cache_operation_v31','record_cache_operation_observation_v31','finish_cache_operation_v31','publish_cache_resource_v3','publish_cache_resource_v31','reconcile_relay_request','claim_relay_job_v2','claim_cache_reconciliation_v71','complete_relay_request','fail_relay_request','start_request_execution_v71','mark_request_dispatch_v71','invalidate_cache_resource_v71','acquire_sync_request_fence','acquire_sync_commit_fence','release_sync_commit_fence_v71') loop
  execute format('revoke all on function %s from public,anon,authenticated',f.signature);
  execute format('grant execute on function %s to service_role',f.signature);
 end loop;
end $$;
commit;
