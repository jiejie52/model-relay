-- Model Relay 3.1.0 - Gemini Stateful Cache closure.
-- Apply after 005_relay_cache_control.sql.
--
-- Resource identity now separates the exact cached prefix fingerprint from a
-- reuse family. A resource created at committed history turn N may serve later
-- requests whose history still starts with that exact prefix.

alter table public.relay_cache_resources
    add column if not exists reuse_key text,
    add column if not exists prefix_version bigint,
    add column if not exists token_count bigint,
    add column if not exists spec_hash text;

create index if not exists relay_cache_resources_reuse_idx
    on public.relay_cache_resources (scope_hash, reuse_key, state, prefix_version desc, generation desc);

create unique index if not exists relay_cache_resources_provider_handle_uq
    on public.relay_cache_resources (connection_id, provider_handle_ref)
    where provider_handle_ref is not null;

-- Tighten cache-operation claiming. A known failed operation is safe to retry;
-- an unknown Provider side effect is deliberately not reclaimable. Succeeded
-- operations are never re-issued.
-- Final-threshold guards are resolved by the installed physical binding. Keep
-- Request read models synchronized without rewriting the immutable cache plan.
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
    select * into v_request from public.relay_requests where id=p_request_id for update;
    if not found or v_request.provider_dispatch_state <> 'not_sent' or v_request.status in ('cancelled','failed','succeeded','indeterminate') then return false; end if;
    if v_request.cache_plan_hash is distinct from p_plan_hash then return false; end if;
    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner<>p_fence_owner or v_job.lease_epoch<>p_fence_epoch
           or v_job.status not in ('leased','running') then return false; end if;
    else
        if v_request.request_executor_id<>p_fence_owner or v_request.request_executor_epoch<>p_fence_epoch
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
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
begin
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
           or v_request.request_executor_lease_expires_at < now() then return false; end if;
    end if;

    if v_binding.resource_id is not null then
        if not exists(select 1 from public.relay_cache_resources r
                      where r.id=v_binding.resource_id and r.state='ready'
                        and (r.expire_time is null or r.expire_time > now())) then
            return false;
        end if;
        insert into public.relay_cache_pins(resource_id,request_id,binding_version,execution_fence,status)
        values(v_binding.resource_id,p_request_id,p_binding_version,p_fence_owner||':'||p_fence_epoch::text,'active')
        on conflict (resource_id,request_id,binding_version) do update set status='active', released_at=null;
        update public.relay_cache_resources set last_used_at=now() where id=v_binding.resource_id;
    end if;

    update public.relay_request_cache_bindings
       set state='sealed', prepared_payload_hash=p_payload_hash, sealed_at=now()
     where request_id=p_request_id and binding_version=p_binding_version;
    update public.relay_requests
       set prepared_payload_hash=p_payload_hash,
           provider_dispatch_state='dispatch_started',
           started_at=coalesce(started_at,now()),
           metadata=jsonb_set(
               coalesce(metadata,'{}'::jsonb),
               '{_relay_cache}',
               coalesce(metadata->'_relay_cache','{}'::jsonb) || jsonb_build_object(
                   'execution_mechanism',v_binding.final_mechanism
               ),
               true
           )
     where id=p_request_id;
    return true;
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
declare
    v_op public.relay_cache_operations%rowtype;
