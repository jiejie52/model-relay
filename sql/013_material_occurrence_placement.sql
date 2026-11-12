-- Model Relay 8.9.0. Apply after 012. Upgrade API and Worker together.
-- Additive/versioned RPCs leave all accepted legacy Requests unchanged.
begin;
alter table public.relay_sessions add column if not exists committed_material_ledger jsonb not null default '[]'::jsonb;
alter table public.relay_requests
  add column if not exists placement_contract_version text,
  add column if not exists material_placement_summary jsonb,
  add column if not exists material_placement_plan jsonb,
  add column if not exists material_seal_occurrences jsonb;
create table if not exists public.relay_material_pins (
  request_id uuid not null references public.relay_requests(id) on delete cascade,
  binding_key text not null,
  material_id text not null,
  provider_binding_id uuid,
  binding_generation bigint not null,
  binding_snapshot jsonb not null,
  status text not null check (status in ('active','held','released')),
  created_at timestamptz not null default clock_timestamp(),
  released_at timestamptz,
  primary key(request_id,binding_key)
);
create index if not exists relay_material_pins_live on public.relay_material_pins(material_id,status);
alter table public.relay_material_pins enable row level security;
revoke all on public.relay_material_pins from public,anon,authenticated;
grant all on public.relay_material_pins to service_role;

create or replace function public.accept_relay_request_v89(p_args jsonb)
returns setof public.relay_requests language plpgsql security definer set search_path=public as $$
declare r public.relay_requests%rowtype; s public.relay_sessions%rowtype; p jsonb;
begin
 p:=p_args->'p_metadata'->'_relay_material_placement';
 if p->>'contract_version' is distinct from 'material-placement/2' then raise exception 'MATERIAL_PLACEMENT_CONTRACT_INVALID'; end if;
 if p_args ? 'p_route_pin' then
  select * into r from public.accept_relay_request_v66(
        p_session_id => (p_args->>'p_session_id')::uuid,
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_tenant_id => (p_args->>'p_tenant_id')::text,
        p_conversation_hash => (p_args->>'p_conversation_hash')::text,
        p_idempotency_key => (p_args->>'p_idempotency_key')::text,
        p_request_hash => (p_args->>'p_request_hash')::text,
        p_caller_intent_hash => (p_args->>'p_caller_intent_hash')::text,
        p_request_identity_version => (p_args->>'p_request_identity_version')::text,
        p_execution_mode => (p_args->>'p_execution_mode')::text,
        p_request_object_id => (p_args->>'p_request_object_id')::text,
        p_context_plan_object_id => (p_args->>'p_context_plan_object_id')::text,
        p_context_plan_hash => (p_args->>'p_context_plan_hash')::text,
        p_cache_plan_object_id => (p_args->>'p_cache_plan_object_id')::text,
        p_cache_plan_hash => (p_args->>'p_cache_plan_hash')::text,
        p_cache_resolution_status => (p_args->>'p_cache_resolution_status')::text,
        p_provider => (p_args->>'p_provider')::text,
        p_connection_id => (p_args->>'p_connection_id')::text,
        p_model => (p_args->>'p_model')::text,
        p_execution_pool => (p_args->>'p_execution_pool')::text,
        p_expected_history_version => (p_args->>'p_expected_history_version')::bigint,
        p_metadata => p_args->'p_metadata',
        p_job_id => (p_args->>'p_job_id')::uuid,
        p_job_expires_at => (p_args->>'p_job_expires_at')::timestamptz,
        p_route_pin => p_args->'p_route_pin');
 else
  select * into r from public.accept_relay_request_v3(
        p_session_id => (p_args->>'p_session_id')::uuid,
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_tenant_id => (p_args->>'p_tenant_id')::text,
        p_conversation_hash => (p_args->>'p_conversation_hash')::text,
        p_idempotency_key => (p_args->>'p_idempotency_key')::text,
        p_request_hash => (p_args->>'p_request_hash')::text,
        p_caller_intent_hash => (p_args->>'p_caller_intent_hash')::text,
        p_request_identity_version => (p_args->>'p_request_identity_version')::text,
        p_execution_mode => (p_args->>'p_execution_mode')::text,
        p_request_object_id => (p_args->>'p_request_object_id')::text,
        p_context_plan_object_id => (p_args->>'p_context_plan_object_id')::text,
        p_context_plan_hash => (p_args->>'p_context_plan_hash')::text,
        p_cache_plan_object_id => (p_args->>'p_cache_plan_object_id')::text,
        p_cache_plan_hash => (p_args->>'p_cache_plan_hash')::text,
        p_cache_resolution_status => (p_args->>'p_cache_resolution_status')::text,
        p_provider => (p_args->>'p_provider')::text,
        p_connection_id => (p_args->>'p_connection_id')::text,
        p_model => (p_args->>'p_model')::text,
        p_execution_pool => (p_args->>'p_execution_pool')::text,
        p_expected_history_version => (p_args->>'p_expected_history_version')::bigint,
        p_metadata => p_args->'p_metadata',
        p_job_id => (p_args->>'p_job_id')::uuid,
        p_job_expires_at => (p_args->>'p_job_expires_at')::timestamptz);
 end if;
 if r.id is null then raise exception 'REQUEST_ACCEPT_FAILED'; end if;
 -- A concurrent idempotent winner must retain its original frozen plan.
 if r.id::text is distinct from p_args->>'p_request_id' then return next r; return; end if;
 select * into s from public.relay_sessions where id=r.session_id for update;
 if p->'identity'->>'session_id' is distinct from s.id::text
    or p->'identity'->>'connection_id' is distinct from s.connection_id
    or p->'identity'->>'account_scope_id' is distinct from s.metadata->'_relay_route'->>'account_scope_id'
    or p->'identity'->>'connection_spec_hash' is distinct from s.metadata->'_relay_route'->>'connection_spec_hash'
    or (p->>'history_version')::bigint is distinct from s.history_version
    or p->'verified_history_occurrences' is distinct from s.committed_material_ledger then
   raise exception 'MATERIAL_PLACEMENT_CONTEXT_MISMATCH';
 end if;
 update public.relay_requests set placement_contract_version='material-placement/2',
  material_placement_plan=p, material_placement_summary=jsonb_build_object(
   'plan_digest',p->>'plan_digest','current_count',jsonb_array_length(p->'current_effective_material_ids'),
   'historical_count',jsonb_array_length(p->'verified_history_occurrences')) where id=r.id returning * into r;
 return next r;
