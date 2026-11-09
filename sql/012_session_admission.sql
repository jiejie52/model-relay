-- Optional only when configuring session quota / reservation budgets.
-- Reservation units are operator-defined admission credits, not provider bills.
begin;
create table if not exists public.relay_session_admissions (
    session_id uuid primary key references public.relay_sessions(id) on delete cascade,
    scope text not null,
    units bigint not null check(units>=0),
    expires_at timestamptz not null
);
create index if not exists relay_session_admissions_scope on public.relay_session_admissions(scope,expires_at);
alter table public.relay_session_admissions enable row level security;
revoke all on public.relay_session_admissions from public,anon,authenticated;

create or replace function public.create_relay_session_admitted(p_row jsonb)
returns jsonb language plpgsql security definer set search_path=public,pg_temp as $$
declare
    q jsonb := p_row#>'{metadata,_relay_route,quota}';
    scope_key text := p_row#>>'{metadata,_relay_admission,scope}';
    max_sessions bigint := (q->>'max_active_sessions')::bigint;
    max_units bigint := (q->>'budget_limit_units')::bigint;
    reserve bigint := coalesce((q->>'reserve_units')::bigint,0);
    used_count bigint; used_units numeric;
    existing public.relay_sessions; created public.relay_sessions;
    columns_sql text; values_sql text;
begin
    if scope_key is null or length(scope_key)<>64 or reserve<0
       or coalesce(max_sessions,1)<1 or coalesce(max_units,1)<1
       or (max_units is not null and reserve<1)
       or (p_row->>'expires_at')::timestamptz<=clock_timestamp() then
        raise exception 'INVALID_SESSION_ADMISSION';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('session-idem:'||(p_row->>'tenant_id')||':'||(p_row->>'idempotency_key'),0));
    select * into existing from public.relay_sessions
      where tenant_id=p_row->>'tenant_id' and idempotency_key=p_row->>'idempotency_key';
    if found then
        if existing.session_hash is distinct from p_row->>'session_hash' then raise exception 'IDEMPOTENCY_CONFLICT'; end if;
        return to_jsonb(existing);
    end if;
    perform pg_advisory_xact_lock(hashtextextended('session-admission:'||scope_key,0));
    select count(*),coalesce(sum(units),0) into used_count,used_units
      from public.relay_session_admissions where scope=scope_key and expires_at>clock_timestamp();
    if max_sessions is not null and used_count>=max_sessions then raise exception 'SESSION_QUOTA_EXHAUSTED'; end if;
    if max_units is not null and used_units+reserve>max_units then raise exception 'SESSION_BUDGET_EXHAUSTED'; end if;
    select string_agg(format('%I',key),',' order by key),string_agg(format('r.%I',key),',' order by key)
      into columns_sql,values_sql from jsonb_object_keys(p_row) key;
    execute format('insert into public.relay_sessions (%s) select %s from jsonb_populate_record(null::public.relay_sessions,$1) r returning *',columns_sql,values_sql)
      into created using p_row;
    insert into public.relay_session_admissions(session_id,scope,units,expires_at)
      values(created.id,scope_key,reserve,created.expires_at);
    return to_jsonb(created);
end $$;
revoke all on function public.create_relay_session_admitted(jsonb) from public,anon,authenticated;
grant execute on function public.create_relay_session_admitted(jsonb) to service_role;
commit;
