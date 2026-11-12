-- Model Relay 8.9.1. Apply after 013_material_occurrence_placement.sql.
-- Forward-only repair: align SQL Binding validation with the shared Python contract.
begin;

create or replace function public.provider_material_binding_state_failure_v891(
    p_state text,
    p_processing_state text
) returns text
language sql
immutable
parallel safe
set search_path=public
as $$
    select case
        when coalesce(
            nullif(lower(btrim(p_state)), ''),
            nullif(lower(btrim(p_processing_state)), '')
        ) is null
        or coalesce(
            nullif(lower(btrim(p_state)), ''),
            nullif(lower(btrim(p_processing_state)), '')
        ) not in ('active', 'ready', 'ready_provider', 'processed')
            then 'BINDING_STATE_INVALID'
        when nullif(lower(btrim(p_processing_state)), '') is not null
         and lower(btrim(p_processing_state)) not in ('active', 'ready', 'ready_provider', 'processed', 'unspecified', 'file_state_unspecified')
            then 'BINDING_PROCESSING_NOT_READY'
        else null
    end;
$$;

comment on function public.provider_material_binding_state_failure_v891(text, text) is
    'Provider Binding contract mirrored by app/core/provider_binding_state.py; ready_provider remains accepted only as a legacy Binding compatibility value.';

create or replace function public.seal_materials_and_dispatch_v89(
    p_args jsonb,
    p_material_occurrences jsonb,
    p_historical_bindings jsonb,
    p_safety_seconds integer
) returns boolean
language plpgsql
security definer
set search_path=public
as $$
declare
    r public.relay_requests%rowtype;
    b public.provider_material_bindings%rowtype;
    m public.relay_materials%rowtype;
    item jsonb;
    occ jsonb;
    snap jsonb;
    pins jsonb;
    req uuid := (p_args->>'p_request_id')::uuid;
    owner text := p_args->>'p_fence_owner';
    epoch bigint := (p_args->>'p_fence_epoch')::bigint;
    i integer := 0;
    valid boolean;
    v_state_failure text;
    v_remaining_ttl_seconds integer;
    v_diagnostic jsonb;
