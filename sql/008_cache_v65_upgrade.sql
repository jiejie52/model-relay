-- Relay 6.5.0. Reuse the existing cache operation, binding and resource tables.
alter table public.relay_sessions
    add column if not exists affinity_epoch integer not null default 1,
    add column if not exists session_affinity_token text,
    add column if not exists affinity_eviction_reason text,
    add column if not exists affinity_eviction_request_id uuid;

alter table public.relay_cache_resources
    drop constraint if exists relay_cache_resources_state_check;
alter table public.relay_cache_resources
    add constraint relay_cache_resources_state_check check (
        state in ('planned','creating','create_unknown','ready','renewing','retiring',
                  'delete_pending','expired','invalid','deleted','orphaned','failed','evicted_by_drift')
    );

create or replace function public.rotate_cache_affinity_v65(
    p_request_id uuid, p_epoch integer, p_token text, p_reason text,
    p_fence_owner text, p_fence_epoch bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    r public.relay_requests%rowtype;
    s public.relay_sessions%rowtype;
    j public.relay_jobs%rowtype;
begin
    select * into r from public.relay_requests where id=p_request_id for update;
    if not found or r.provider_dispatch_state <> 'not_sent'
       or r.status not in ('running','leased','queued') then return false; end if;
    if r.job_id is not null then
        select * into j from public.relay_jobs where id=r.job_id for update;
        if not found or j.lease_owner is distinct from p_fence_owner
           or j.lease_epoch is distinct from p_fence_epoch
           or j.status not in ('running','leased')
           or j.lease_expires_at is null or j.lease_expires_at <= now() then return false; end if;
    elsif r.request_executor_id is distinct from p_fence_owner
          or r.request_executor_epoch is distinct from p_fence_epoch
          or r.request_executor_lease_expires_at is null
          or r.request_executor_lease_expires_at <= now() then return false;
    end if;

    select * into s from public.relay_sessions where id=r.session_id for update;
    if not found or s.tenant_id is distinct from r.tenant_id
       or s.conversation_hash is distinct from r.conversation_hash
       or s.affinity_epoch <> p_epoch
       or s.affinity_eviction_request_id is not distinct from p_request_id
       or s.active_request_id is distinct from p_request_id then return false; end if;

    update public.relay_sessions
       set affinity_epoch=p_epoch+1, session_affinity_token=p_token,
           affinity_eviction_reason=p_reason, affinity_eviction_request_id=p_request_id
     where id=s.id;
    update public.relay_cache_resources
       set state='evicted_by_drift', updated_at=now()
     where session_id=s.id and state='ready';
    return true;
end;
$$;
revoke all on function public.rotate_cache_affinity_v65(uuid,integer,text,text,text,bigint) from public, anon, authenticated;
grant execute on function public.rotate_cache_affinity_v65(uuid,integer,text,text,text,bigint) to service_role;

create or replace function public.record_cache_affinity_event_v65(
    p_request_id uuid, p_audit jsonb, p_fence_owner text, p_fence_epoch bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    r public.relay_requests%rowtype;
    j public.relay_jobs%rowtype;
begin
    select * into r from public.relay_requests where id=p_request_id for update;
    if not found or r.provider_dispatch_state <> 'dispatch_started'
       or r.status not in ('running','leased') then return false; end if;
    if r.job_id is not null then
        select * into j from public.relay_jobs where id=r.job_id for update;
        if not found or j.lease_owner is distinct from p_fence_owner
           or j.lease_epoch is distinct from p_fence_epoch
           or j.status not in ('running','leased')
           or j.lease_expires_at is null or j.lease_expires_at <= now() then return false; end if;
    elsif r.request_executor_id is distinct from p_fence_owner
          or r.request_executor_epoch is distinct from p_fence_epoch
          or r.request_executor_lease_expires_at is null
          or r.request_executor_lease_expires_at <= now() then return false;
    end if;
    update public.relay_requests
       set cache_usage=coalesce(cache_usage,'{}'::jsonb) || jsonb_build_object('affinity_audit',p_audit)
     where id=p_request_id;
    return true;
end;
$$;
revoke all on function public.record_cache_affinity_event_v65(uuid,jsonb,text,bigint) from public, anon, authenticated;
grant execute on function public.record_cache_affinity_event_v65(uuid,jsonb,text,bigint) to service_role;