end; $$;

create or replace function public.seal_materials_and_dispatch_v89(
 p_args jsonb,p_material_occurrences jsonb,p_historical_bindings jsonb,p_safety_seconds integer
) returns boolean language plpgsql security definer set search_path=public as $$
declare r public.relay_requests%rowtype; b public.provider_material_bindings%rowtype;
 m public.relay_materials%rowtype; item jsonb; occ jsonb; snap jsonb; pins jsonb;
 req uuid:=(p_args->>'p_request_id')::uuid; owner text:=p_args->>'p_fence_owner';
 epoch bigint:=(p_args->>'p_fence_epoch')::bigint; i integer:=0; valid boolean;
begin
 if not public.validate_request_execution_fence_v71(req,owner,epoch,array['not_sent']) then return false; end if;
 select * into r from public.relay_requests where id=req for update;
 if r.placement_contract_version is distinct from 'material-placement/2'
    or jsonb_typeof(p_material_occurrences) is distinct from 'array'
    or jsonb_typeof(p_historical_bindings) is distinct from 'array'
    or p_safety_seconds is null or p_safety_seconds<30 or p_safety_seconds>3600
    or p_safety_seconds is distinct from (r.material_placement_plan->>'binding_safety_seconds')::integer
    or jsonb_array_length(p_material_occurrences)<>jsonb_array_length(r.material_binding_snapshot) then
   raise exception 'MATERIAL_PLACEMENT_CONTRACT_INVALID';
 end if;
 for occ in select value from jsonb_array_elements(p_material_occurrences) loop
   snap:=r.material_binding_snapshot->i;
   if not (occ->'binding' @> snap) or occ->>'material_id' is distinct from snap->>'material_id'
      or occ->>'source_request_id' is distinct from req::text
      or (occ->>'part_index')::integer is distinct from i
      or occ->>'material_id' is distinct from r.material_placement_plan->'current_effective_material_ids'->>i then
     raise exception 'CURRENT_TURN_MATERIAL_BINDING_MISMATCH';
   end if;
   i:=i+1;
 end loop;
 for occ in select value from jsonb_array_elements(r.material_placement_plan->'verified_history_occurrences') loop
   if not exists(select 1 from jsonb_array_elements(p_historical_bindings) x
      where x->>'material_id'=occ->>'material_id'
        and x->>'binding_generation'=occ->>'binding_generation'
        and (x->>'external_uri') is not distinct from (occ->'binding'->>'external_uri')) then
     raise exception 'HISTORICAL_MATERIAL_UNPINNED';
   end if;
 end loop;
 pins:=coalesce(p_historical_bindings,'[]'::jsonb) || coalesce((select jsonb_agg(x->'binding') from jsonb_array_elements(p_material_occurrences) x),'[]'::jsonb);
 -- Lock material and binding rows in stable order before checking and pinning.
 perform 1 from public.relay_materials where id in(select x->>'material_id' from jsonb_array_elements(pins) x) order by id for update;
 perform 1 from public.provider_material_bindings where id::text in(select x->>'provider_binding_id' from jsonb_array_elements(pins) x) order by id for update;
 for item in select value from jsonb_array_elements(pins) loop
   select * into m from public.relay_materials where id=item->>'material_id';
   if not found or m.tenant_id<>r.tenant_id or m.conversation_hash<>r.conversation_hash
      or m.sha256 is distinct from item->>'content_sha256' or m.status in ('deleted','failed','reupload_required')
      or item->>'connection_id' is distinct from r.connection_id
      or item->>'account_scope_hash' is distinct from r.material_placement_plan->'identity'->>'account_scope_id' then
     raise exception 'MATERIAL_BINDING_SCOPE_MISMATCH';
   end if;
   if item->>'representation' <> 'gemini_inline_data' then
     select * into b from public.provider_material_bindings where id::text=item->>'provider_binding_id';
     if not found or b.material_id<>m.id or b.connection_id<>r.connection_id
       or b.account_scope_hash is distinct from item->>'account_scope_hash'
       or b.generation is distinct from (item->>'binding_generation')::bigint
       or b.representation is distinct from item->>'representation'
       or b.state not in ('ready','ready_provider') or b.expires_at is null
       or b.expires_at<=clock_timestamp()+make_interval(secs=>p_safety_seconds)
       or coalesce(b.external_uri,b.file_uri) is distinct from item->>'external_uri' then
       raise exception 'HISTORICAL_MATERIAL_BINDING_EXPIRED';
     end if;
   end if;
 end loop;
 for item in select value from jsonb_array_elements(pins) loop
   insert into public.relay_material_pins(request_id,binding_key,material_id,provider_binding_id,binding_generation,binding_snapshot,status)
    values(req,(item->>'material_id')||':'||(item->>'binding_generation')||':'||coalesce(item->>'provider_binding_id','inline'),
      item->>'material_id',nullif(item->>'provider_binding_id','')::uuid,(item->>'binding_generation')::bigint,item,'active')
    on conflict(request_id,binding_key) do update set status='active',released_at=null;
 end loop;
 valid:=public.seal_cache_and_dispatch_v3(
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_binding_version => (p_args->>'p_binding_version')::integer,
        p_binding_hash => (p_args->>'p_binding_hash')::text,
        p_payload_hash => (p_args->>'p_payload_hash')::text,
        p_fence_owner => (p_args->>'p_fence_owner')::text,
        p_fence_epoch => (p_args->>'p_fence_epoch')::bigint);
 if not valid then raise exception 'MATERIAL_DISPATCH_SEAL_REJECTED'; end if;
 update public.relay_requests set material_seal_occurrences=p_material_occurrences where id=req;
 if not public.validate_request_execution_fence_v71(req,owner,epoch,array['dispatch_started']) then raise exception 'EXECUTION_FENCE_LOST'; end if;
 return true;
