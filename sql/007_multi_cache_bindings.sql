-- Model Relay 8.0.0: pin every stateful resource represented by one frozen
-- Request cache-binding set. The request still has one sealed binding record;
-- its JSON cache_bindings array is the authoritative per-binding inventory.
-- Legacy relay-cache-binding/1 rows continue to use the scalar resource_id.

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
                    where r.id=v_resource_id
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
                            ) > now()
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
            where r.id=v_binding.resource_id and r.state='ready'
              and (
                    coalesce(
                        nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                        nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                    ) is null
                    or coalesce(
                        nullif(to_jsonb(r)->>'expire_time', '')::timestamptz,
                        nullif(to_jsonb(r)->>'expires_at', '')::timestamptz
                    ) > now()
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
                update public.relay_cache_resources set last_used_at=now() where id=v_resource_id;
            end if;
        end loop;
    elsif v_binding.resource_id is not null then
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
           started_at=coalesce(started_at,now())
     where id=p_request_id;
    return true;
end;
$$;

revoke all on function public.seal_cache_and_dispatch_v3(uuid,integer,text,text,text,bigint) from public, anon, authenticated;
grant execute on function public.seal_cache_and_dispatch_v3(uuid,integer,text,text,text,bigint) to service_role;
