"""Excel-only deployment compiler. No network I/O or channel-specific branches."""
from dataclasses import replace
from hashlib import sha256
import re
import math
from string import Formatter
from urllib.parse import urlsplit
from app.control_plane.definitions import ModelControlPlane
from app.control_plane.governance.v2_import import DeclarationError, parse_v2_declarations
from app.control_plane.governance.snapshot_compiler import _PROVIDER_NAMES, _MODALITIES, _THINKING, _STRUCTURED
from app.core.idempotency import stable_hash

COMPILER_VERSION = 'relay-excel/8.7.0'
SUPPLY_OPTIONAL = ('供应组ID','允许租户','允许区域','最大活跃会话','预留预算上限','每会话预留预算')
PROFILE_OPTIONAL = ('证据来源','预期计费层级','要求原生约束证据','思考参数剥离响应头','思考降级响应头','有效思考模式响应头')
from app.control_plane.governance.thinking_mappings import FIELDS as THINKING_FIELDS, CONTRACT_OPTIONAL, compile_thinking_mappings

SCHEMAS = {
 '思考映射清单': THINKING_FIELDS,
 '环境映射': ('环境','部署ID','管理提供方'),
 '连接配置': ('连接ID','渠道名称','渠道ID','协议族','账号ID','项目ID','区域','REST端点','上传端点','SDK端点','API版本','鉴权方式','凭据引用'),
 '协议配置': ('协议ID','协议族','允许主机','亲和启用','亲和头','亲和模板','实际模型响应头','原生约束响应头','计费层级响应头'),
 '能力合同': ('合同ID','输入类型','思考投影','结构化输出','最大输出Token','材料传输','外链阈值MiB','显式缓存材料模式','最低温度','最高温度','最低TopP','最高TopP'),
 '供应配置': ('对外模型名','环境','渠道名称','连接ID','协议ID','合同ID'),
}

def extension_rows(wb):
    result = {}
    for name, fields in SCHEMAS.items():
        if name not in wb.sheetnames:
            continue
        ws = wb[name]
        if tuple(ws.cell(5,i).value for i in range(1,len(fields)+1)) != fields:
            raise DeclarationError(name+'!第5行: 表头与配置格式不符')
        if name == '能力合同':
            extras=tuple(ws.cell(5,i).value for i in range(len(fields)+1,len(fields)+len(CONTRACT_OPTIONAL)+1))
            while extras and extras[-1] is None: extras=extras[:-1]
            if extras!=CONTRACT_OPTIONAL[:len(extras)]: raise DeclarationError('能力合同: 可选思考约束表头顺序错误')
            fields=fields+extras
        if name == '供应配置':
            extras = tuple(ws.cell(5,i).value for i in range(len(fields)+1,len(fields)+len(SUPPLY_OPTIONAL)+1))
            while extras and extras[-1] is None: extras=extras[:-1]
            if extras != SUPPLY_OPTIONAL[:len(extras)]: raise DeclarationError('供应配置: 可选表头顺序错误')
            fields=fields+extras
        if name == '协议配置':
            extras=tuple(ws.cell(5,i).value for i in range(len(fields)+1,len(fields)+len(PROFILE_OPTIONAL)+1))
            while extras and extras[-1] is None:extras=extras[:-1]
            if extras!=PROFILE_OPTIONAL[:len(extras)]:raise DeclarationError('协议配置: 可选证据表头顺序错误')
            fields=fields+extras
        ws.reset_dimensions()
        rows=[]
        for row, values in enumerate(ws.iter_rows(min_row=6,max_col=len(fields),values_only=True),6):
            if all(v is None or str(v).strip()=='' for v in values): continue
            if any(isinstance(v,str) and v.lstrip().startswith('=') for v in values):
                raise DeclarationError(f'{name}!第{row}行: 配置字段不能使用公式')
            rows.append({**dict(zip(fields,values)), '_sheet':name, '_row':row})
        result[name]=rows
    return result

def fail(row, field, message):
    raise DeclarationError(f'{row["_sheet"]}!第{row["_row"]}行 {field}: {message}')