begin
    if not public.validate_request_execution_fence_v71(req, owner, epoch, array['not_sent']) then
        return false;
    end if;

    select * into r from public.relay_requests where id=req for update;
    if r.placement_contract_version is distinct from 'material-placement/2'
       or jsonb_typeof(p_material_occurrences) is distinct from 'array'
       or jsonb_typeof(p_historical_bindings) is distinct from 'array'
       or p_safety_seconds is null or p_safety_seconds < 30 or p_safety_seconds > 3600
       or p_safety_seconds is distinct from (r.material_placement_plan->>'binding_safety_seconds')::integer
       or jsonb_array_length(p_material_occurrences) <> jsonb_array_length(r.material_binding_snapshot) then
        raise exception 'MATERIAL_PLACEMENT_CONTRACT_INVALID';
    end if;

    for occ in select value from jsonb_array_elements(p_material_occurrences) loop
        snap := r.material_binding_snapshot->i;
        if not (occ->'binding' @> snap)
           or occ->>'material_id' is distinct from snap->>'material_id'
           or occ->>'source_request_id' is distinct from req::text
           or (occ->>'part_index')::integer is distinct from i
           or occ->>'material_id' is distinct from r.material_placement_plan->'current_effective_material_ids'->>i then
            raise exception 'CURRENT_TURN_MATERIAL_BINDING_MISMATCH';
        end if;
        i := i + 1;
    end loop;

    for occ in select value from jsonb_array_elements(r.material_placement_plan->'verified_history_occurrences') loop
        if not exists (
            select 1 from jsonb_array_elements(p_historical_bindings) x
            where x->>'material_id' = occ->>'material_id'
              and x->>'binding_generation' = occ->>'binding_generation'
              and (x->>'external_uri') is not distinct from (occ->'binding'->>'external_uri')
        ) then
            raise exception 'HISTORICAL_MATERIAL_UNPINNED';
        end if;
    end loop;

    pins := coalesce(p_historical_bindings, '[]'::jsonb)
        || coalesce((select jsonb_agg(x->'binding') from jsonb_array_elements(p_material_occurrences) x), '[]'::jsonb);

    -- Lock material and binding rows in stable order before checking and pinning.
    perform 1
      from public.relay_materials
     where id in (select x->>'material_id' from jsonb_array_elements(pins) x)
     order by id
     for update;
    perform 1
      from public.provider_material_bindings
     where id::text in (select x->>'provider_binding_id' from jsonb_array_elements(pins) x)
     order by id
     for update;

    for item in select value from jsonb_array_elements(pins) loop
        select * into m from public.relay_materials where id=item->>'material_id';
        if not found
           or m.tenant_id <> r.tenant_id
           or m.conversation_hash <> r.conversation_hash
           or m.sha256 is distinct from item->>'content_sha256'
           or m.status in ('deleted', 'failed', 'reupload_required')
           or item->>'connection_id' is distinct from r.connection_id
           or item->>'account_scope_hash' is distinct from r.material_placement_plan->'identity'->>'account_scope_id' then
            raise exception using
                message = 'MATERIAL_BINDING_SCOPE_MISMATCH',
                detail = jsonb_build_object(
                    'request_id', req::text,
                    'session_id', r.session_id::text,
                    'material_id', item->>'material_id',
                    'connection_id', r.connection_id,
                    'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                    'validation_failure_reason', 'BINDING_SCOPE_MISMATCH',
                    'failure_stage', 'atomic_dispatch_seal_sql'
                )::text;
        end if;

        if item->>'representation' is distinct from 'gemini_inline_data' then
            select * into b
              from public.provider_material_bindings
             where id::text = item->>'provider_binding_id';

            if not found then
                v_diagnostic := jsonb_build_object(
                    'request_id', req::text,
                    'session_id', r.session_id::text,
                    'material_id', item->>'material_id',
                    'connection_id', r.connection_id,
                    'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                    'failure_stage', 'atomic_dispatch_seal_sql'
                );
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_NOT_FOUND'
                    ))::text;
            end if;

            v_remaining_ttl_seconds := case
                when b.expires_at is not null
                    then floor(extract(epoch from (b.expires_at - clock_timestamp())))::integer
                else null
            end;
            v_diagnostic := jsonb_build_object(
                'request_id', req::text,
                'session_id', r.session_id::text,
                'material_id', item->>'material_id',
                'connection_id', r.connection_id,
                'expected_generation', nullif(item->>'binding_generation', '')::bigint,
                'actual_generation', b.generation,
                'binding_state', b.state,
                'processing_state', b.processing_state,
                'remaining_ttl_seconds', v_remaining_ttl_seconds,
                'failure_stage', 'atomic_dispatch_seal_sql'
            );

            if b.material_id is distinct from m.id
               or b.connection_id is distinct from r.connection_id
               or b.account_scope_hash is distinct from item->>'account_scope_hash' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_SCOPE_MISMATCH'
                    ))::text;
            end if;

            if b.representation is distinct from item->>'representation' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_REPRESENTATION_MISMATCH'
                    ))::text;
            end if;

            if b.generation is distinct from (item->>'binding_generation')::bigint then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_GENERATION_MISMATCH'
                    ))::text;
            end if;

            v_state_failure := public.provider_material_binding_state_failure_v891(
                b.state, b.processing_state
            );
            if v_state_failure is not null then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', v_state_failure
                    ))::text;
            end if;

            if b.expires_at is null
               or b.expires_at <= clock_timestamp() + make_interval(secs=>p_safety_seconds) then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_EXPIRED'
                    ))::text;
            end if;

            if coalesce(b.external_uri, b.file_uri) is distinct from item->>'external_uri' then
                raise exception using
                    message = 'HISTORICAL_MATERIAL_BINDING_EXPIRED',
                    detail = (v_diagnostic || jsonb_build_object(
                        'validation_failure_reason', 'BINDING_URI_MISMATCH'
                    ))::text;
            end if;
        end if;
    end loop;

    for item in select value from jsonb_array_elements(pins) loop
        insert into public.relay_material_pins(
            request_id, binding_key, material_id, provider_binding_id,
            binding_generation, binding_snapshot, status
        ) values (
            req,
            (item->>'material_id') || ':' || (item->>'binding_generation') || ':' || coalesce(item->>'provider_binding_id', 'inline'),
            item->>'material_id',
            nullif(item->>'provider_binding_id', '')::uuid,
            (item->>'binding_generation')::bigint,
            item,
            'active'
        )
        on conflict(request_id, binding_key) do update
            set status='active', released_at=null;
    end loop;

    valid := public.seal_cache_and_dispatch_v3(
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_binding_version => (p_args->>'p_binding_version')::integer,
        p_binding_hash => (p_args->>'p_binding_hash')::text,
        p_payload_hash => (p_args->>'p_payload_hash')::text,
        p_fence_owner => (p_args->>'p_fence_owner')::text,
        p_fence_epoch => (p_args->>'p_fence_epoch')::bigint
    );
    if not valid then
        raise exception 'MATERIAL_DISPATCH_SEAL_REJECTED';
    end if;

    update public.relay_requests
       set material_seal_occurrences=p_material_occurrences
     where id=req;
    if not public.validate_request_execution_fence_v71(req, owner, epoch, array['dispatch_started']) then
        raise exception 'EXECUTION_FENCE_LOST';
    end if;
    return true;
end;
$$;

revoke all on function public.provider_material_binding_state_failure_v891(text, text) from public, anon, authenticated;
revoke all on function public.seal_materials_and_dispatch_v89(jsonb, jsonb, jsonb, integer) from public, anon, authenticated;
grant execute on function public.provider_material_binding_state_failure_v891(text, text) to service_role;
grant execute on function public.seal_materials_and_dispatch_v89(jsonb, jsonb, jsonb, integer) to service_role;

notify pgrst, 'reload schema';
commit;
