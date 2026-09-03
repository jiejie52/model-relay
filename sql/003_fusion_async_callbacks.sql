-- V20.25.23 Fusion Async Resume / Callback Contract.
-- Apply after 001_relay_schema.sql and 002_fusion_runtime.sql.
-- This is additive and does not change existing Relay Job result semantics.

alter table public.relay_jobs
    add column if not exists callback_kind text,
    add column if not exists callback_status text,
    add column if not exists callback_conversation_id text,
    add column if not exists callback_user_id text,
    add column if not exists callback_resume_query text,
    add column if not exists callback_attempt_count integer not null default 0,
    add column if not exists callback_next_attempt_at timestamptz,
    add column if not exists callback_lease_owner text,
    add column if not exists callback_lease_expires_at timestamptz,
    add column if not exists callback_last_error text,
    add column if not exists callback_completed_at timestamptz;

-- Existing rows remain NULL (no callback registration). New callback jobs use:
-- waiting -> pending -> delivering -> delivered|failed.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'relay_jobs_callback_status_check'
    ) then
        alter table public.relay_jobs
            add constraint relay_jobs_callback_status_check check (
                callback_status is null or callback_status in (
                    'waiting', 'pending', 'delivering', 'delivered', 'failed'
                )
            );
    end if;
end $$;

create index if not exists relay_jobs_callback_queue_idx
    on public.relay_jobs (callback_status, callback_next_attempt_at, completed_at)
    where callback_kind is not null;

-- Claim one terminal job whose Dify callback is due. Stale callback leases are
-- reclaimable, so a worker restart cannot permanently lose an async resume.
create or replace function public.claim_relay_callback(
    p_worker_id text,
    p_lease_seconds integer default 180
)
returns setof public.relay_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    v_job_id uuid;
begin
    select j.id
      into v_job_id
      from public.relay_jobs j
     where j.callback_kind = 'dify_chatflow_resume'
       and j.status in ('succeeded', 'failed', 'cancelled', 'expired')
       and (
            (
                j.callback_status = 'pending'
                and (j.callback_next_attempt_at is null or j.callback_next_attempt_at <= now())
            )
            or (
                j.callback_status = 'delivering'
                and j.callback_lease_expires_at is not null
                and j.callback_lease_expires_at < now()
            )
       )
     order by coalesce(j.callback_next_attempt_at, j.completed_at, j.created_at) asc
     for update skip locked
     limit 1;

    if v_job_id is null then
        return;
    end if;

    return query
    update public.relay_jobs
       set callback_status = 'delivering',
           callback_lease_owner = p_worker_id,
           callback_lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           callback_attempt_count = callback_attempt_count + 1
     where id = v_job_id
     returning *;
end;
$$;

revoke all on function public.claim_relay_callback(text, integer)
    from public, anon, authenticated;
grant execute on function public.claim_relay_callback(text, integer)
    to service_role;