end; $$;

create or replace function public.complete_material_request_v89(p_args jsonb,p_material_occurrences jsonb)
returns boolean language plpgsql security definer set search_path=public as $$
declare r public.relay_requests%rowtype; s public.relay_sessions%rowtype; occ jsonb; i integer:=0; valid boolean;
begin
 if not public.validate_request_execution_fence_v71((p_args->>'p_request_id')::uuid,p_args->>'p_fence_owner',
   (p_args->>'p_fence_epoch')::bigint,array['result_stored']) then return false; end if;
 select * into r from public.relay_requests where id=(p_args->>'p_request_id')::uuid for update;
 select * into s from public.relay_sessions where id=r.session_id for update;
 if r.placement_contract_version is distinct from 'material-placement/2'
   or r.material_seal_occurrences is null or jsonb_typeof(p_material_occurrences) is distinct from 'array'
   or jsonb_array_length(p_material_occurrences)<>jsonb_array_length(r.material_seal_occurrences)
   or s.committed_material_ledger is distinct from r.material_placement_plan->'verified_history_occurrences' then
   raise exception 'MATERIAL_OCCURRENCE_ARCHIVE_INVALID';
 end if;
 for occ in select value from jsonb_array_elements(p_material_occurrences) loop
   if (occ-'history_version'-'transport_history_hash') is distinct from r.material_seal_occurrences->i
      or (occ->>'history_version')::bigint is distinct from r.expected_history_version+1
      or occ->>'transport_history_hash' !~ '^[0-9a-f]{64}$' then raise exception 'MATERIAL_OCCURRENCE_ARCHIVE_INVALID'; end if;
   i:=i+1;
 end loop;
 valid:=public.complete_relay_request_v66(
        p_request_id => (p_args->>'p_request_id')::uuid,
        p_session_id => (p_args->>'p_session_id')::uuid,
        p_expected_history_version => (p_args->>'p_expected_history_version')::bigint,
        p_history_object_id => (p_args->>'p_history_object_id')::text,
        p_result_object_id => (p_args->>'p_result_object_id')::text,
        p_output_object_id => (p_args->>'p_output_object_id')::text,
        p_compact_result => p_args->'p_compact_result',
        p_provider_response_id => (p_args->>'p_provider_response_id')::text,
        p_fence_owner => (p_args->>'p_fence_owner')::text,
        p_fence_epoch => (p_args->>'p_fence_epoch')::bigint);
 if not valid then raise exception 'MATERIAL_COMMIT_FENCE_REJECTED'; end if;
 update public.relay_sessions set committed_material_ledger=committed_material_ledger||p_material_occurrences where id=r.session_id;
 return true;
