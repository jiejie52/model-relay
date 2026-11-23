-- Model Relay 10.6.2. Forward-only idempotent repair after 015/016/017/018.
-- Ensures all continuation schema columns, constraints, indexes, and triggers
-- required by 016/017/018 exist even if an earlier 016 draft ran before
-- anchor_class (and related columns) were added, without downgrading 017/018
-- RPCs or triggers.
begin;

alter table public.relay_continuation_anchors
    add column if not exists source_request_id uuid references public.relay_requests(id) on delete cascade,
    add column if not exists covered_turn_count integer,
    add column if not exists covered_material_bindings jsonb,
    add column if not exists connection_spec_hash text,
    add column if not exists anchor_class text,
    add column if not exists affinity_epoch bigint,
    add column if not exists protocol text,
    add column if not exists model text,
    add column if not exists invalidated_by_request_id uuid references public.relay_requests(id) on delete set null,
    add column if not exists invalidation_reason text,
    add column if not exists is_root_prefix boolean not null default true,
    add column if not exists covered_materials_digest text,
    add column if not exists updated_at timestamptz not null default clock_timestamp();

alter table public.relay_continuation_anchors
    drop constraint if exists relay_continuation_anchors_state_check;
alter table public.relay_continuation_anchors
    add constraint relay_continuation_anchors_state_check
    check (state in ('active', 'superseded', 'expired', 'invalidated'));

alter table public.relay_anchor_pins
    add column if not exists status text not null default 'active'
        check (status in ('active', 'held', 'released')),
    add column if not exists released_at timestamptz;

alter table public.relay_requests
    add column if not exists sealed_continuation_binding jsonb,
    add column if not exists continuation_plan_object_id text,
    add column if not exists continuation_plan_hash text,
    add column if not exists produced_anchor_id uuid,
    add column if not exists continuation_binding_version text,
    add column if not exists continuation_binding_hash text,
    add column if not exists parent_anchor_id uuid;

create unique index if not exists relay_continuation_anchor_source_v91
    on public.relay_continuation_anchors(source_request_id);
create index if not exists relay_continuation_prefix_v91
    on public.relay_continuation_anchors(session_id, head_fingerprint, covered_turn_count desc)
    where state = 'active' and source_request_id is not null;
create index if not exists relay_continuation_prefix_v92
    on public.relay_continuation_anchors (session_id, head_fingerprint, covered_turn_count desc, history_version desc)
    where state = 'active' and source_request_id is not null and is_root_prefix;
create index if not exists relay_requests_session_taint_v92_idx
    on public.relay_requests (session_id, created_at desc)
    where status in ('indeterminate', 'failed', 'cancelled');

alter table public.relay_continuation_anchors enable row level security;
alter table public.relay_anchor_pins enable row level security;
revoke all on public.relay_continuation_anchors, public.relay_anchor_pins from public, anon, authenticated;
grant select, insert, update, delete on table public.relay_continuation_anchors to service_role;
grant select, insert, update, delete on table public.relay_anchor_pins to service_role;

create or replace function public.sync_continuation_plan_columns_v91()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    if new.continuation_plan_object_id is null then
        new.continuation_plan_object_id := new.metadata->'_relay_continuation'->>'plan_object_id';
    end if;
    if new.continuation_plan_hash is null then
        new.continuation_plan_hash := new.metadata->'_relay_continuation'->>'plan_hash';
    end if;
    return new;
end;
$$;

create or replace function public.protect_continuation_pin_v91()
returns trigger language plpgsql security definer set search_path = public as $$
begin
    if exists (
        select 1 from public.relay_anchor_pins
         where anchor_id = old.id
           and status in ('active', 'held')
    ) then
        raise exception 'CONTINUATION_ANCHOR_PINNED';
    end if;
    return old;
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

drop trigger if exists sync_continuation_plan_columns_v91 on public.relay_requests;
create trigger sync_continuation_plan_columns_v91
    before insert on public.relay_requests
    for each row execute function public.sync_continuation_plan_columns_v91();

drop trigger if exists release_continuation_after_request_v91 on public.relay_requests;
create trigger release_continuation_after_request_v91
    after update of status, provider_dispatch_state on public.relay_requests
    for each row execute function public.release_continuation_after_request_v91();

drop trigger if exists protect_continuation_pin_v91 on public.relay_continuation_anchors;
create trigger protect_continuation_pin_v91
    before delete on public.relay_continuation_anchors
    for each row execute function public.protect_continuation_pin_v91();

drop trigger if exists retire_linear_anchors_on_history_advance_v92 on public.relay_sessions;
create trigger retire_linear_anchors_on_history_advance_v92
    after update of history_version on public.relay_sessions
    for each row execute function public.retire_linear_anchors_on_history_advance_v92();

revoke all on function public.sync_continuation_plan_columns_v91(),
    public.protect_continuation_pin_v91(),
    public.retire_linear_anchors_on_history_advance_v92(),
    public.release_continuation_after_request_v91()
    from public, anon, authenticated;

notify pgrst, 'reload schema';
commit;