begin
    insert into public.relay_cache_operations(
        scope_hash,content_fingerprint,operation_type,request_id,idempotency_key,
        lease_owner,lease_epoch,lease_expires_at,state,attempt_no
    ) values(
        p_scope_hash,p_content_fingerprint,p_operation_type,p_request_id,p_idempotency_key,
        p_lease_owner,1,now()+make_interval(secs=>greatest(p_lease_seconds,15)),'leased',1
    ) on conflict(scope_hash,content_fingerprint,operation_type,idempotency_key) do nothing;

    select * into v_op from public.relay_cache_operations
     where scope_hash=p_scope_hash and content_fingerprint=p_content_fingerprint
       and operation_type=p_operation_type and idempotency_key=p_idempotency_key for update;
    if not found then return; end if;

    -- running means the Provider operation has crossed its dispatch fence. If
    -- that lease expires the external side effect is unknown and MUST NOT be
    -- replayed. A reconciler may inspect raw_result/provider request id later.
    if v_op.state='running' then
        if v_op.lease_expires_at is null or v_op.lease_expires_at < now() then
            update public.relay_cache_operations
               set state='unknown', lease_expires_at=null, updated_at=now()
             where op_id=v_op.op_id and state='running';
        end if;
        return;
    end if;

    if v_op.state in ('unknown','succeeded','cancelled') then
        return;
    end if;

    update public.relay_cache_operations
       set request_id=p_request_id,
           lease_owner=p_lease_owner,
           lease_epoch=lease_epoch+case
               when state='failed' or lease_owner is distinct from p_lease_owner or lease_expires_at<now() then 1
               else 0 end,
           lease_expires_at=now()+make_interval(secs=>greatest(p_lease_seconds,15)),
           state='leased',
           attempt_no=attempt_no+case when state in ('planned','failed') or lease_expires_at<now() then 1 else 0 end,
           updated_at=now()
     where op_id=v_op.op_id
       and (
           state in ('planned','failed')
           or (state='leased' and lease_expires_at<now())
           or (state='leased' and lease_owner=p_lease_owner)
       );

    return query
      select * from public.relay_cache_operations
       where op_id=v_op.op_id and lease_owner=p_lease_owner and state='leased';
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
    update public.relay_cache_operations
       set state='running', updated_at=now()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state='leased'
       and lease_expires_at >= now();
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
    update public.relay_cache_operations
       set raw_result=coalesce(p_raw_result,'{}'::jsonb),
           provider_request_id=coalesce(p_provider_request_id,provider_request_id),
           updated_at=now()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state='running'
       and lease_expires_at >= now();
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
    if p_state not in ('failed','unknown','cancelled') then
        return false;
    end if;
    update public.relay_cache_operations
       set state=p_state,
           raw_result=coalesce(p_raw_result,'{}'::jsonb),
           lease_expires_at=null,
           updated_at=now()
     where op_id=p_operation_id
       and lease_owner=p_lease_owner
       and lease_epoch=p_lease_epoch
       and state in ('leased','running');
    return found;
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
    select * into v_op from public.relay_cache_operations where op_id=p_operation_id for update;
    if not found or v_op.lease_owner<>p_lease_owner or v_op.lease_epoch<>p_lease_epoch
       or v_op.state <> 'running' or v_op.lease_expires_at is null or v_op.lease_expires_at < now() then return; end if;
    if p_provider_handle_ref is null or p_provider_handle_ref !~ '^cachedContents/[A-Za-z0-9._~-]+$' then
        return;
    end if;

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
        p_lease_epoch,p_lease_owner,now(),
        jsonb_build_object(
            'create_operation_id',p_operation_id,
            'prefix_version',p_prefix_version,
            'token_count',p_token_count
        )
    ) returning id into v_resource_id;

    update public.relay_cache_operations
       set state='succeeded',resource_id=v_resource_id,raw_result=p_raw_result,
           lease_expires_at=null,updated_at=now()
     where op_id=p_operation_id;

    return query select * from public.relay_cache_resources where id=v_resource_id;
end;
$$;

-- Used only before model dispatch when a Provider-side get proves that a local
-- ready row is no longer safe to reference. The handle match prevents callers
-- from invalidating a different resource by id alone.
create or replace function public.invalidate_cache_resource_v31(
    p_resource_id uuid,
    p_provider_handle_ref text,
    p_state text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_state not in ('expired','invalid','deleted','orphaned','failed') then
        return false;
    end if;
    update public.relay_cache_resources
       set state=p_state, updated_at=now()
     where id=p_resource_id
       and provider_handle_ref=p_provider_handle_ref
       and state in ('ready','renewing','retiring','delete_pending');
    return found;
end;
$$;

revoke all on function public.start_cache_operation_v31(uuid,text,bigint) from public, anon, authenticated;
grant execute on function public.start_cache_operation_v31(uuid,text,bigint) to service_role;
revoke all on function public.record_cache_operation_observation_v31(uuid,text,bigint,jsonb,text) from public, anon, authenticated;
grant execute on function public.record_cache_operation_observation_v31(uuid,text,bigint,jsonb,text) to service_role;
revoke all on function public.finish_cache_operation_v31(uuid,text,bigint,text,jsonb) from public, anon, authenticated;
grant execute on function public.finish_cache_operation_v31(uuid,text,bigint,text,jsonb) to service_role;
revoke all on function public.publish_cache_resource_v31(uuid,text,bigint,text,text,uuid,text,text,text,text,text,text,bigint,bigint,text,text,timestamptz,jsonb) from public, anon, authenticated;
grant execute on function public.publish_cache_resource_v31(uuid,text,bigint,text,text,uuid,text,text,text,text,text,text,bigint,bigint,text,text,timestamptz,jsonb) to service_role;
revoke all on function public.invalidate_cache_resource_v31(uuid,text,text) from public, anon, authenticated;
grant execute on function public.invalidate_cache_resource_v31(uuid,text,text) to service_role;