end; $$;

create or replace function public.protect_pinned_material_v89()
returns trigger language plpgsql security definer set search_path=public as $$
begin
 if tg_op='UPDATE' and to_jsonb(new)=to_jsonb(old) then return new; end if;
 if tg_table_name='relay_materials' then
  if tg_op='UPDATE' and new.sha256 is not distinct from old.sha256 and new.status is not distinct from old.status then return new; end if;
  if exists(select 1 from public.relay_material_pins p where p.material_id=old.id and p.status in ('active','held')) then
   raise exception 'MATERIAL_RESOURCE_PINNED';
  end if;
 else
  if tg_op='UPDATE' and new.state is not distinct from old.state and new.generation is not distinct from old.generation
    and new.external_uri is not distinct from old.external_uri and new.file_uri is not distinct from old.file_uri
    and new.expires_at is not distinct from old.expires_at then return new; end if;
  if exists(select 1 from public.relay_material_pins p where p.provider_binding_id=old.id and p.status in ('active','held')) then
   raise exception 'MATERIAL_RESOURCE_PINNED';
  end if;
 end if;
 if tg_op='DELETE' then return old; end if;
 return new;
end; $$;
drop trigger if exists protect_pinned_material_v89 on public.relay_materials;
create trigger protect_pinned_material_v89 before delete or update of sha256,status on public.relay_materials
 for each row execute function public.protect_pinned_material_v89();
drop trigger if exists protect_pinned_binding_v89 on public.provider_material_bindings;
create trigger protect_pinned_binding_v89 before delete or update of state,generation,external_uri,file_uri,expires_at on public.provider_material_bindings
 for each row execute function public.protect_pinned_material_v89();

create or replace function public.release_material_pins_v89()
returns trigger language plpgsql security definer set search_path=public as $$
begin
 if new.placement_contract_version='material-placement/2' then
  if new.provider_dispatch_state='committed' or (new.status='failed' and coalesce((new.error->>'unknown_completion')::boolean,false)=false) then
   update public.relay_material_pins set status='released',released_at=clock_timestamp() where request_id=new.id;
  elsif new.status in ('indeterminate','cancelled','failed') then
   -- A cancellation cannot prove that the Provider stopped reading the file.
   update public.relay_material_pins set status='held',released_at=null where request_id=new.id and status<>'released';
  end if;
 end if;
 return new;
end; $$;
drop trigger if exists release_material_pins_v89 on public.relay_requests;
create trigger release_material_pins_v89 after update of status,provider_dispatch_state on public.relay_requests
 for each row execute function public.release_material_pins_v89();
revoke all on function public.accept_relay_request_v89(jsonb),public.seal_materials_and_dispatch_v89(jsonb,jsonb,jsonb,integer),public.complete_material_request_v89(jsonb,jsonb),public.protect_pinned_material_v89(),public.release_material_pins_v89() from public,anon,authenticated;
grant execute on function public.accept_relay_request_v89(jsonb),public.seal_materials_and_dispatch_v89(jsonb,jsonb,jsonb,integer),public.complete_material_request_v89(jsonb,jsonb) to service_role;
notify pgrst,'reload schema';
commit;
