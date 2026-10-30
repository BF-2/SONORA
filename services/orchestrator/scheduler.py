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

OPERATIONS={'separate','pitch','convert','mix','export','pipeline','comp','export_stems','svs'}
TERMINAL={'succeeded','failed','cancelled','blocked'}

class Scheduler:
    def __init__(self,store):
        self.store=store; self.queue=queue.PriorityQueue(); self.tokens={}; self.lock=threading.RLock(); self.serial=0
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
            params=copy.deepcopy(p['parameters']); params.update(body.get('parameters',{}))
            params.setdefault('pitch_edits',copy.deepcopy(p['pitch_edits']))
            params['pitch_edits']=note_edits(p['notes'])+params['pitch_edits']
            if operation=='svs':
                params.setdefault('notes',copy.deepcopy(p['notes']))
                params.setdefault('lyrics',copy.deepcopy(p['lyrics']))
                params.setdefault('language','ja')
                params.setdefault('duration_ticks',max((n.get('end_tick',n.get('end',0)) for n in params['notes']),default=0))
                for n in params['notes']:
                    start=n.get('start_tick',n.get('start',0))
                    unit=next((u for u in params['lyrics'] if u.get('start',0)<=start<u.get('end',0)),None)
                    if unit and not n.get('lyric'): n['lyric']=unit.get('text','');n['phonemes']=unit.get('pronunciation') or None
            source=body.get('input_artifact_id')
            if source and source not in [a['id'] for a in p['assets']]: raise ValueError('输入产物不属于当前工程。')
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
                msg=str(exc); code='execution_failed'
                if 'out of memory' in msg.lower(): code='out_of_memory'
                if '缺少' in msg or '未安装' in msg or 'unavailable' in msg.lower(): code='missing_dependency'
                self.update(job,state='blocked' if code=='missing_dependency' else 'failed',message=msg[:300],error={'code':code,'message':msg[-6000:],'recovery':'保留已完成产物；检查模型/日志后重试，或改用较短片段。'},stage='需要处理')
            finally: self.queue.task_done()

    def source(self,job,roles=None):
        p=job['project_snapshot']
        if job.get('input_artifact_id'):
            return self.store.get('artifacts',job['input_artifact_id'])
        for role in roles or ['guide','synthesized','vocal','original']:
            for a in reversed(p['assets']):
                if a.get('stem_role')==role: return self.store.get('artifacts',a['id'])
        raise ValueError('请先导入音频。')

    def model(self,identifier):
        model=next((m for m in resources().get('models',[]) if m['id']==identifier),None)
        if not model: raise ValueError('缺少已注册的兼容模型，请在声音与模型中导入。')
        if media.sha256(model['path'])!=model['model_hash']: raise ValueError('模型文件哈希已变更，请重新注册。旧缓存不可复用。')
        if model.get('index_path') and model.get('index_hash') and media.sha256(model['index_path'])!=model['index_hash']: raise ValueError('检索索引哈希变化，请重新注册。')
        return model

    def attach(self,job,artifacts,add_tracks=True):
        def mutate(p):
            for a in artifacts:
                if a['id'] not in [x['id'] for x in p['assets']]: p['assets'].append(a)
                if add_tracks and a['media_type'].startswith('audio'):
                    role=a['stem_role']
                    if role in ('vocal','instrumental','mix'):
                        for t in p['tracks']:
                            if t['role']==role: t['mute']=True
                    p['tracks'].append(dict(id=uid('track'),name=a['name'],role=role,artifact_id=a['id'],gain_db=0,pan=0,mute=False,solo=False,offset_ticks=round(a.get('source_offset',0)*TIMEBASE/a.get('source_sample_rate',a['sample_rate'])),effects=[]))
            p['graph'].append({'id':uid('node'),'job_id':job['id'],'type':job['operation'],'input_refs':[job.get('input_artifact_id')],'artifact_refs':[a['id'] for a in artifacts],'state':'succeeded','project_revision':job['project_revision'],'parameters':copy.deepcopy(job['parameters']),'dependency_ranges':'whole_node_context_unknown'})
        self.store.change(job['project_id'],mutate)
        job['artifact_ids']+= [a['id'] for a in artifacts]
        for a in artifacts: self.store.event('artifact.committed',job,{'artifact_id':a['id']})

    def engine(self,engine,job,source,cancel,pitch=None):
        config=resources(); envconfig=config.get(engine if engine in ('separation','svs') or engine=='pitch' and config.get('pitch') else 'applio',{})
        python=envconfig.get('python'); script=ROOT/'workers'/engine/'run.py'
        if os.environ.get('SONORA_DISABLE_MODEL_WORKERS')=='1': raise ValueError('模型 worker 已关闭，不能转换。')
        if not python or not Path(python).is_file() or not script.is_file(): raise ValueError(f'缺少 {engine} 执行环境，请运行资源准备脚本。')
        if engine in ('rvc','pitch'):
            ledger=ROOT/'engine-envs/resource-links.json'
            if ledger.exists():
                for item in json.loads(ledger.read_text(encoding='utf-8'))['files']:
                    if media.sha256(item['path'])!=item['sha256']:raise ValueError('推理代码/编码器/音高权重已改变，请重新准备资源；旧缓存不可使用。')
        model=self.model(job['parameters'].get('model_id')) if engine=='rvc' else None
        if model and float(job['parameters'].get('index_rate',0))>0 and not model.get('index_path'): raise ValueError('检索率大于零但模型缺少索引，请明确设为 0 或导入索引。')
        parameters=copy.deepcopy(job['parameters']); parameters['algorithm']=parameters.get('pitch_algorithm','rmvpe')
        if source:
            source_track=next((t for t in job['project_snapshot']['tracks'] if t['artifact_id']==source['id']),None)
            parameters['source_timeline_offset_ticks']=source_track.get('offset_ticks',0) if source_track else source.get('timeline_offset_ticks',0)
        if engine=='separation':
            selected=parameters.get('separation_model_id',config['separation']['default_model'])
            sep=next((m for m in config['separation']['models'] if m['id']==selected),None)
            if not sep or media.sha256(sep['path'])!=sep['model_hash'] or media.sha256(sep['config_path'])!=sep['config_hash']:
                raise ValueError('分离模型或配置已变更，需重新注册，旧缓存不能复用。')
        if engine=='svs':
            model=next((m for m in config['svs']['models'] if m['id']==parameters.get('svs_model_id',config['svs']['default_model'])),None)
            if not model: raise ValueError('缺少兼容的 SVS 声库。')
            for relative,item in model['components'].items():
                if media.sha256(Path(model['root'])/relative)!=item['sha256']: raise ValueError('SVS 声库文件已变化，请重新注册。')
        source_files={str(p.relative_to(ROOT)):media.sha256(p) for p in sorted((ROOT/'workers').rglob('*.py'))}
        model_snapshot=config.get('separation',{}) if engine=='separation' else model
        fingerprint={'source':source['sha256'] if source else None,'engine':engine,'worker_hash':media.hash_json(source_files),'resources':config,'model':model_snapshot,'parameters':parameters,'pitch':pitch['sha256'] if pitch else None}
        key=media.hash_json(fingerprint)
        cached=self.store.cache_get(key)
        if cached:
            try:
                outputs=[self.store.get('artifacts',a) for a in cached]
                for a in outputs: media.validate_artifact(a)
                self.store.event('cache.hit',job,{'artifact_ids':cached,'key':key})
                return outputs
            except (KeyError,ValueError,OSError): pass
        folder=DATA/'jobs'/job['id']/engine; folder.mkdir(parents=True,exist_ok=True)
        source_path=media.validate_artifact(source) if source else None
        request={'protocol_version':1,'job_id':job['id'],'input_path':str(source_path) if source_path else None,'output_dir':str(folder),'parameters':parameters,'model':model,'pitch_path':pitch['path'] if pitch else None,'resource_config_path':str(ROOT/'engine-envs/resources.json')}
        req=folder/'request.json'; response=folder/'response.json'; req.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
        self.update(job,state='running',stage={'separation':'分离人声与伴奏','pitch':'提取独立 F0','rvc':'执行真实声音转换','svs':'歌词与音符合成新演唱'}[engine],message='正在运行本地执行器',progress=None,progress_basis='unknown')
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1';env['PYTHONUTF8']='1';env['TORCH_FORCE_WEIGHTS_ONLY_LOAD']='1'
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
            if engine=='rvc':
                if item.get('processing_kind')!='model_inference': raise ValueError('转换执行器没有返回模型推理产物。')
                if parameters.get('pitch_edits') and not item.get('consumed_f0_hash'): raise ValueError('执行器未证明消费了目标 F0，拒绝标为修音完成。')
            outputs.append(media.commit(self.store,p,kind=item.get('processing_kind','dsp'),inputs=([source['id']] if source else [])+([pitch['id']] if pitch else []),job=job,worker=str(script.relative_to(ROOT)),engine=item.get('engine',engine),model_hash=item.get('model_hash') or (model['model_hash'] if model else None),role=item.get('role','pitch' if engine=='pitch' else 'vocal'),name=p.name,extra=extra))
        if not outputs: raise ValueError('执行器未产出有效结果。')
        self.store.cache_put(key,[a['id'] for a in outputs])
        return outputs

    def choose_pitch(self,job):
        specified=job['parameters'].get('pitch_artifact_id')
        if specified: return self.store.get('artifacts',specified)
        return next((a for a in reversed(job['project_snapshot']['assets']) if a.get('stem_role')=='pitch'),None)

    def convert(self,job,source,cancel,pitch=None):
        params=job['parameters']; depth=1
        if params.get('chain_candidate_id'):
            candidate=next((c for c in job['project_snapshot']['candidates'] if c['id']==params['chain_candidate_id']),None)
            if not candidate: raise ValueError('串联来源候选不存在。')
            source=self.store.get('artifacts',candidate['artifact_id']); depth=candidate['generation_depth']+1
        elif source.get('stem_role')=='converted': raise ValueError('重复转换必须显式选择串联候选。普通候选应从干净源生成。')
        track=next((t for t in job['project_snapshot']['tracks'] if t['artifact_id']==source['id']),None)
        timeline_offset=track.get('offset_ticks',0) if track else 0
        if job['project_snapshot']['task']=='rewrite' and source.get('stem_role') not in ('guide','synthesized'): raise ValueError('改词翻唱需要导入唱着新词的导唱或先合成新演唱。VC 本身不会生成新词。')
        requested_range=params.get('range_ticks')
        if requested_range:
            self.store.event('warning',job,{'message':'选区使用带上下文的独立短试听，不保证与整段渲染逐样本相同；最终拼选需检查接缝。'})
        pitch=pitch or self.choose_pitch(job)
        if pitch and source['id'] not in pitch.get('input_artifact_ids',[]): pitch=None
        if pitch is None:
            pitches=self.engine('pitch',job,source,cancel); self.attach(job,pitches,False); pitch=pitches[0]
        outputs=self.engine('rvc',job,source,cancel,pitch)
        for a in outputs:
            a['stem_role']='converted';a.setdefault('timeline_offset_ticks',timeline_offset); self.store.put('artifacts',a)
        self.attach(job,outputs,False)
        def mutate(p):
            candidate={'id':uid('candidate'),'name':f"候选 {len(p['candidates'])+1}",'source_artifact_id':source['id'],'artifact_id':outputs[0]['id'],'model_id':params['model_id'],'model_manifest_hash':self.model(params['model_id'])['model_hash'],'parameters':copy.deepcopy(params),'generation_depth':depth,'state':'succeeded','notes':'','selected':True,'created_at':now(),'project_revision':job['project_revision']}
            before=job['project_snapshot']
            unchanged=all(p.get(k)==before.get(k) for k in ('parameters','pitch_edits','notes','lyrics','selected_candidate_id'))
            current_tracks={t['id']:t for t in p['tracks']}
            unchanged=unchanged and all(current_tracks.get(t['id'])==t for t in before['tracks'])
            candidate['selected']=unchanged
            candidate['render_status']='current' if unchanged else 'previous_revision'
            if unchanged:
                for c in p['candidates']: c['selected']=False
                p['selected_candidate_id']=candidate['id']
                for t in p['tracks']:
                    if t['role'] in ('vocal','guide','synthesized','converted','original','mix'): t['mute']=True
            p['candidates'].append(candidate)
            p['tracks'].append(dict(id=uid('track'),name=candidate['name'],role='converted',artifact_id=outputs[0]['id'],gain_db=0,pan=0,mute=not unchanged,solo=False,offset_ticks=outputs[0].get('timeline_offset_ticks',timeline_offset),effects=[]))
        self.store.change(job['project_id'],mutate)
        return outputs

    def execute(self,job,cancel):
        op=job['operation']; folder=DATA/'jobs'/job['id']; folder.mkdir(parents=True,exist_ok=True)
        if op=='separate':
            outputs=self.engine('separation',job,self.source(job,['original']),cancel);self.attach(job,outputs);return outputs
        if op=='pitch':
            outputs=self.engine('pitch',job,self.source(job),cancel);self.attach(job,outputs,False);return outputs
        if op=='convert': return self.convert(job,self.source(job),cancel)
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
            job['project_snapshot']=self.store.get('projects',job['project_id'])
            mixed=self.mix_job(job,cancel)
            job['parameters']['artifact_id']=mixed[0]['id'];job['parameters']['model_converted']=True
            self.export(job,cancel)
            return mixed
        if op=='mix': return self.mix_job(job,cancel)
        if op=='export': return self.export(job,cancel)
        if op=='comp':
            p=job['project_snapshot'];params=job['parameters']
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
            self.store.change(job['project_id'],add_comp)
            return [a]
        if op=='export_stems': return self.export_stems(job,cancel)
        raise ValueError('未知操作。')

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
            self.export(job,cancel)
        job['parameters']=original_params
        return outputs

    def mix_job(self,job,cancel):
        self.update(job,state='running',stage='混音与效果',message='处理选定轨道；原混音默认不叠加')
        p=job['project_snapshot']; tracks=job['parameters'].get('tracks')
        if tracks is None:
            tracks=[t for t in p['tracks'] if t['role'] not in ('original','mix')]
            if not tracks: tracks=[t for t in p['tracks'] if t['role']=='original']
        owned={a['id'] for a in p['assets']}
        if any(t['artifact_id'] not in owned for t in tracks): raise ValueError('混音轨道不属于当前工程。')
        output=DATA/'jobs'/job['id']/'mix.wav';inputs=media.mix(self.store,tracks,output,cancel)
        a=media.commit(self.store,output,kind='dsp',inputs=inputs,job=job,role='mix',name='最终混音',extra={'bus_export_semantics':'post-fader/post-FX/stereo sum; no implicit master limiting; effect tails trimmed to timeline length'})
        self.attach(job,[a]);return [a]

    def has_conversion(self,artifact,visited=None):
        visited=visited or set()
        if artifact['id'] in visited: return False
        visited.add(artifact['id'])
        if artifact['processing_kind']=='model_inference' and artifact.get('stem_role') in ('converted','synthesized') and artifact.get('model_hash'):
            producing=self.store.get('jobs',artifact['job_id'])
            # A downstream failure must not invalidate an already checked inference artifact.
            return artifact['commit_state']=='committed' and (artifact.get('inference_stage_committed') or artifact['id'] in producing['artifact_ids'])
        return any(self.has_conversion(self.store.get('artifacts',i),visited) for i in artifact.get('input_artifact_ids',[]))

    def export(self,job,cancel):
        p=job['project_snapshot']; params=job['parameters']
        if job['operation'] in ('pipeline','export_stems'): p=self.store.get('projects',job['project_id'])
        selected=params.get('artifact_id') or job.get('input_artifact_id')
        a=self.store.get('artifacts',selected) if selected else next((x for x in reversed(p['assets']) if x.get('stem_role') in ('mix','converted','synthesized','original')),None)
        if not a or a['id'] not in [x['id'] for x in p['assets']]: raise ValueError('请先选择可导出的音频产物。')
        path=media.validate_artifact(a)
        if not a['media_type'].startswith('audio'): raise ValueError('所选产物不是音频。')
        converted=self.has_conversion(a)
        if params.get('model_converted') and not converted: raise ValueError('此产物没有成功的真实声音转换来源，不能命名为模型转换成品。')
        ext=params.get('format','wav').lower()
        if ext not in ('wav','flac','mp3'): raise ValueError('当前支持 WAV / FLAC / MP3。')
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
        codec={'wav':['-c:a','pcm_s24le'],'flac':['-c:a','flac'],'mp3':['-c:a','libmp3lame','-b:a','320k']}[ext]
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
        export={'id':uid('export'),'name':final.name,'path':str(final),'artifact_id':a['id'],'job_id':job['id'],'format':ext,'sample_rate':info['sample_rate'],'channels':info['channels'],'duration':info['duration'],'sha256':media.sha256(final),'processing_kind':'model_inference' if converted else a['processing_kind'],'created_at':now(),'measurements':measurements,'source_offset_ticks':start_tick,'bus_export_semantics':a.get('bus_export_semantics','raw artifact before track fader/FX')}
        self.store.change(job['project_id'],lambda project:project['exports'].append(export))
        return [a]
