-- Commit-only recovery for synchronous requests with archived provider facts.
-- Apply after 009_cache_v66_patch.sql. Existing inference-fence RPCs are unchanged.
begin;

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
    update public.relay_requests
       set request_executor_id = p_executor_id,
           request_executor_epoch = request_executor_epoch + 1,
           request_executor_lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds,30))
     where id = p_request_id
       and execution_mode = 'sync'
       and job_id is null
       and status = 'running'
       and provider_dispatch_state = 'result_stored'
       and provisional_result_object_id is not null
       and (
           request_executor_id is null
           or request_executor_lease_expires_at is null
           or request_executor_lease_expires_at < now()
           or request_executor_id = p_executor_id
       )
     returning request_executor_epoch into v_epoch;
    return v_epoch;
end;
$$;

revoke all on function public.acquire_sync_commit_fence(uuid,text,integer) from public, anon, authenticated;
grant execute on function public.acquire_sync_commit_fence(uuid,text,integer) to service_role;

-- Expired executors may archive diagnostics but cannot advance Request/Job terminal facts.
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
    if p_status not in ('failed','indeterminate') then return false; end if;
    select * into v_request from public.relay_requests
     where id=p_request_id and session_id=p_session_id for update;
    if not found or v_request.status in ('succeeded','cancelled','failed','indeterminate') then return false; end if;

    if v_request.job_id is not null then
        select * into v_job from public.relay_jobs where id=v_request.job_id for update;
        if not found or v_job.lease_owner is distinct from p_fence_owner
           or v_job.lease_epoch is distinct from p_fence_epoch
           or v_job.status not in ('leased','running')
           or v_job.lease_expires_at is null or v_job.lease_expires_at<=now() then return false; end if;
    else
        if v_request.request_executor_id is distinct from p_fence_owner
           or v_request.request_executor_epoch is distinct from p_fence_epoch
           or v_request.request_executor_lease_expires_at is null
           or v_request.request_executor_lease_expires_at<=now() then return false; end if;
    end if;

    update public.relay_requests set status=p_status,error=p_error,completed_at=now() where id=p_request_id;
    if v_request.job_id is not null then
        update public.relay_jobs
           set status=p_status,completed_at=now(),heartbeat_at=now(),error_code=null,error_message=null
         where id=v_request.job_id;
    end if;
    if p_release_session then
        update public.relay_sessions set active_request_id=null,updated_at=now()
         where id=p_session_id and active_request_id=p_request_id;
    end if;
    update public.relay_cache_pins
       set status=case when p_hold_pins_until is null then 'released' else 'held' end,
           hold_until=p_hold_pins_until,
           released_at=case when p_hold_pins_until is null then now() else null end
     where request_id=p_request_id and status<>'released';
    return true;
end;
$$;

revoke all on function public.fail_relay_request_v3(uuid,uuid,text,jsonb,boolean,text,bigint,timestamptz) from public, anon, authenticated;
grant execute on function public.fail_relay_request_v3(uuid,uuid,text,jsonb,boolean,text,bigint,timestamptz) to service_role;

commit;
