"""Automatic deployment lifecycle; Excel is the sole authoring source.

No caller supplies release manifests, approval JSON or activation pointers.
History and signed validation observations live in the existing object store.
"""
import asyncio
from dataclasses import dataclass,replace
from hashlib import sha256
import hmac,json,time
from pathlib import Path
from app.control_plane.definitions import ModelControlPlane
from app.control_plane.governance.workbook import compile_workbook,COMPILER_VERSION
from app.control_plane.governance.deployment_validation import verify_supply,VALIDATOR_VERSION,ProbeFailure
from app.core.idempotency import stable_hash

def encode(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()

def serialize(plane):
    return {'revision':plane.revision,'status':plane.status,'release_metadata':dict(plane.release_metadata),
        **{name:[item.canonical() for _,item in sorted(getattr(plane,name).items())]
           for name in ('connections','capability_contracts','protocol_profiles','cache_policies','offerings')}}

def deserialize(value):
    parsers={'connections':'_connection_from_mapping','capability_contracts':'_contract_from_mapping','protocol_profiles':'_profile_from_mapping','cache_policies':'_cache_policy_from_mapping'}
    parts={name:[getattr(ModelControlPlane,parser)(item) for item in value[name]] for name,parser in parsers.items()}
    return ModelControlPlane(revision=value['revision'],status=value['status'],release_metadata=value['release_metadata'],
        **parts,offerings=[ModelControlPlane._offering_from_mapping(item,'*') for item in value['offerings']])

@dataclass
class WorkbookConfiguration:
    plane: object
    retained: list
    report: dict

def public_configuration_status(report):
    return {key:report[key] for key in ('source','state','compiler_version','workbook_sha256','configuration_hash',
        'ready_supplies','pending_supplies','control_plane_hash') if key in report}

async def prepare_configuration(settings, backend, *, verifier=verify_supply):
    report={'source':'excel','compiler_version':COMPILER_VERSION,'path':settings.model_workbook_file,'state':'ready','issues':[],'supplies':[]}
    base=ModelControlPlane._builtin(settings)
    empty=lambda:ModelControlPlane(revision='configuration-pending',connections=base.connections.values(),capability_contracts=base.capability_contracts.values(),
        protocol_profiles=base.protocol_profiles.values(),cache_policies=base.cache_policies.values(),offerings=[])
    prefix=settings.relay_storage_prefix+'/configuration/'+sha256(settings.deployment_id.encode()).hexdigest()[:20]+'/'
    archive_ok=True; retained=[]
    try:
        names=await asyncio.wait_for(backend.storage_list(prefix+'snapshots/'),settings.configuration_io_timeout_seconds)
        for name in names:
            if not name.endswith('.json'): continue
            raw=await asyncio.wait_for(backend.storage_get(prefix+'snapshots/'+name),settings.configuration_io_timeout_seconds)
            if sha256(raw).hexdigest()+'.json'!=name: raise ValueError('archive_digest_mismatch')
            retained.append(deserialize(json.loads(raw)))
    except Exception as exc:
        archive_ok=False;report['issues'].append({'stage':'snapshot_history','reason':type(exc).__name__})
    try:
        content=Path(settings.model_workbook_file).read_bytes()
        plane,targets=compile_workbook(content,settings)
        report.update(workbook_sha256=sha256(content).hexdigest(),configuration_hash=plane.release_metadata['configuration_hash'])
    except Exception as exc:
        report.update(state='pending',issues=report['issues']+[{'stage':'workbook_compile','reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}])
        return WorkbookConfiguration(empty(),retained,report)
    semaphore=asyncio.Semaphore(2)
    async def qualify(supply):
        location=targets[supply.offering_id]
        if not supply.enabled: return supply.offering_id,{'status':'disabled',**location}
        secret=plane.credential(supply.connection_id,settings)
        if not secret: return supply.offering_id,{'status':'pending','reason':'credential_missing',**location}
        if not archive_ok: return supply.offering_id,{'status':'pending','reason':'snapshot_history_unavailable',**location}
        target={'connection':plane.connection(supply.connection_id).canonical(),'contract':plane.contract(supply.capability_contract_id).canonical(),
            'profile':plane.protocol_profile(supply.protocol_profile_id).canonical(),
            'provider':supply.provider,'model':supply.model_pattern,'upstream_model':supply.metadata.get('upstream_model',supply.model_pattern),
            'execution_authorization':supply.metadata.get('execution_authorization',{}),'validator':VALIDATOR_VERSION}
        fingerprint=hmac.new(secret.encode(),encode(target),'sha256').hexdigest()
        path=prefix+'validation/'+fingerprint+'.json'
        result=None
        try:
            cached=json.loads(await asyncio.wait_for(backend.storage_get(path),settings.configuration_io_timeout_seconds))
            signature=cached.pop('signature',None)
            if (hmac.compare_digest(signature or '',hmac.new(secret.encode(),encode(cached),'sha256').hexdigest())
                    and cached.get('expires_at',0)>time.time() and cached.get('fingerprint')==fingerprint and cached.get('status')=='ready'):
                result={**cached,'cache_reused':True}
        except Exception: pass
        if result is None:
            async with semaphore:
                operation_path=prefix+'validation-operations/'+fingerprint+'.json'
                needs_operation='stateful_resource' in plane.contract(supply.capability_contract_id).cache.get('supported_mechanisms',[])
                claimed=False
                async def journal(record):
                    nonlocal claimed
                    if record.get('phase')=='cache_create_started':
                        try:
                            await asyncio.wait_for(backend.storage_put(operation_path,encode({**record,'target':stable_hash(target)}),upsert=False),settings.configuration_io_timeout_seconds)
                        except Exception:
                            raise ProbeFailure('cache_validation_operation_busy_or_unknown') from None
                        claimed=True
                    else:
                        if not claimed: raise ProbeFailure('cache_validation_operation_not_claimed')
                        await asyncio.wait_for(backend.storage_put(operation_path,encode(record)),settings.configuration_io_timeout_seconds)
                try:
                    args={'journal':journal} if verifier is verify_supply and needs_operation else {}
                    result=await asyncio.wait_for(verifier(settings,plane,supply,**args),settings.configuration_probe_timeout_seconds)
                except asyncio.TimeoutError: result={'status':'pending','reason':'validation_timeout'}
                except Exception as exc: result={'status':'pending','reason':'validation_'+type(exc).__name__}
                if claimed and result.get('status')=='ready':
                    try: await asyncio.wait_for(backend.storage_delete(operation_path),settings.configuration_io_timeout_seconds)
                    except Exception: pass
            if result.get('status')=='ready':
                record={**result,'fingerprint':fingerprint,'expires_at':int(time.time())+settings.configuration_verification_ttl_seconds}
                record['signature']=hmac.new(secret.encode(),encode(record),'sha256').hexdigest()
                try: await asyncio.wait_for(backend.storage_put(path,encode(record)),settings.configuration_io_timeout_seconds)
                except Exception: result={'status':'pending','reason':'validation_evidence_storage_unavailable'}
        return supply.offering_id,{**{k:v for k,v in result.items() if k not in ('signature','fingerprint','expires_at')},**location}
    async def bounded(supply):
        try: return await asyncio.wait_for(qualify(supply),settings.configuration_probe_timeout_seconds+settings.configuration_io_timeout_seconds)
        except asyncio.TimeoutError:
            return supply.offering_id,{'status':'pending','reason':'startup_validation_timeout',**targets[supply.offering_id]}
    results=dict(await asyncio.gather(*(bounded(s) for s in plane.offerings.values() if s.offering_id in targets)))
    report['supplies']=[{'offering_id':sid,**record} for sid,record in sorted(results.items())]
    report['ready_supplies']=sum(r['status']=='ready' for r in results.values())
    report['pending_supplies']=sum(r['status']=='pending' for r in results.values())
    contracts=dict(plane.capability_contracts); supplies=[]
    for supply in plane.offerings.values():
        if supply.offering_id in results:
            ready=results[supply.offering_id]['status']=='ready'
            if ready:
                contract=contracts[supply.capability_contract_id]
                cache={**contract.cache,'verification_status':'verified','mechanism_profiles':{
                    k:{**v,'verification_status':'verified'} for k,v in contract.cache.get('mechanism_profiles',{}).items()}}
                contracts[contract.contract_id]=replace(contract,cache=cache)
            supply=replace(supply,enabled=supply.enabled and ready,metadata={**supply.metadata,
                'deployment_validation_state':results[supply.offering_id]['status'],
                'deployment_validation_reason':results[supply.offering_id].get('reason')})
        supplies.append(supply)
    active=ModelControlPlane(revision=plane.revision,connections=plane.connections.values(),capability_contracts=contracts.values(),
        protocol_profiles=plane.protocol_profiles.values(),cache_policies=plane.cache_policies.values(),offerings=supplies,release_metadata=dict(plane.release_metadata))
    if any(r['status']=='pending' for r in results.values()): report['state']='partial'
    if not archive_ok: report['state']='pending'
    if archive_ok:
        raw=encode(serialize(active))
        try: await asyncio.wait_for(backend.storage_put(prefix+'snapshots/'+sha256(raw).hexdigest()+'.json',raw),settings.configuration_io_timeout_seconds)
        except Exception:
            report['state']='pending';report['issues'].append({'stage':'snapshot_archive','reason':'storage_unavailable'})
            # Do not admit new managed calls whose frozen client versions cannot be retained.
            for record in results.values():
                if record['status']=='ready': record.update(status='pending',reason='snapshot_archive_unavailable')
            report['supplies']=[{'offering_id':sid,**record} for sid,record in sorted(results.items())]
            report['ready_supplies']=0
            report['pending_supplies']=sum(r['status']=='pending' for r in results.values())
            active=ModelControlPlane(revision=active.revision,connections=active.connections.values(),capability_contracts=active.capability_contracts.values(),
                protocol_profiles=active.protocol_profiles.values(),cache_policies=active.cache_policies.values(),
                offerings=[replace(s,enabled=False,metadata={**s.metadata,
                    'deployment_validation_state':results[s.offering_id]['status'],
                    'deployment_validation_reason':results[s.offering_id].get('reason')}) if s.offering_id in targets else s
                    for s in active.offerings.values()],release_metadata=dict(active.release_metadata))
    report['control_plane_hash']=active.control_plane_hash
    return WorkbookConfiguration(active,retained,report)
