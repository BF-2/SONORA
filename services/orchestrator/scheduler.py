from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import queue
import threading
import time
import shutil
from . import media
from .config import DATA, ROOT, TIMEBASE, resources, binary
from .store import now, uid, Conflict
from .editing import note_edits,compose
from .model_catalog import ModelCatalog
from .rvc_service import resolve_selection,selection_fields,normalized_job_parameters,render_signature,pitch_matches,StalePitchBinding
from .model_selection import resolve_binding
from .candidate_assembly import assemble_preview
from .mixing import mix_tracks,preview_key
from workers.rvc_parameters import pitch_analysis_parameters,parameter_hash,pitch_parameter_subset
from workers.pitch.engine import apply_target_parameters,shift_pitch_to_project
from workers.separation.options import separation_options
from .voices import VOICE_PARTS,PART_NAMES,voice_part,new_binding,sync_part_tracks,resolve_part,part_unchanged,invalidate_mix,mix_signature,covers_source

OPERATIONS={'separate','separate_harmony','pitch','convert','compare_versions','adopt_preview','convert_parts','mix','mix_preview','export','pipeline','comp','export_stems','svs'}
TERMINAL={'succeeded','failed','cancelled','blocked'}

class Scheduler:
    def __init__(self,store,catalog=None):
        self.store=store;self.catalog=catalog or ModelCatalog(store.path); self.queue=queue.PriorityQueue(); self.tokens={}; self.lock=threading.RLock(); self.serial=0
        for job in store.all('jobs'):
            if job['state'] not in TERMINAL:
                job.update(state='failed',error={'code':'interrupted','message':'上次会话中断；已提交产物保留，可重试。','recovery':'重试任务'},updated_at=now())
                store.put('jobs',job)
        self.thread=threading.Thread(target=self.loop,daemon=True,name='sonora-serial-worker'); self.thread.start()

    def submit(self,pid,body):
        with self.lock:
            p=self.store.get('projects',pid)
            key=body.get('idempotency_key')
            if not key or len(key)>128: raise ValueError('作业需要有效幂等键。')
            previous=self.store.idem(pid,key)
            if previous: return previous
            if body.get('revision')!=p['revision']: raise Conflict('工程已更新，请刷新后重试。')
            operation=body.get('operation')
            if operation not in OPERATIONS: raise ValueError('不支持的作业类型。')
            source=body.get('input_artifact_id')
            if source and source not in [a['id'] for a in p['assets']]: raise ValueError('输入产物不属于当前工程。')
            overrides=copy.deepcopy(body.get('parameters',{}))
            part=overrides.get('voice_part')
            if operation in ('pitch','convert','compare_versions','adopt_preview') and source and not part:
                part=voice_part(next(a for a in p['assets'] if a['id']==source))
            if part:
                if operation not in ('pitch','convert','compare_versions','adopt_preview'):raise ValueError('当前操作不能使用单声部转换参数。')
                binding,bound,params=resolve_part(p,part,overrides)
                if source and source!=bound['id']:
                    chosen=next(a for a in p['assets'] if a['id']==source)
                    if operation!='pitch' or voice_part(chosen)!=part:raise ValueError('输入与当前声部绑定不一致。')
                source=source or bound['id']
                if operation in ('convert','compare_versions') and binding['mode']!='convert':raise ValueError('该声部当前设为保留原声或静音，请先选择转换。')
                params['pitch_edits']=note_edits(binding.get('notes',[]))+params['pitch_edits']
            else:
                params=copy.deepcopy(p['parameters']);params.update(overrides)
                params.setdefault('pitch_edits',copy.deepcopy(p['pitch_edits']))
                params['pitch_edits']=note_edits(p['notes'])+params['pitch_edits']
                if operation in ('convert','pitch','compare_versions','adopt_preview'):
                    source=source or params.get('source_artifact_id')
                    params,source=resolve_binding(p,params,source)
                    if source and source not in [a['id'] for a in p['assets']]:raise ValueError('绑定输入不属于当前工程。')
            if operation in ('convert','compare_versions') and overrides.get('full_source'):
                params.pop('range_ticks',None)
                params['ignore_clip_bindings']=True
            # An explicit reset must win over a saved track/clip F0 binding.
            if 'pitch_artifact_id' in overrides:params['pitch_artifact_id']=overrides['pitch_artifact_id']
            if operation=='compare_versions':
                selected=params.get('model_ids')
                if not isinstance(selected,list) or not 2<=len(selected)<=8 or len(set(selected))!=len(selected):raise ValueError('请选择同一训练实验的2–8个不同推理版本。')
                region=params.get('range_ticks')
                if not params.get('full_source') and (not isinstance(region,list) or len(region)!=2 or any(type(v) is not int for v in region) or region[0]<0 or not 4800<=region[1]-region[0]<=60*TIMEBASE):raise ValueError('版本对比需要0.1–60秒的明确同源选区，建议8–15秒。')
            if operation=='convert_parts':
                if params.get('range_ticks'):raise ValueError('双声部批处理采用完整声部；短试听请在对应声部单独运行。')
                selected=params.get('parts',list(VOICE_PARTS))
                if not isinstance(selected,list) or not selected or len(set(selected))!=len(selected) or set(selected)-set(VOICE_PARTS):raise ValueError('请选择主唱和/或和声。')
                for selected_part in selected:
                    binding,_,_=resolve_part(p,selected_part)
                    if binding['mode']=='convert' and not binding.get('parameters',{}).get('model_id'):raise ValueError(f'请为{PART_NAMES[selected_part]}选择声音模型。')
                if source:raise ValueError('分别转换使用各自声部绑定，不能指定同一个公共输入。')
            if operation=='svs':
                params.setdefault('notes',copy.deepcopy(p['notes']))
                params.setdefault('lyrics',copy.deepcopy(p['lyrics']))
                params.setdefault('language','ja')
                params.setdefault('duration_ticks',max((n.get('end_tick',n.get('end',0)) for n in params['notes']),default=0))
                for n in params['notes']:
                    start=n.get('start_tick',n.get('start',0))
                    unit=next((u for u in params['lyrics'] if u.get('start',0)<=start<u.get('end',0)),None)
                    if unit and not n.get('lyric'): n['lyric']=unit.get('text','');n['phonemes']=unit.get('pronunciation') or None
            job=dict(id=uid('job'),schema_version=1,revision=1,project_id=pid,project_revision=p['revision'],operation=operation,idempotency_key=key,state='queued',progress=None,progress_basis='unknown',stage='等待执行',message='已排队',parameters=params,input_artifact_id=source,artifact_ids=[],created_at=now(),updated_at=now(),error=None,attempts=1,project_snapshot=copy.deepcopy(p))
            job['plan_hash']=media.hash_json({'operation':operation,'project_revision':p['revision'],'parameters':params,'source':source})
            self.store.put('jobs',job); self.tokens[job['id']]=threading.Event(); self.serial+=1
            self.queue.put((0 if params.get('range_ticks') else 1,self.serial,job['id']))
            self.store.event('job.queued',job)
            return job

    def cancel(self,jid):
        job=self.store.get('jobs',jid)
        if job['state'] not in TERMINAL:
            token=self.tokens.get(jid)
            if token: token.set()
            if job['state']=='queued': self.update(job,state='cancelled',message='已取消')
        return self.store.get('jobs',jid)

    def update(self,job,**fields):
        job.update(fields,updated_at=now()); self.store.put('jobs',job)
        self.store.event('job.'+job['state'],job,{'stage':job['stage'],'progress':job['progress'],'message':job['message'],'error':job.get('error')})

    def loop(self):
        while True:
            _,_,jid=self.queue.get()
            job=self.store.get('jobs',jid); cancel=self.tokens[jid]
            try:
                if cancel.is_set(): raise media.Cancelled()
                self.update(job,state='preparing',stage='验证不可变输入',message='正在检查素材和引擎')
                outputs=self.execute(job,cancel)
                if cancel.is_set(): raise media.Cancelled()
                self.update(job,state='succeeded',stage='完成',progress=1,progress_basis='nodes',message='产物已校验并提交',artifact_ids=list(dict.fromkeys(job['artifact_ids']+[a['id'] for a in outputs])))
            except media.Cancelled:
                self.update(job,state='cancelled',stage='已取消',message='已停止，之前提交的产物仍可使用')
            except Exception as exc:
                msg=str(exc); code=getattr(exc,'code','execution_failed')
                if 'out of memory' in msg.lower(): code='out_of_memory'
                if '缺少' in msg or '未安装' in msg or 'unavailable' in msg.lower(): code='missing_dependency'
                self.update(job,state='blocked' if code=='missing_dependency' else 'failed',message=msg[:300],error={'code':code,'message':msg[-6000:],'recovery':getattr(exc,'recovery','保留已完成产物；检查模型/日志后重试，或改用较短片段。')},stage='需要处理')
            finally: self.queue.task_done()

    def source(self,job,roles=None):
        p=job['project_snapshot']
        if job.get('input_artifact_id'):
            return self.store.get('artifacts',job['input_artifact_id'])
        chosen=job['parameters'].get('source_artifact_id')
        if chosen and roles is None:
            if chosen not in {a['id'] for a in p['assets']}:raise ValueError('所选输入不属于当前工程。')
            return self.store.get('artifacts',chosen)
        for role in roles or ['guide','synthesized','vocal','original']:
            for a in reversed(p['assets']):
                if a.get('stem_role')==role: return self.store.get('artifacts',a['id'])
        raise ValueError('请先导入音频。')

    def model(self,identifier,index_id=None):
        if not identifier:raise ValueError('缺少已注册的兼容模型，请选择声音与版本。')
        model=self.catalog.resolve_model(identifier,index_id)
        if media.sha256(model['path'])!=model['model_hash']: raise ValueError('模型文件哈希已变更，请重新注册。旧缓存不可复用。')
        if model.get('index_path') and model.get('index_hash') and media.sha256(model['index_path'])!=model['index_hash']: raise ValueError('检索索引哈希变化，请重新注册。')
        return model

    def attach(self,job,artifacts,add_tracks=True,parameters=None):
        def mutate(p):
            roles={a.get('stem_role') for a in artifacts}
            if add_tracks and roles in ({'vocal','instrumental'},set(VOICE_PARTS)):
                from .separation_versions import register
                register(p,job,artifacts)
                return
            is_pair=any(a['stem_role'] in VOICE_PARTS for a in artifacts)
            before=job['project_snapshot']
            can_bind=not is_pair or (p.get('voice_parts',{})==before.get('voice_parts',{}) and
                all(next((t for t in p['tracks'] if t['id']==old['id']),None)==old for old in before['tracks'] if old['role']=='vocal' or voice_part(old)) and
                next((a['id'] for a in reversed(p['assets']) if a['stem_role']=='vocal'),None)==next((a['id'] for a in reversed(before['assets']) if a['stem_role']=='vocal'),None))
            for a in artifacts:
                if a['id'] not in [x['id'] for x in p['assets']]: p['assets'].append(a)
                if add_tracks and a['media_type'].startswith('audio'):
                    role=a['stem_role']
                    if role in VOICE_PARTS and any(t['artifact_id']==a['id'] and t['role']==role for t in p['tracks']):continue
                    if role in ('vocal','instrumental','mix',*VOICE_PARTS) and (role not in VOICE_PARTS or can_bind):
                        for t in p['tracks']:
                            if t['role']==role: t['mute']=True
                    p['tracks'].append(dict(id=uid('track'),name=a['name'],role=role,voice_part=voice_part(a),artifact_id=a['id'],gain_db=0,pan=0,mute=is_pair and not can_bind,solo=False,offset_ticks=a.get('timeline_offset_ticks',round(a.get('source_offset',0)*TIMEBASE/a.get('source_sample_rate',a['sample_rate']))),effects=[]))
            if is_pair and can_bind:
                bindings=p.setdefault('voice_parts',{})
                for a in artifacts:
                    if a['stem_role'] in VOICE_PARTS:bindings[a['stem_role']]=new_binding(a,bindings.get(a['stem_role']))
                sync_part_tracks(p)
                invalidate_mix(p,'主唱/和声分轨已更新')
            elif is_pair:
                p['warnings'].append({'code':'separation_previous_revision','artifact_ids':[a['id'] for a in artifacts],'message':'分离期间工程声部已修改，本次分离结果保留且静音，没有覆盖新的绑定。重新运行分离可从缓存采用。'})
            if add_tracks and not is_pair and any(a.get('stem_role') in ('vocal','instrumental','guide','synthesized') for a in artifacts):invalidate_mix(p,'上游音轨已更新，请更新混音')
            context=parameters or job['parameters']
            node_type='mix' if any(a['stem_role']=='mix' for a in artifacts) else 'pitch' if all(a['stem_role']=='pitch' for a in artifacts) else 'convert' if all(a['stem_role']=='converted' for a in artifacts) else job['operation']
            p['graph'].append({'id':uid('node'),'job_id':job['id'],'type':node_type,'voice_part':context.get('voice_part'),'input_refs':list(dict.fromkeys(ref for a in artifacts for ref in a.get('input_artifact_ids',[]))),'artifact_refs':[a['id'] for a in artifacts],'state':'succeeded','project_revision':job['project_revision'],'parameters':copy.deepcopy(context),'dependency_ranges':'whole_node_context_unknown'})
        self.store.change(job['project_id'],mutate)
        job['artifact_ids']+= [a['id'] for a in artifacts]
        for a in artifacts: self.store.event('artifact.committed',job,{'artifact_id':a['id']})

    def engine(self,engine,job,source,cancel,pitch=None,parameters=None):
        requested=parameters or job['parameters']
        config=resources(); envconfig=config.get(engine if engine in ('separation','svs') or engine=='pitch' and config.get('pitch') else 'applio',{})
        python=envconfig.get('python'); script=ROOT/'workers'/engine/'run.py'
        if os.environ.get('SONORA_DISABLE_MODEL_WORKERS')=='1': raise ValueError('模型 worker 已关闭，不能转换。')
        if not python or not Path(python).is_file() or not script.is_file(): raise ValueError(f'缺少 {engine} 执行环境，请运行资源准备脚本。')
        if engine in ('rvc','pitch'):
            ledger=ROOT/'engine-envs/resource-links.json'
            if ledger.exists():
                for item in json.loads(ledger.read_text(encoding='utf-8'))['files']:
                    if media.sha256(item['path'])!=item['sha256']:raise ValueError('推理代码/编码器/音高权重已改变，请重新准备资源；旧缓存不可使用。')
        if engine=='rvc':
            model=resolve_selection(self.catalog,requested)
            parameters,parameter_audit=normalized_job_parameters(requested,model,external=bool(pitch))
            parameters.update(selection_fields(model,parameters))
        else:
            model=None;parameters=copy.deepcopy(requested)
        if model and float(requested.get('index_rate',0))>0 and not model.get('index_path'): raise ValueError('检索率大于零但模型缺少索引，请明确设为 0 或导入索引。')
        parameters['algorithm']=parameters.get('pitch_algorithm','rmvpe')
        if source:
            source_track=next((t for t in job['project_snapshot']['tracks'] if t['artifact_id']==source['id']),None)
            parameters['source_timeline_offset_ticks']=source_track.get('offset_ticks',0) if source_track else source.get('timeline_offset_ticks',0)
        if engine=='separation':
            parameters.update(separation_options(parameters))
            harmony=parameters.get('separation_task')=='lead_backing'
            selected=parameters.get('harmony_model_id',config['separation'].get('default_harmony_model')) if harmony else parameters.get('separation_model_id',config['separation']['default_model'])
            sep=next((m for m in config['separation']['models'] if m['id']==selected),None)
            if not sep:raise ValueError('缺少已准备的主唱/和声模型。' if harmony else '缺少已准备的分离模型。')
            if (sep.get('task')=='lead_backing')!=harmony:raise ValueError('分离模型任务不匹配，不能混淆人声/伴奏与主唱/和声。')
            if not sep or media.sha256(sep['path'])!=sep['model_hash'] or media.sha256(sep['config_path'])!=sep['config_hash']:
                raise ValueError('分离模型或配置已变更，需重新注册，旧缓存不能复用。')
        if engine=='svs':
            model=next((m for m in config['svs']['models'] if m['id']==parameters.get('svs_model_id',config['svs']['default_model'])),None)
            if not model: raise ValueError('缺少兼容的 SVS 声库。')
            for relative,item in model['components'].items():
                if media.sha256(Path(model['root'])/relative)!=item['sha256']: raise ValueError('SVS 声库文件已变化，请重新注册。')
        source_files={p.relative_to(ROOT).as_posix():media.sha256(p) for p in sorted((ROOT/'workers').rglob('*.py'))}
        model_snapshot=config.get('separation',{}) if engine=='separation' else model
        fingerprint={'source':source['sha256'] if source else None,'engine':engine,'worker_hash':media.hash_json(source_files),'resources':config,'model':model_snapshot,'parameters':parameters,'pitch':pitch['sha256'] if pitch else None}
        if engine=='pitch':
            fingerprint={'source':source['sha256'],'engine':'pitch','analysis_parameters':pitch_analysis_parameters(parameters),
                         'worker_code':{name:digest for name,digest in source_files.items() if name.startswith('workers/pitch/') or name in ('workers/common.py','workers/rvc_parameters.py')},
                         'runtime':config.get('pitch',config.get('applio',{})),'predictor_bundle':config.get('applio',{}).get('source_hash')}
        elif engine=='separation':
            fingerprint={'source':source['sha256'],'engine':'separation','worker_code':{name:digest for name,digest in source_files.items() if name.startswith('workers/separation/') or name=='workers/common.py'},
                         'model_hash':sep['model_hash'],'config_hash':sep['config_hash'],'runtime':config.get('separation',{}).get('version'),
                         'parameters':{key:parameters.get(key) for key in ('separation_task','segment_size','overlap_step_seconds','separation_algorithm','primary_gain','device','cpu_threads')}}
        elif engine=='rvc':
            fingerprint={'source':source['sha256'],'engine':'rvc','worker_code':source_files,'runtime':config.get('applio',{}),
                         'model_hash':model['model_hash'],'index_hash':model.get('index_hash') if parameters['index_rate']>0 else None,
                         'architecture':model.get('architecture'),'sample_rate':model.get('sample_rate'),'embedder':model.get('embedder_model'),
                         'parameters':parameter_audit['effective_parameters'],'pitch':pitch['sha256'] if pitch else None,
                         'edits':parameters.get('pitch_edits',[]),'range_ticks':parameters.get('range_ticks'),
                         'source_timeline_offset_ticks':parameters.get('source_timeline_offset_ticks',0)}
        key=media.hash_json(fingerprint)
        cached=self.store.cache_get(key)
        if cached:
            try:
                outputs=[self.store.get('artifacts',a) for a in cached]
                for a in outputs: media.validate_artifact(a)
                current_inputs=([source['id']] if source else [])+([pitch['id']] if pitch else [])
                aliases=[]
                for a in outputs:
                    scope_changed=engine in ('pitch','rvc') and a.get('voice_part')!=parameters.get('voice_part')
                    if engine in ('pitch','separation'):scope_changed=scope_changed or a.get('timeline_offset_ticks',0)!=parameters.get('source_timeline_offset_ticks',0)
                    if a.get('input_artifact_ids',[])!=current_inputs or scope_changed:
                        alias=copy.deepcopy(a)
                        alias.update(id=uid('artifact'),input_artifact_ids=current_inputs,cache_source_artifact_id=a['id'],cache_consuming_job_id=job['id'],created_at=now(),updated_at=now())
                        if engine in ('pitch','rvc'):alias['voice_part']=parameters.get('voice_part')
                        if engine in ('pitch','separation'):alias['timeline_offset_ticks']=parameters.get('source_timeline_offset_ticks',0)
                        self.store.put('artifacts',alias);aliases.append(alias)
                    else:aliases.append(a)
                outputs=aliases
                if engine=='pitch':outputs=[self.refresh_pitch_target(job,a,source,parameters) for a in outputs]
                self.store.event('cache.hit',job,{'artifact_ids':[a['id'] for a in outputs],'cached_artifact_ids':cached,'key':key})
                return outputs
            except (KeyError,ValueError,OSError): pass
        suffix='-'+parameters['voice_part'] if parameters.get('voice_part') in VOICE_PARTS else ''
        folder=DATA/'jobs'/job['id']/(engine+suffix); folder.mkdir(parents=True,exist_ok=True)
        source_path=media.validate_artifact(source) if source else None
        request={'protocol_version':1,'job_id':job['id'],'input_path':str(source_path) if source_path else None,'output_dir':str(folder),'parameters':parameters,'model':model,'pitch_path':pitch['path'] if pitch else None,'resource_config_path':str(ROOT/'engine-envs/resources.json')}
        req=folder/'request.json'; response=folder/'response.json'; req.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        stage={'separation':'分离主唱与和声' if parameters.get('separation_task')=='lead_backing' else '分离人声与伴奏','pitch':'提取独立 F0','rvc':'执行真实声音转换','svs':'歌词与音符合成新演唱'}[engine]
        if parameters.get('voice_part') in VOICE_PARTS:stage=PART_NAMES[parameters['voice_part']]+' · '+stage
        self.update(job,state='running',stage=stage,message='正在运行本地执行器',progress=None,progress_basis='unknown')
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUTF8']='1';env['TORCH_FORCE_WEIGHTS_ONLY_LOAD']='1'
        # Shared runtimes are read-only. Numba otherwise probes their package
        # directories with temporary files and can retry indefinitely on Windows.
        numba_cache=DATA/'engine-cache'/'numba';scratch=folder/'temp'
        numba_cache.mkdir(parents=True,exist_ok=True);scratch.mkdir(exist_ok=True)
        env.update(NUMBA_CACHE_DIR=str(numba_cache.resolve()),TEMP=str(scratch.resolve()),TMP=str(scratch.resolve()))
        started=time.monotonic()
        media.run([python,script,'--request',req,'--response',response],cancel,folder/'worker.log',env=env,cwd=ROOT)
        if cancel.is_set(): raise media.Cancelled()
        if not response.is_file(): raise ValueError('执行器未返回产物清单。')
        result=json.loads(response.read_text(encoding='utf-8'))
        if not result.get('ok'): raise ValueError(str(result.get('error','执行器失败')))
        outputs=[]
        for item in result.get('outputs',[]):
            p=Path(item['path']).resolve()
            if not p.is_relative_to(folder.resolve()) or not p.is_file(): raise ValueError('执行器返回越界或缺失产物。')
            extra={k:v for k,v in item.items() if k not in ('path','role','processing_kind','engine','model_hash')}
            extra.update({'worker_metadata':result.get('metadata',{}),'warnings':result.get('warnings',[]),'elapsed_seconds':time.monotonic()-started,'cache_key':key,'inference_stage_committed':engine in ('rvc','svs','separation')})
            extra.setdefault('source_offset',source.get('source_offset',0) if source else 0)
            extra.setdefault('source_sample_rate',source.get('source_sample_rate',source['sample_rate']) if source else item.get('sample_rate',44100))
            if engine=='pitch':extra['timeline_offset_ticks']=parameters.get('source_timeline_offset_ticks',0)
            if parameters.get('voice_part') in VOICE_PARTS:extra['voice_part']=parameters['voice_part']
            if engine=='separation':extra.setdefault('timeline_offset_ticks',parameters.get('source_timeline_offset_ticks',0))
            if engine=='rvc':
                if item.get('processing_kind')!='model_inference': raise ValueError('转换执行器没有返回模型推理产物。')
                if parameters.get('pitch_edits') and not item.get('consumed_f0_hash'): raise ValueError('执行器未证明消费了目标 F0，拒绝标为修音完成。')
                extra['model_snapshot']={key:model.get(key) for key in ('id','canonical_id','model_artifact_id','model_hash','name','voice_id','run_id','epoch','global_step','version_label','epoch_source','architecture','content_dim','sample_rate','speaker_ids_trained','embedder_model','index_id','index_hash','index_match')}
                extra['effective_parameters']=result.get('metadata',{}).get('effective_parameters',{})
                extra['parameter_sources']=result.get('metadata',{}).get('parameter_sources',{})
            outputs.append(media.commit(self.store,p,kind=item.get('processing_kind','dsp'),inputs=([source['id']] if source else [])+([pitch['id']] if pitch else []),job=job,worker=str(script.relative_to(ROOT)),engine=item.get('engine',engine),model_hash=item.get('model_hash') or (model['model_hash'] if model else None),role=item.get('role','pitch' if engine=='pitch' else 'vocal'),name=p.name,extra=extra))
        if not outputs: raise ValueError('执行器未产出有效结果。')
        self.store.cache_put(key,[a['id'] for a in outputs])
        return outputs

    def refresh_pitch_target(self,job,artifact,source,parameters):
        data=json.loads(media.validate_artifact(artifact).read_text(encoding='utf-8'))
        raw={k:v for k,v in data.items() if k!='target_layer'}
        basis=shift_pitch_to_project(raw,parameters.get('source_timeline_offset_ticks',0))
        target=apply_target_parameters(basis,pitch_parameter_subset(parameters))
        if data.get('target_layer',{}).get('target_hash')==target.get('target_hash') and data.get('target_layer',{}).get('target_parameters_hash')==target.get('target_parameters_hash'):
            return artifact
        folder=DATA/'jobs'/job['id']/'pitch-target';folder.mkdir(parents=True,exist_ok=True)
        path=folder/(uid('target')+'.json');path.write_text(json.dumps({**raw,'target_layer':target},ensure_ascii=False,allow_nan=False),encoding='utf-8')
        extra={'worker_metadata':{**artifact.get('worker_metadata',{}),'target_hash':target.get('target_hash'),'target_parameters_hash':target.get('target_parameters_hash'),'cached_analysis_artifact_id':artifact['id']},
               'voice_part':parameters.get('voice_part'),'timeline_offset_ticks':parameters.get('source_timeline_offset_ticks',0),'analysis_parameters':data.get('analysis_parameters') or artifact.get('analysis_parameters')}
        return media.commit(self.store,path,kind='dsp',inputs=[source['id'],artifact['id']],job=job,worker='pitch-target-from-cache',engine='sonora-f0-target',role='pitch',name='音高分析与当前目标',extra=extra)

    def choose_pitch(self,job,source=None,parameters=None):
        params=parameters or job['parameters'];part=params.get('voice_part')
        specified=params.get('pitch_artifact_id')
        if specified:
            pitch=next((a for a in job['project_snapshot']['assets'] if a['id']==specified and a.get('stem_role')=='pitch'),None)
            if not pitch:raise ValueError('所选 F0 产物不属于当前工程或不是音高分析。')
            if source and source['id'] not in pitch.get('input_artifact_ids',[]):raise ValueError('所选 F0 不属于当前声部输入。')
            if not self.pitch_matches(pitch,params):raise StalePitchBinding('音高分析参数已改变，指定曲线是旧分析；请重新检测或清除旧分析绑定后试听。')
            return self.store.get('artifacts',specified)
        return next((a for a in reversed(job['project_snapshot']['assets']) if a.get('stem_role')=='pitch' and a.get('voice_part')==part and (source is None or source['id'] in a.get('input_artifact_ids',[])) and self.pitch_matches(a,params)),None)

    def pitch_matches(self,pitch,parameters):
        return pitch_matches(pitch,parameters,self.store)

    def listening_reference(self,job,source,parameters,offset,cancel):
        region=parameters.get('range_ticks')
        if not region:return source
        current=self.store.get('projects',job['project_id'])
        existing=next((a for a in current['assets'] if a.get('stem_role')=='preview_reference' and a.get('listening_source_id')==source['id'] and a.get('range_ticks')==region),None)
        if existing:
            media.validate_artifact(existing);return existing
        folder=DATA/'jobs'/job['id']/'listening-reference';folder.mkdir(parents=True,exist_ok=True)
        output=folder/(uid('reference')+'.wav')
        start,end=media.crop(source,output,region[0]-offset,region[1]-offset,cancel)
        a=media.commit(self.store,output,kind='dsp',inputs=[source['id']],job=job,worker='ffmpeg-reference-crop',engine='ffmpeg',role='preview_reference',name=source['name']+' · 同区间原声',
            extra={'listening_source_id':source['id'],'range_ticks':region,'source_offset':start,'source_sample_rate':source['sample_rate'],'timeline_offset_ticks':region[0],
                   'time_map':{'source_rate':source['sample_rate'],'output_rate':source['sample_rate'],'source_offset':start,'output_offset':0}})
        self.attach(job,[a],False,parameters);return a

    def convert(self,job,source,cancel,pitch=None,parameters=None):
        params=parameters or job['parameters']; depth=1;part=params.get('voice_part')
        if params.get('chain_candidate_id'):
            candidate=next((c for c in job['project_snapshot']['candidates'] if c['id']==params['chain_candidate_id']),None)
            if not candidate: raise ValueError('串联来源候选不存在。')
            if candidate.get('voice_part')!=part:raise ValueError('不能跨声部串联候选。')
            source=self.store.get('artifacts',candidate['artifact_id']); depth=candidate['generation_depth']+1
        elif source.get('stem_role')=='converted': raise ValueError('重复转换必须显式选择串联候选。普通候选应从干净源生成。')
        track=next((t for t in job['project_snapshot']['tracks'] if t['artifact_id']==source['id']),None)
        timeline_offset=track.get('offset_ticks',0) if track else 0
        if not part and job['project_snapshot']['task']=='rewrite' and source.get('stem_role') not in ('guide','synthesized'): raise ValueError('改词翻唱需要导入唱着新词的导唱或先合成新演唱。VC 本身不会生成新词。')
        requested_range=params.get('range_ticks')
        if requested_range:
            self.store.event('warning',job,{'message':'选区使用带上下文的独立短试听，不保证与整段渲染逐样本相同；最终拼选需检查接缝。'})
        pitch=pitch or self.choose_pitch(job,source,params)
        if pitch and source['id'] not in pitch.get('input_artifact_ids',[]): pitch=None
        if pitch is None:
            pitches=self.engine('pitch',job,source,cancel,parameters=params); self.attach(job,pitches,False,params); pitch=pitches[0]
        elif pitch.get('path'):
            refreshed=self.refresh_pitch_target(job,pitch,source,{**params,'source_timeline_offset_ticks':timeline_offset})
            if refreshed['id']!=pitch['id']:self.attach(job,[refreshed],False,params)
            pitch=refreshed
        outputs=self.engine('rvc',job,source,cancel,pitch,parameters=params)
        for a in outputs:
            a['stem_role']='converted';a.setdefault('timeline_offset_ticks',timeline_offset); self.store.put('artifacts',a)
        self.attach(job,outputs,False,params)
        identity=outputs[0].get('model_snapshot') or self.model(params['model_id'])
        reference=self.listening_reference(job,source,params,timeline_offset,cancel)
        def mutate(p):
            candidate={'id':uid('candidate'),'name':f"候选 {len(p['candidates'])+1}",'source_artifact_id':source['id'],'artifact_id':outputs[0]['id'],'model_id':params['model_id'],'model_manifest_hash':identity['model_hash'],'model_snapshot':copy.deepcopy(identity),'effective_parameters':outputs[0].get('effective_parameters',{}),'index_hash':identity.get('index_hash'),'index_id':identity.get('index_id'),'source_hash':source['sha256'],'parameters':copy.deepcopy(params),'generation_depth':depth,'state':'succeeded','notes':'','selected':True,'created_at':now(),'project_revision':job['project_revision']}
            candidate['comparison_id']=params.get('comparison_id')
            candidate['requested_f0_hash']=outputs[0].get('requested_f0_hash');candidate['consumed_f0_hash']=outputs[0].get('consumed_f0_hash')
            candidate['pitch_artifact_id']=pitch['id'] if pitch else None
            candidate['source_preview_artifact_id']=reference['id']
            candidate['render_signature']=render_signature(source,{**params,**selection_fields({**identity,'id':params['model_id']},params),'source_timeline_offset_ticks':timeline_offset})
            if identity.get('epoch') is not None:candidate['name']+=f" · Epoch {identity['epoch']}"
            before=job['project_snapshot']
            if part:
                candidate['name']=PART_NAMES[part]+' · '+candidate['name'];candidate['voice_part']=part
                candidate['part_source_artifact_id']=before['voice_parts'][part]['source_artifact_id']
                bound_source=next(a for a in before['assets'] if a['id']==candidate['part_source_artifact_id'])
                bound_track=next((t for t in before['tracks'] if t['artifact_id']==bound_source['id']),{})
                full=covers_source(outputs[0],bound_source,bound_track.get('offset_ticks',0))
                candidate['preview_only']=not full
                unchanged=full and part_unchanged(p,before,part) and not params.get('comparison_id') and params.get('adopt_result',True)
                candidate['selected']=unchanged;candidate['render_status']='preview_only' if not full else 'current' if unchanged else 'previous_revision'
                p['candidates'].append(candidate)
                source_track=track or {}
                adopted=before['voice_parts'][part].get('selected_candidate_id')
                adopted_candidate=next((c for c in before['candidates'] if c['id']==adopted),None)
                if adopted_candidate:
                    source_track=next((t for t in reversed(before['tracks']) if voice_part(t)==part and t['artifact_id']==adopted_candidate['artifact_id'] and not t.get('mute')),source_track)
                p['tracks'].append(dict(id=uid('track'),name=candidate['name'],role='converted',voice_part=part,candidate_id=candidate['id'],artifact_id=outputs[0]['id'],gain_db=source_track.get('gain_db',0),pan=source_track.get('pan',0),mute=True,solo=source_track.get('solo',False),offset_ticks=outputs[0].get('timeline_offset_ticks',timeline_offset),effects=copy.deepcopy(source_track.get('effects',[]))))
                if unchanged:
                    p['voice_parts'][part]['selected_candidate_id']=candidate['id']
                    sync_part_tracks(p,[part])
                    invalidate_mix(p,PART_NAMES[part]+'已采用新的转换候选')
                return
            unchanged=all(p.get(k)==before.get(k) for k in ('parameters','pitch_edits','notes','lyrics','selected_candidate_id'))
            current_tracks={t['id']:t for t in p['tracks']}
            if job['operation']=='pipeline':
                routing_keys={'source_artifact_id','harmony_source_artifact_id','active_voice_part','pitch_artifact_id'}
                unchanged=(all(p.get(k)==before.get(k) for k in ('pitch_edits','notes','lyrics','selected_candidate_id')) and
                           {k:v for k,v in p['parameters'].items() if k not in routing_keys}=={k:v for k,v in before['parameters'].items() if k not in routing_keys})
                unchanged=unchanged and p.get('last_user_audio_edit')==before.get('last_user_audio_edit') and all(current_tracks.get(t['id'])==t for t in before['tracks'] if t['role'] not in ('vocal','instrumental'))
            else:unchanged=unchanged and all(current_tracks.get(t['id'])==t for t in before['tracks'])
            unchanged=unchanged and not p.get('voice_parts')
            full=covers_source(outputs[0],source,timeline_offset)
            candidate['preview_only']=not full
            unchanged=unchanged and full and not params.get('comparison_id') and params.get('adopt_result',True)
            candidate['selected']=unchanged
            candidate['render_status']='current' if unchanged else 'previous_revision'
            if unchanged:
                for c in p['candidates']: c['selected']=False
                p['selected_candidate_id']=candidate['id']
                for t in p['tracks']:
                    if t['role'] in ('vocal','guide','synthesized','converted','original','mix'): t['mute']=True
            p['candidates'].append(candidate)
            p['tracks'].append(dict(id=uid('track'),candidate_id=candidate['id'],name=candidate['name'],role='converted',artifact_id=outputs[0]['id'],gain_db=0,pan=0,mute=not unchanged,solo=False,offset_ticks=outputs[0].get('timeline_offset_ticks',timeline_offset),effects=[]))
        self.store.change(job['project_id'],mutate,auto_archive=True)
        return outputs

    def execute(self,job,cancel):
        op=job['operation']; folder=DATA/'jobs'/job['id']; folder.mkdir(parents=True,exist_ok=True)
        if op=='separate':
            outputs=self.engine('separation',job,self.source(job,['original']),cancel);self.attach(job,outputs);return outputs
        if op=='separate_harmony':
            source=self.source(job,['vocal'])
            if source['stem_role']!='vocal':raise ValueError('主唱/和声分离需要混合人声；请先分离原曲或明确导入干人声。')
            params={**job['parameters'],'separation_task':'lead_backing'}
            outputs=self.engine('separation',job,source,cancel,parameters=params)
            if {a['stem_role'] for a in outputs}!=set(VOICE_PARTS):raise ValueError('执行器未完整返回主唱与和声，不改变现有绑定。')
            # Bind the pair in one project transaction only after both outputs validate.
            self.attach(job,outputs,parameters=params)
            return outputs
        if op=='pitch':
            outputs=self.engine('pitch',job,self.source(job),cancel);self.attach(job,outputs,False);return outputs
        if op=='convert': return self.convert(job,self.source(job),cancel)
        if op=='compare_versions':return self.compare_versions(job,cancel)
        if op=='adopt_preview':return self.adopt_preview(job,cancel)
        if op=='mix_preview':return self.mix_preview(job,cancel)
        if op=='convert_parts':
            return self.convert_parts(job,cancel)
        if op=='svs':
            outputs=self.engine('svs',job,None,cancel);self.attach(job,outputs);return outputs
        if op=='pipeline':
            source=self.source(job,['guide','synthesized'] if job['project_snapshot']['task']=='rewrite' else ['original','vocal'])
            if job['project_snapshot']['task'] in ('cover','rewrite') and source['stem_role']=='original' and not job['parameters'].get('skip_separation'):
                stems=self.engine('separation',job,source,cancel);self.attach(job,stems)
                source=next((a for a in stems if a['stem_role']=='vocal'),None)
                if source is None: raise ValueError('分离执行器没有声明主唱 stem。')
            pitches=self.engine('pitch',job,source,cancel);self.attach(job,pitches,False)
            self.convert(job,source,cancel,pitches[0])
            latest=self.store.get('projects',job['project_id'])
            if latest.get('last_user_audio_edit')!=job['project_snapshot'].get('last_user_audio_edit'):raise ValueError('制作期间工程已修改，已完成结果保留；请确认采用候选后重新混音。')
            latest['selection']=copy.deepcopy(job['project_snapshot']['selection'])
            mixed=self.mix_job(job,cancel,project_snapshot=latest)
            latest=self.store.get('projects',job['project_id']);latest['selection']=copy.deepcopy(job['project_snapshot']['selection'])
            self.export(job,cancel,parameters={**job['parameters'],'artifact_id':mixed[0]['id'],'model_converted':True},project_snapshot=latest)
            return mixed
        if op=='mix': return self.mix_job(job,cancel)
        if op=='export': return self.export(job,cancel)
        if op=='comp':
            p=job['project_snapshot'];params=job['parameters']
            if p.get('voice_parts'):raise ValueError('主唱/和声模式当前分别采用完整候选；旧完整人声拼选不能覆盖双声部混音。')
            base_candidate=next((c for c in p['candidates'] if c['id']==params.get('base_candidate_id',p['selected_candidate_id'])),None)
            if base_candidate and base_candidate.get('voice_part'):raise ValueError('主唱/和声当前独立选择完整候选；分段拼选请使用单声部工程。')
            path,inputs,seams=compose(self.store,p['candidates'],params.get('base_candidate_id',p['selected_candidate_id']),params.get('segments',[]),folder,cancel)
            a=media.commit(self.store,path,kind='dsp',inputs=inputs,job=job,engine='numpy/ffmpeg',worker='dsp_comp',role='converted',name='片段拼选',extra={'seam_ticks':seams,'seam_review':'needs_listening','bus_export_semantics':'same-source, timeline-preserving linear edge crossfades'})
            self.attach(job,[a],False)
            base=next(c for c in p['candidates'] if c['id']==params.get('base_candidate_id',p['selected_candidate_id']))
            def add_comp(project):
                candidate={**base,'id':uid('candidate'),'name':'片段拼选','artifact_id':a['id'],'parameters':copy.deepcopy(params),'selected':True,'notes':'请循环试听接缝；时间与总长度已校验。'}
                for c in project['candidates']:c['selected']=False
                project['candidates'].append(candidate);project['selected_candidate_id']=candidate['id']
                for t in project['tracks']:
                    if t['role'] in ('converted','original','vocal','guide','mix'):t['mute']=True
                project['tracks'].append(dict(id=uid('track'),name='片段拼选',role='converted',artifact_id=a['id'],gain_db=0,pan=0,mute=False,solo=False,offset_ticks=0,effects=[]))
            self.store.change(job['project_id'],add_comp,auto_archive=True)
            return [a]
        if op=='export_stems': return self.export_stems(job,cancel)
        raise ValueError('未知操作。')

    def compare_versions(self,job,cancel):
        params=copy.deepcopy(job['parameters'])
        if params.get('chain_candidate_id'):raise ValueError('Epoch对比必须从同一原始输入开始，串联请另行操作。')
        source=self.source(job)
        if source.get('stem_role')=='converted':raise ValueError('Epoch对比不能把上一轮转换结果作为默认输入。')
        region=params.get('range_ticks');track=next((t for t in job['project_snapshot']['tracks'] if t['artifact_id']==source['id']),{})
        offset=track.get('offset_ticks',0);end=offset+round(source['sample_count']*TIMEBASE/source['sample_rate'])
        if params.get('full_source'):region=[max(0,offset),end]
        if region[0]<max(offset,0) or region[1]>end:raise ValueError('版本对比选区超出输入音频。')
        models=[];chosen_index=params.get('index_id')
        for identifier in params['model_ids']:
            if cancel.is_set():raise media.Cancelled()
            self.update(job,state='preparing',stage='校验比较版本',message=f'验证 {identifier} 的权重、说话人与索引')
            selected_index=chosen_index if chosen_index else '' if float(params.get('index_rate',0))==0 or 'index_id' in params else None
            model=self.catalog.resolve_model(identifier,selected_index)
            if chosen_index is None and model.get('index_id'):chosen_index=model['index_id']
            models.append(model)
        if len({m['model_hash'] for m in models})!=len(models):raise ValueError('所选文件包含相同权重的复制品，不能冒充不同Epoch进行比较。')
        if len({m['run_id'] for m in models})!=1:raise ValueError('批量Epoch对比只接受同一训练实验，跨实验请建立普通候选。')
        for field in ('architecture','sample_rate','content_dim','embedder_model','f0_conditioned'):
            if len({str(m.get(field)) for m in models})!=1:raise ValueError('所选版本的 '+field+' 不兼容，不能使用同一对比配置。')
        base={**params,**selection_fields(models[0],params),'source_timeline_offset_ticks':offset,'adopt_result':False}
        base,accepted=normalized_job_parameters(base,models[0])
        for model in models[1:]:normalized_job_parameters({**base,**selection_fields(model,base)},model)
        comparison_id=uid('comparison');base['comparison_id']=comparison_id
        snapshot={'id':comparison_id,'schema_version':1,'job_id':job['id'],'state':'preparing','source_artifact_id':source['id'],'source_hash':source['sha256'],
                  'range_ticks':region,'model_ids':[m['id'] for m in models],'model_hashes':[m['model_hash'] for m in models],
                  'run_id':models[0]['run_id'],'parameter_snapshot':copy.deepcopy(base),'candidate_ids':[],'created_at':now(),'generation_depth':1}
        self.store.change(job['project_id'],lambda p:p.setdefault('comparisons',[]).append(snapshot))
        def update_comparison(**changes):
            def change(p):
                item=next(c for c in p['comparisons'] if c['id']==comparison_id);item.update(changes,updated_at=now())
            self.store.change(job['project_id'],change,history=False)
        outputs=[]
        try:
            pitch=self.choose_pitch(job,source,base)
            if pitch is None:
                pitch=self.engine('pitch',job,source,cancel,parameters=base)[0];self.attach(job,[pitch],False,base)
            else:
                pitch=self.refresh_pitch_target(job,pitch,source,base)
                self.attach(job,[pitch],False,base)
            update_comparison(state='running',pitch_artifact_id=pitch['id'],pitch_hash=pitch['sha256'])
            expected_f0=None;expected_consumed=None
            for index,model in enumerate(models):
                if cancel.is_set():raise media.Cancelled()
                variant={**base,**selection_fields(model,base)}
                rendered=self.convert(job,source,cancel,pitch=pitch,parameters=variant)
                actual=rendered[0]
                if expected_f0 is None:expected_f0=actual.get('requested_f0_hash');expected_consumed=actual.get('consumed_f0_hash')
                elif actual.get('requested_f0_hash')!=expected_f0 or actual.get('consumed_f0_hash')!=expected_consumed:raise ValueError('版本对比的实际F0条件不一致，拒绝标为同条件比较。')
                if not expected_f0 or not expected_consumed:raise ValueError('对比结果缺少实际消费F0证据。')
                outputs.extend(rendered)
                p=self.store.get('projects',job['project_id']);ids=[c['id'] for c in p['candidates'] if c.get('comparison_id')==comparison_id]
                update_comparison(candidate_ids=ids,completed_versions=index+1,requested_f0_hash=expected_f0,consumed_f0_hash=expected_consumed)
                self.store.event('comparison.version_completed',job,{'comparison_id':comparison_id,'model_id':model['id'],'completed':index+1,'total':len(models)})
            update_comparison(state='succeeded')
            return outputs
        except media.Cancelled:
            update_comparison(state='cancelled');raise
        except Exception as error:
            update_comparison(state='failed',error=str(error));raise

    def adopt_preview(self,job,cancel):
        params=job['parameters'];before=job['project_snapshot']
        folder=DATA/'jobs'/job['id']/'assembly'
        path,base,preview,assembly,offset=assemble_preview(self.store,before,params.get('candidate_id'),params.get('base_candidate_id'),folder,cancel,params.get('fade_ms',20))
        part=base.get('voice_part')
        if params.get('voice_part')!=part:raise ValueError('局部采用的候选不属于当前编辑声部。')
        artifact=media.commit(self.store,path,kind='dsp',inputs=[base['artifact_id'],preview['artifact_id']],job=job,worker='candidate-assembly',engine='ffmpeg/numpy',role='converted',name='局部采用 · '+preview['name'],
            extra={'assembly':assembly,'voice_part':part,'timeline_offset_ticks':offset,'bus_export_semantics':'same-source version preview inserted into full converted base; linear edge crossfade'})
        self.attach(job,[artifact],False)
        def mutate(project):
            c={**copy.deepcopy(preview),'id':uid('candidate'),'artifact_id':artifact['id'],'name':'局部采用 · '+preview['name'],'preview_only':False,'assembly':assembly,'comparison_id':None,
               'generation_depth':max(base['generation_depth'],preview['generation_depth']),'created_at':now(),'notes':'选区内采用试听版本，其余使用完整基础候选；请检查接缝。','selected':False}
            c['parameters'].pop('range_ticks',None)
            unchanged=part_unchanged(project,before,part) if part else all(project.get(k)==before.get(k) for k in ('parameters','pitch_edits','notes','selected_candidate_id')) and all(next((t for t in project['tracks'] if t['id']==old['id']),None)==old for old in before['tracks']) and not project.get('voice_parts')
            c['selected']=unchanged;project['candidates'].append(c)
            base_track=next((t for t in project['tracks'] if t['artifact_id']==base['artifact_id']),{})
            project['tracks'].append({'id':uid('track'),'name':c['name'],'role':'converted','voice_part':part,'candidate_id':c['id'],'artifact_id':artifact['id'],'gain_db':base_track.get('gain_db',0),'pan':base_track.get('pan',0),'effects':copy.deepcopy(base_track.get('effects',[])),'offset_ticks':offset,'mute':not unchanged,'solo':False})
            if unchanged:
                if part:
                    project['voice_parts'][part]['selected_candidate_id']=c['id'];sync_part_tracks(project,[part])
                else:
                    project['selected_candidate_id']=c['id']
                    for candidate in project['candidates']:candidate['selected']=candidate['id']==c['id']
                    for track in project['tracks']:
                        if track['role'] in ('original','vocal','guide','synthesized','converted','mix'):track['mute']=track['artifact_id']!=artifact['id']
                invalidate_mix(project,'已局部采用指定Epoch试听版本')
        self.store.change(job['project_id'],mutate,auto_archive=True)
        return [artifact]

    def convert_parts(self,job,cancel):
        snapshot=job['project_snapshot'];outputs=[]
        selected=job['parameters'].get('parts',list(VOICE_PARTS))
        for part in selected:
            if cancel.is_set():raise media.Cancelled()
            binding,source,parameters=resolve_part(snapshot,part)
            if binding['mode']!='convert':
                self.store.event('node.skipped',job,{'voice_part':part,'reason':binding['mode']})
                continue
            for key in ('device','range_ticks','seed'):
                if key in job['parameters']:parameters[key]=job['parameters'][key]
            parameters['pitch_edits']=note_edits(binding.get('notes',[]))+parameters['pitch_edits']
            outputs.extend(self.convert(job,source,cancel,parameters=parameters))
        if job['parameters'].get('mix_after'):
            if cancel.is_set():raise media.Cancelled()
            latest=self.store.get('projects',job['project_id'])
            # Mix the current explicit user selections. A stale newly rendered candidate
            # remains muted and cannot replace a selection made while the batch ran.
            mixed=self.mix_job(job,cancel,project_snapshot=latest,parameters={})
            outputs.extend(mixed)
        return outputs

    def export_stems(self,job,cancel):
        p=job['project_snapshot'];params=job['parameters'];semantics=params.get('semantics','post_fader_fx')
        if semantics not in ('raw','post_fader_fx'):raise ValueError('分轨导出语义必须明确。')
        tracks=[t for t in p['tracks'] if not t.get('mute') and t['role'] not in ('original','mix')]
        if any(t.get('solo') for t in tracks):tracks=[t for t in tracks if t.get('solo')]
        if not tracks:raise ValueError('没有已启用的人声或伴奏分轨。')
        length=max(round(self.store.get('artifacts',t['artifact_id'])['sample_count']*TIMEBASE/self.store.get('artifacts',t['artifact_id'])['sample_rate'])+t.get('offset_ticks',0) for t in tracks)
        outputs=[];folder=DATA/'jobs'/job['id']
        original_params=copy.deepcopy(job['parameters'])
        for i,t in enumerate(tracks):
            if cancel.is_set():raise media.Cancelled()
            self.update(job,state='running',stage='分轨导出',message=f'处理第 {i+1}/{len(tracks)} 轨',progress=i/len(tracks),progress_basis='tracks')
            settings={**t,'mute':False,'solo':False}
            if semantics=='raw':settings.update(gain_db=0,pan=0,effects=[])
            temporary=folder/f'stem-{i}.wav';media.mix(self.store,[settings],temporary,cancel)
            aligned=folder/f'stem-{i}-aligned.wav'
            media.run([binary('ffmpeg'),'-v','error',*media.safe_input(temporary),'-af',f'apad,atrim=end_sample={length}','-c:a','pcm_f32le','-y',str(aligned)],cancel)
            a=media.commit(self.store,aligned,kind='dsp',inputs=[t['artifact_id']],job=job,engine='ffmpeg',role='stem_export',name=t['name'],extra={'bus_export_semantics':semantics+'; no bus/send processing; common 48kHz stereo timeline'})
            self.attach(job,[a],False);outputs.append(a)
            job['parameters']={**original_params,'artifact_id':a['id'],'name':f"{p['name']} · {t['name']} · {semantics}",'selection_only':False,'model_converted':False}
            if semantics=='raw' and original_params.get('format','wav')=='wav':job['parameters']['wav_subtype']='pcm_f32le'
            self.export(job,cancel)
        job['parameters']=original_params
        return outputs

    def mix_job(self,job,cancel,project_snapshot=None,parameters=None):
        self.update(job,state='running',stage='混音与效果',message='处理选定轨道；原混音默认不叠加')
        p=project_snapshot or job['project_snapshot'];params=job['parameters'] if parameters is None else parameters;tracks=params.get('tracks')
        if tracks is None:
            tracks=mix_tracks(p)
        owned={a['id'] for a in p['assets']}
        if any(t['artifact_id'] not in owned for t in tracks): raise ValueError('混音轨道不属于当前工程。')
        output=DATA/'jobs'/job['id']/'mix.wav';inputs=media.mix(self.store,tracks,output,cancel)
        a=media.commit(self.store,output,kind='dsp',inputs=inputs,job=job,role='mix',name='最终混音',extra={'bus_export_semantics':'post-fader/post-FX/stereo sum; no implicit master limiting; effect tails trimmed to timeline length'})
        self.attach(job,[a])
        def mark_mix(current):
            if mix_signature(current)==mix_signature(p):current['warnings']=[w for w in current['warnings'] if w.get('code')!='pending_mix']
            else:invalidate_mix(current,'混音期间轨道或声部选择已变化，本次结果属于先前版本')
        self.store.change(job['project_id'],mark_mix,history=False)
        return [a]

    def mix_preview(self,job,cancel):
        p=job['project_snapshot'];params=job['parameters'];region=params.get('range_ticks')
        length=max((round(a['sample_count']*TIMEBASE/a['sample_rate'])+next((t.get('offset_ticks',0) for t in p['tracks'] if t['artifact_id']==a['id']),a.get('timeline_offset_ticks',0)) for a in p['assets'] if a.get('media_type','').startswith('audio') and a.get('stem_role')!='mix_preview'),default=0)
        if not isinstance(region,list) or len(region)!=2 or any(type(v) is not int for v in region) or region[0]<0 or region[1]>length or region[1]-region[0]<2400:raise ValueError('混音试听选区需要位于工程音频范围内，且至少50毫秒。')
        monitor=params.get('monitor_track_id');tracks=mix_tracks(p);key=preview_key(p,region,monitor)
        owned={a['id'] for a in p['assets']}
        if any(t['artifact_id'] not in owned for t in tracks):raise ValueError('混音轨道不属于当前工程。')
        if monitor:
            tracks=[{**t,'mute':False,'solo':False} for t in tracks if t['id']==monitor]
            if not tracks:raise ValueError('监听轨道不存在。')
        cached=self.store.cache_get(key);a=None
        if cached:
            try:a=self.store.get('artifacts',cached[0]);media.validate_artifact(a)
            except (KeyError,ValueError):a=None
        if a is None:
            self.update(job,state='running',stage='渲染当前混音选区',message='使用当前推子、声像、静音/独奏、偏移及效果')
            folder=DATA/'jobs'/job['id'];output=folder/'mix-preview.wav'
            inputs=media.mix(self.store,tracks,output,cancel,range_ticks=region)
            enabled=[t for t in tracks if not t.get('mute')]
            if any(t.get('solo') for t in enabled):enabled=[t for t in enabled if t.get('solo')]
            by_id={a['id']:a for a in p['assets']}
            bus_end=max((round(by_id[t['artifact_id']]['sample_count']*TIMEBASE/by_id[t['artifact_id']]['sample_rate'])+t.get('offset_ticks',0) for t in enabled),default=0)
            meters={}
            for i,t in enumerate(enabled):
                one=folder/f'meter-{i}.wav';media.mix(self.store,[t],one,cancel,range_ticks=region,timeline_end=bus_end)
                meters[t['id']]=media.meter_levels(one)
                one.unlink(missing_ok=True)
            a=media.commit(self.store,output,kind='dsp',inputs=inputs,job=job,role='mix_preview',name='当前混音选区试听',
                extra={'preview_key':key,'range_ticks':region,'timeline_offset_ticks':region[0],'monitor_track_id':monitor,'track_meters':meters,'master_meter':media.meter_levels(output),'measurements':media.loudness(output)})
            self.store.cache_put(key,[a['id']])
        # Monitoring is a cached job result, not a new project edit or export source.
        # Keeping it out of project snapshots avoids one large meter array per drag.
        job['artifact_ids'].append(a['id'])
        self.store.event('artifact.committed',job,{'artifact_id':a['id']})
        return [a]

    def has_conversion(self,artifact,visited=None):
        visited=visited or set()
        if artifact['id'] in visited: return False
        visited.add(artifact['id'])
        if artifact['processing_kind']=='model_inference' and artifact.get('stem_role') in ('converted','synthesized') and artifact.get('model_hash'):
            producing=self.store.get('jobs',artifact['job_id'])
            # A downstream failure must not invalidate an already checked inference artifact.
            return artifact['commit_state']=='committed' and (artifact.get('inference_stage_committed') or artifact['id'] in producing['artifact_ids'])
        return any(self.has_conversion(self.store.get('artifacts',i),visited) for i in artifact.get('input_artifact_ids',[]))

    def export(self,job,cancel,parameters=None,project_snapshot=None):
        p=project_snapshot or job['project_snapshot']; params=job['parameters'] if parameters is None else parameters
        if project_snapshot is None and job['operation']=='export_stems': p=self.store.get('projects',job['project_id'])
        selected=params.get('artifact_id') or job.get('input_artifact_id')
        a=self.store.get('artifacts',selected) if selected else next((x for x in reversed(p['assets']) if x.get('stem_role') in ('mix','converted','synthesized','original')),None)
        if not a or a['id'] not in [x['id'] for x in p['assets']]: raise ValueError('请先选择可导出的音频产物。')
        if a.get('stem_role')=='mix' and not params.get('allow_stale') and any(a['id'] in n.get('artifact_refs',[]) and n.get('state')=='stale' for n in p['graph']):raise ValueError('所选混音已因编辑失效，请重新混音；导出旧版本需显式允许 allow_stale。')
        path=media.validate_artifact(a)
        if not a['media_type'].startswith('audio'): raise ValueError('所选产物不是音频。')
        converted=self.has_conversion(a)
        if params.get('model_converted') and not converted: raise ValueError('此产物没有成功的真实声音转换来源，不能命名为模型转换成品。')
        ext=params.get('format','wav').lower()
        if ext not in ('wav','flac','mp3'): raise ValueError('当前支持 WAV / FLAC / MP3。')
        wav_subtype=params.get('wav_subtype','pcm_s24le')
        if wav_subtype not in ('pcm_s24le','pcm_s16le','pcm_f32le'):raise ValueError('不支持的 WAV 采样格式。')
        if not (ext=='wav' and wav_subtype=='pcm_f32le') and a.get('diagnostics',{}).get('sample_peak_dbfs',-240)>0 and not params.get('allow_clipping'):
            raise ValueError('所选音频峰值超过 0 dBFS；请降低推子后重新混音，或以浮点 WAV 保存原尺度，避免整数导出削波。')
        name=str(params.get('name') or ('模型转换成品' if converted else '原音频处理版'))
        name=''.join(c for c in name if c not in '<>:"/\\|?*' and ord(c)>=32).strip(' .')[:100] or 'SONORA'
        if name.upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}: name='SONORA_'+name
        folder=DATA/'jobs'/job['id']; output=folder/('export.'+ext)
        filters=[]; start_tick=int(a.get('timeline_offset_ticks',0))
        if params.get('selection_only'):
            selection=job['project_snapshot']['selection'];offset=int(a.get('timeline_offset_ticks',0))
            start_tick=max(selection['start'],offset);end=min(selection['end'],offset+round(a['sample_count']*TIMEBASE/a['sample_rate']))
            if end<=start_tick: raise ValueError('导出选区与所选产物没有交集。')
            filters=[f"atrim=start_sample={round((start_tick-offset)*a['sample_rate']/TIMEBASE)}:end_sample={round((end-offset)*a['sample_rate']/TIMEBASE)}",'asetpts=PTS-STARTPTS']
        args=[binary('ffmpeg'),'-v','error','-nostdin',*media.safe_input(path)]
        if filters: args+=['-af',','.join(filters)]
        codec={'wav':['-c:a',wav_subtype],'flac':['-c:a','flac'],'mp3':['-c:a','libmp3lame','-b:a','320k']}[ext]
        rate=int(params.get('sample_rate',a['sample_rate']))
        if rate not in (16000,22050,24000,32000,44100,48000,88200,96000): raise ValueError('不支持的导出采样率。')
        self.update(job,state='running',stage='编码与技术检查',message='验证输出后提交，遇同名文件另存版本')
        media.run(args+['-ar',str(rate),*codec,'-y',str(output)],cancel)
        info=media.probe(output)
        if info['duration']<=0: raise ValueError('导出没有有效时长。')
        measurements=media.loudness(output)
        if cancel.is_set(): raise media.Cancelled()
        final=DATA/'exports'/(name+'.'+ext);version=2
        while final.exists(): final=DATA/'exports'/f'{name} ({version}).{ext}';version+=1
        os.replace(output,final)
        export={'id':uid('export'),'name':final.name,'path':str(final),'artifact_id':a['id'],'job_id':job['id'],'format':ext,'codec':info['codec'],'sample_rate':info['sample_rate'],'channels':info['channels'],'duration':info['duration'],'sha256':media.sha256(final),'processing_kind':'model_inference' if converted else a['processing_kind'],'created_at':now(),'measurements':measurements,'source_offset_ticks':start_tick,'bus_export_semantics':a.get('bus_export_semantics','raw artifact before track fader/FX')}
        def record_export(project):
            project['exports'].append(export)
            project['graph'].append({'id':uid('node'),'job_id':job['id'],'type':'export','state':'succeeded','input_refs':[a['id']],'artifact_refs':[],
                                     'export_ref':export['id'],'project_revision':job['project_revision'],'parameters':copy.deepcopy(params),
                                     'dependency_ranges':'selected_export_range','checksum':export['sha256']})
        self.store.change(job['project_id'],record_export)
        return [a]