def value(row, field, optional=False):
    item=row.get(field)
    if item is None or str(item).strip()=='':
        if optional: return None
        fail(row,field,'不能为空')
    return str(item).strip()

def number(row, field, optional=False, minimum=0):
    item=row.get(field)
    if optional and item in (None,''): return None
    if isinstance(item,bool) or not isinstance(item,(int,float)) or item < minimum or int(item)!=item:
        fail(row,field,'必须是有效范围内的整数')
    return int(item)

def parts(text):
    return [x.strip() for x in re.split('[,、;；]',str(text or '')) if x.strip()]

def unique(rows, key):
    out={}
    for row in rows:
        identity=value(row,key)
        if identity in out: fail(row,key,'标识重复')
        out[identity]=row
    return out

def compile_workbook(content, settings):
    declarations=parse_v2_declarations(content)
    ext=declarations.get('extensions',{})
    if set(ext)!=set(SCHEMAS):
        raise DeclarationError('8.4.0 工作簿需要环境映射、连接配置、协议配置、能力合同和供应配置表')
    environments=unique(ext['环境映射'],'环境')
    scopes={}
    for env,row in environments.items():
        scopes[env]=(value(row,'部署ID'),parts(value(row,'管理提供方')))
    base=ModelControlPlane._builtin(settings)
    connections=dict(base.connections); profiles=dict(base.protocol_profiles); contracts=dict(base.capability_contracts); policies=dict(base.cache_policies)
    conn_rows=unique(ext['连接配置'],'连接ID')
    for cid,row in conn_rows.items():
        protocol=value(row,'协议族'); auth=value(row,'鉴权方式'); credential=value(row,'凭据引用')
        expected={'gemini_native':'x_goog_api_key_header','claude_messages':'x_api_key_header',
            'responses':'bearer_header','chat_completions':'bearer_header','moonshot_chat':'bearer_header'}
        if protocol not in expected or auth!=expected[protocol]: fail(row,'协议族/鉴权方式','没有匹配的已注册实现')
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',credential): fail(row,'凭据引用','必须是环境变量名称')
        values={key:value(row,field,optional=True) for key,field in (
            ('base_url','REST端点'),('rest_base_url','REST端点'),('upload_base_url','上传端点'),('account_id','账号ID'),('project_id','项目ID'),('region','区域'),('api_version','API版本'))}
        if not values['base_url'] or not values['account_id'] or not values['region']: fail(row,'连接边界','端点、账号和区域不能为空')
        for endpoint in [values['base_url'],values['upload_base_url'],value(row,'SDK端点',True)]:
            if endpoint:
                url=urlsplit(endpoint)
                if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
                    fail(row,'端点','必须使用不含凭据和查询参数的 HTTPS 地址')
        connections[cid]=ModelControlPlane._connection_from_mapping({**values,'connection_id':cid,'channel_id':value(row,'渠道ID'),
            'protocol':protocol,'auth_type':auth,'credential_ref':credential,'sdk_endpoint_options':{'base_url':value(row,'SDK端点',True) or values['base_url']}})
    for pid,row in unique(ext['协议配置'],'协议ID').items():
        hosts=parts(value(row,'允许主机'))
        if any('/' in x or '*' in x or ':' in x for x in hosts): fail(row,'允许主机','必须为精确主机名')
        affinity=value(row,'亲和启用')
        if affinity not in ('是','否'): fail(row,'亲和启用','只能填写是或否')
        policy={'enabled':affinity=='是'}
        if policy['enabled']:
            policy.update(header=value(row,'亲和头'),value_template=value(row,'亲和模板'),allowed_hosts=hosts)
            if not re.fullmatch('[A-Za-z0-9-]+',policy['header']) or any(field not in ('token','epoch','session_id')
                for _,field,_,_ in Formatter().parse(policy['value_template']) if field): fail(row,'亲和模板','包含不支持的占位符或请求头')
        evidence={}
        for key,field in [('actual_model','实际模型响应头'),('native_constraint_enforced','原生约束响应头'),('billing_tier','计费层级响应头'),('thinking_parameter_stripped','思考参数剥离响应头'),('thinking_degraded','思考降级响应头'),('effective_thinking_mode','有效思考模式响应头')]:
            header=value(row,field,True)
            if header: evidence[key]=header
        source=value(row,'证据来源',True) or 'gateway_headers'
        if source not in ('gateway_headers','first_party_native_payload'):fail(row,'证据来源','未知模式')
        required=value(row,'要求原生约束证据',True) or '否'
        if required not in ('是','否'):fail(row,'要求原生约束证据','只能填写是或否')
        if source=='first_party_native_payload' and (value(row,'协议族')!='gemini_native' or policy['enabled']):fail(row,'证据来源','需原生协议且不能启用网关亲和')
        profiles[pid]=ModelControlPlane._profile_from_mapping({'profile_id':pid,'revision':COMPILER_VERSION,'protocol':value(row,'协议族'),
            'cache':{},'metadata':{'allowed_hosts':hosts,'affinity_policy':policy,'response_evidence_headers':evidence,
                'evidence_source_mode':source,'expected_billing_tier':value(row,'预期计费层级',True),'require_native_constraint_evidence':required=='是'}})
    cap_rows=unique(ext['能力合同'],'合同ID')
    thinking_specs=compile_thinking_mappings(ext['思考映射清单'],cap_rows)
    bindings={}
    for row in ext['供应配置']:
        key=tuple(value(row,f) for f in ('对外模型名','环境','渠道名称'))
        if key in bindings: fail(row,'供应','同模型、环境和渠道只能登记一次')
        bindings[key]=row
    for cid,row in cap_rows.items():
        modalities=parts(value(row,'输入类型'))
        if any(x not in _MODALITIES for x in modalities): fail(row,'输入类型','包含未知选项')
        structured=value(row,'结构化输出')
        if structured not in _STRUCTURED: fail(row,'结构化输出','包含未知选项')
        transport=parts(value(row,'材料传输'))
        if not set(transport)<= {'provider_files_api','external_signed_url','inline_data'}: fail(row,'材料传输','尚无实现')
        thinking=thinking_specs[cid]
        maximum=number(row,'最大输出Token',minimum=1)
        options={'max_output_tokens':{'type':'integer','min':1,'max':maximum}}
        for option,low,high in [('temperature','最低温度','最高温度'),('top_p','最低TopP','最高TopP')]:
            a,b=row.get(low),row.get(high)
            if a in (None,'') and b in (None,''): continue
            if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in (a,b)) or a>b:
                fail(row,low+'/'+high,'必须是有效且有序的数值范围')
            options[option]={'type':'number','min':a,'max':b}
        contracts[cid]=ModelControlPlane._contract_from_mapping({'contract_id':cid,'revision':COMPILER_VERSION,
            'supported_options':options,
            'thinking':thinking,'structured_output':{'mode':{'native':'native_json_schema','post_validate':'post_validate','none':'none'}[_STRUCTURED[structured]]},
            'input_modalities':[_MODALITIES[x] for x in modalities], 'cache':{},
            'metadata':{'material_transport':{'supported_material_transports':transport,'external_url_max_total_bytes':number(row,'外链阈值MiB')*1024*1024}}})
    # Excel owns only explicitly managed provider scopes; compatibility seed is
    # retained for other families and for historical frozen client versions.
    managed={p for deployment,providers in scopes.values() if deployment==settings.deployment_id for p in providers}
    supplies={k:replace(v,enabled=False) if v.provider in managed else v for k,v in base.offerings.items()}
    model_rows={r['对外模型名']:r for r in declarations['models']}
    cache_rows={(r['对外模型名'],r['渠道名称']):r for r in declarations['caches']}
    groups={}; targets={}
    for index,route in enumerate(declarations['routes'],6):
        row={'_sheet':'渠道优先级','_row':route.get('_row',index),**route}
        model=row['对外模型名']; env=row['环境']; channel=row['渠道名称']; m=model_rows[model]
        provider=_PROVIDER_NAMES.get(m['提供方'],m['提供方'].lower())
        if env not in scopes: fail(row,'环境','环境映射表中未登记')
        deployment,providers=scopes[env]
        if provider not in providers or deployment!=settings.deployment_id: continue
        groups[(provider,model,deployment)]={'provider':provider,'model':model,'deployment_id':deployment}
        enabled=m['发布意向']=='申请发布' and row['启用意向']=='启用'
        binding=bindings.get((model,env,channel))
        if not binding:
            if enabled: fail(row,'渠道名称','缺少供应配置引用')
            continue
        connection=connections.get(value(binding,'连接ID')); profile=profiles.get(value(binding,'协议ID')); contract=contracts.get(value(binding,'合同ID'))
        if not connection or not profile or not contract: fail(binding,'连接ID/协议ID/合同ID','引用不存在')
        if connection.protocol!=profile.protocol: fail(binding,'协议ID','与连接协议不一致')
        if connection.protocol=='gemini_native' and not set(contract.metadata['material_transport']['supported_material_transports']) & {'provider_files_api','external_signed_url'}:
            fail(binding,'合同ID','当前原生材料绑定器需要 Files 或外链传输授权')
        from app.protocol_adapters.thinking_projection import validate_projection
        try: validate_projection(contract.thinking,connection.protocol)
        except ValueError as exc: fail(binding,'合同ID/思考投影','contract_id='+value(binding,'合同ID')+': '+str(exc))
        requirement={'input_modalities':[_MODALITIES[x] for x in parts(m['输入类型'])]}
        if not contract.supports_requirements(requirement)[0]: fail(binding,'合同ID','不能满足模型清单中的输入和思考要求')
        conn_row=conn_rows.get(connection.connection_id)
        if conn_row is None or value(conn_row,'渠道名称')!=channel: fail(binding,'渠道名称','与连接配置不一致')
        for url in filter(None,[connection.rest_base_url,connection.upload_base_url,connection.sdk_endpoint_options.get('base_url')]):
            if urlsplit(url).hostname not in profile.metadata['allowed_hosts']: fail(binding,'协议ID','主机授权与连接端点不符')
        cache=cache_rows.get((model,channel))
        if not cache: fail(row,'缓存设置','缺少缓存行')
        mechanisms=[]
        if cache['启用意向']=='启用' and cache['缓存机制']!='不使用': mechanisms=[{'显式资源':'stateful_resource','隐式前缀':'implicit_prefix'}[cache['缓存机制']]]
        from app.protocol_adapters.factory import RESOURCE_MECHANISMS
        if not set(mechanisms)<=RESOURCE_MECHANISMS[connection.protocol]: fail(row,'缓存机制','协议实现不支持')
        cap_id='excel-cap:'+stable_hash([model,env,channel])[:20]; policy_id='excel-policy:'+stable_hash([model,env,channel])[:20]
        source_cap=cap_rows[value(binding,'合同ID')]; file_mode=value(source_cap,'显式缓存材料模式')
        if file_mode not in ('inline_bytes_only','file_uri_allowed'): fail(source_cap,'显式缓存材料模式','未知模式')
        if mechanisms==['implicit_prefix']: file_mode='inline_bytes_only'
        if mechanisms and file_mode=='file_uri_allowed' and ('provider_files_api' not in contract.metadata['material_transport']['supported_material_transports'] or 'stateful_resource' not in mechanisms):
            fail(row,'缓存设置','file_uri_allowed 必须同时具备 Files 和显式资源能力')
        cache_contract={'version':'relay-cache-contract/1','supported_mechanisms':mechanisms,'verification_status':'unverified',
            'explicit_cache_material_mode':file_mode,'composite_mode':'mutually_exclusive',
            'mechanism_profiles':{mech:{'verification_status':'unverified','minimum_cacheable_tokens':cache['计划门槛 Token'] or 1024,
                'threshold_mode':'count_tokens' if mech=='stateful_resource' else 'provider_enforced','final_threshold_guard':True,
                'ttl_seconds':(cache['资源有效期 分钟'] or 60)*60} for mech in mechanisms}}
        contracts[cap_id]=replace(contract,contract_id=cap_id,cache=cache_contract)
        profile_id='excel-profile:'+stable_hash([profile.profile_id,connection.connection_id])[:20]
        profiles[profile_id]=replace(profile,profile_id=profile_id,cache={'supported_mechanisms':sorted(RESOURCE_MECHANISMS[connection.protocol]),
            **({'token_count_connection_id':connection.connection_id} if connection.protocol=='gemini_native' else {}),'composite_mode':'mutually_exclusive'})
        policies[policy_id]=ModelControlPlane._cache_policy_from_mapping({'policy_id':policy_id,'revision':COMPILER_VERSION,'scope':'session',
            'mechanism_preference':mechanisms,'auto_prepare_failure':'uncached_same_context' if cache['自动准备失败时']=='原文无缓存继续' else 'fail',
            'metadata':{'default_mode':{'关闭':'off','自动':'auto','强制':'on'}[cache['默认策略']] if mechanisms else 'off','allow_force':cache['可选强制']=='是'}})
        sid='excel:'+stable_hash([model,env,channel])[:24]
        quota={}
        for field,key in [('最大活跃会话','max_active_sessions'),('预留预算上限','budget_limit_units'),('每会话预留预算','reserve_units')]:
            n=number(binding,field,optional=True,minimum=1)
            if n is not None: quota[key]=n
        if bool(quota.get('budget_limit_units')) != bool(quota.get('reserve_units')): fail(binding,'预算','上限和每会话预留必须同时填写')
        quota.update({key:parts(binding.get(field)) for field,key in [('允许租户','allowed_tenants'),('允许区域','allowed_regions')] if binding.get(field)})
        supplies[sid]=ModelControlPlane._offering_from_mapping({'offering_id':sid,'provider':provider,'model_pattern':model,
            'supply_group_id':value(binding,'供应组ID',True),'quota':quota,
            'connection_id':connection.connection_id,'capability_contract_id':cap_id,'protocol_profile_id':profile_id,'cache_policy_id':policy_id,
            'deployment_id':deployment,'priority':10000-row['顺位'],'enabled':enabled,
            'requires_file_adapter':'provider_files_api' in contract.metadata['material_transport']['supported_material_transports'],
            'observed_model_policy':'strict','metadata':{'managed_v2':True,'upstream_model':row['渠道侧模型名'],
                'unavailable_action':'reject_new_session' if row['不可用时']=='拒绝新会话' else 'try_next',
                'execution_authorization':{'allow_provider_fallback':False,'allow_paid_fallback':False,
                    'evidence_source_mode':profile.metadata.get('evidence_source_mode','gateway_headers'),
                    'expected_billing_tier':profile.metadata.get('expected_billing_tier'),
                    'on_unknown_billing':'reject' if profile.metadata.get('expected_billing_tier') else 'audit',
                    'require_native_constraint_evidence':profile.metadata.get('require_native_constraint_evidence',False)}}},deployment)
        targets[sid]={'sheet':'渠道优先级','row':row['_row'],'model':model,'channel':channel,
            'connection_id':connection.connection_id,'channel_id':connection.channel_id,'protocol':connection.protocol,
            'endpoint':connection.rest_base_url or connection.base_url}
    group_scopes={}
    for s in supplies.values():
        if not s.supply_group_id: continue
        c=connections[s.connection_id]
        scope=(s.provider,s.model_pattern,s.deployment_id,c.channel_id,c.account_id,c.project_id,c.region,stable_hash(dict(s.quota)))
        old=group_scopes.setdefault(s.supply_group_id,scope)
        if old!=scope: raise DeclarationError('供应组不能跨模型、部署、渠道、账号、项目、区域或准入预算作用域')
    metadata={'source':'excel-auto','compiler_version':COMPILER_VERSION,'workbook_sha256':sha256(content).hexdigest(),
        'configuration_hash':stable_hash([sha256(content).hexdigest(),COMPILER_VERSION,settings.deployment_id]),'managed_model_groups':list(groups.values())}
    plane=ModelControlPlane(revision='excel-'+metadata['configuration_hash'][:20],connections=connections.values(),capability_contracts=contracts.values(),
        protocol_profiles=profiles.values(),cache_policies=policies.values(),offerings=supplies.values(),release_metadata=metadata)
    return plane,targets
