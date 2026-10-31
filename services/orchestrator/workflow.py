"""Read-only stage guidance derived from actual project state, not click history."""
from pathlib import Path
from .config import TIMEBASE
from .rvc_service import candidate_matches,pitch_analysis_state
from .mixing import mix_tracks

STAGES=[('import','导入诊断',['导入音频','确认媒体属性','选择任务','设置选区']),
        ('prepare','人声整理',['确认输入','选择整理路线','运行或跳过分离','独听检查产物']),
        ('pitch','音高与歌词',['确认人声源','检测音高','检查与编辑','锁定目标曲线']),
        ('convert','声音转换',['选择输入','选择声音与版本','检查索引和兼容性','调整参数','生成短试听','采用或完整生成']),
        ('mix','混音',['确认采用的人声','选择伴奏','调整推子与效果','渲染混音','检查电平']),
        ('export','导出',['选择有效产物','设置格式与范围','检查技术指标','导出文件与配方'])]

def scoped_context(project,part=None):
    binding=project.get('voice_parts',{}).get(part or '')
    parameters=binding.get('parameters',{}) if binding else project.get('parameters',{})
    chosen=binding.get('source_artifact_id') if binding else parameters.get('source_artifact_id')
    source=next((a for a in project['assets'] if a['id']==chosen),None)
    if source is None and not chosen:
        for role in ('guide','synthesized','vocal','original'):
            source=next((a for a in reversed(project['assets']) if a.get('stem_role')==role),None)
            if source:break
    return source,parameters,binding

def graph_stale(project,artifact_id):
    for node in reversed(project.get('graph',[])):
        if artifact_id in node.get('artifact_refs',[]):return node.get('state')=='stale'
    return False

def describe_workflow(project,model=None,part=None,store=None):
    source,params,binding=scoped_context(project,part)
    source_valid=bool(source and source.get('commit_state')=='committed' and Path(source['path']).is_file())
    setup_confirmed=bool(project['assets']) or bool(params.get('task_confirmed') and params.get('import_role'))
    raw=next((a for a in project['assets'] if a.get('stem_role')=='original'),source or next((a for a in project['assets'] if a.get('media_type','').startswith('audio')),None))
    enabled_tracks=[t for t in mix_tracks(project) if not t.get('mute')]
    if any(t.get('solo') for t in enabled_tracks):enabled_tracks=[t for t in enabled_tracks if t.get('solo')]
    pitch=next((a for a in reversed(project['assets']) if a.get('stem_role')=='pitch' and source and source['id'] in a.get('input_artifact_ids',[])),None)
    analysis=pitch_analysis_state(project,source,params,part if binding else None,store)
    candidates=[c for c in project['candidates'] if not c.get('archived') and c.get('voice_part')==(part if binding else None) and source and c.get('source_artifact_id')==source['id']]
    selected_id=binding.get('selected_candidate_id') if binding else project.get('selected_candidate_id')
    selected=next((c for c in candidates if c['id']==selected_id),None)
    def current_candidate(c):return bool(c.get('assembly') and c['id']==selected_id and not graph_stale(project,c['artifact_id'])) or candidate_matches(project,c,source,params,model) or not c.get('render_signature') and c.get('model_id')==params.get('model_id') and not graph_stale(project,c['artifact_id'])
    selected_fresh=bool(selected and current_candidate(selected))
    preview_fresh=next((c for c in reversed(candidates) if current_candidate(c)),None)
    mix=next((a for a in reversed(project['assets']) if a.get('stem_role')=='mix'),None)
    outputs={0:[a for a in project['assets'] if a.get('processing_kind') in ('decode','demo')],
             1:[a for a in project['assets'] if a.get('stem_role') in ('vocal','instrumental','lead_vocal','backing_vocal')],
             2:[pitch] if pitch else [],3:candidates,4:[mix] if mix else [],5:project['exports']}
    result=[]
    for index,(key,title,steps) in enumerate(STAGES):
        current=raw if index==0 else mix if index==5 and mix else source
        valid=bool(current and current.get('commit_state')=='committed' and Path(current['path']).is_file())
        stage={'id':index,'key':key,'title':title,'sequence':steps,'state':'not_started','input':{'artifact_id':current['id'] if current else None,'name':current.get('name') if current else '尚未选择输入','valid':valid,'range_ticks':project['selection'],'stale':graph_stale(project,current['id']) if current else False},'blockers':[],'outputs':[{'id':a['id'],'name':a.get('name',title+'结果'),'state':a.get('state','committed'),'selected':a.get('selected',False)} for a in outputs[index]],'next_hint':''}
        def action(identifier,label,operation=None,target=None,**extra):
            kind='execute' if operation or identifier in ('validate_model','refresh_pitch_preview') else 'navigate'
            stage['recommendation']={'id':identifier,'label':label,'operation':operation,'kind':kind,'target_stage':target,**extra}
        if index==0:
            if not setup_confirmed:stage['state']='blocked';action('setup','先确认任务与素材角色',target=0);stage['next_hint']='先选择制作任务和本次音频角色，再导入文件。'
            elif valid:stage['state']='ready';action('prepare','前往人声整理',target=1);stage['next_hint']='原文件保留；歌曲可先分离，干人声可直接分析。'
            else:action('import','选择音频',target=0)
        elif not source_valid and not (index==4 and enabled_tracks or index==5 and mix):
            stage['blockers'].append({'code':'missing_input','message':'当前没有有效音频输入。','repair':'导入音频或按哈希重新链接缺失素材。'});action('import','导入或重新链接音频',target=0);stage['state']='blocked'
        elif index==1:
            if project['task']=='speech' or source.get('stem_role') in ('guide','synthesized','vocal','lead_vocal','backing_vocal'):
                stage['state']='editable';action('pitch','直接检测当前人声音高','pitch',2);stage['next_hint']='选择直接分析会跳过分离节点，不会执行隐藏处理。'
            else:action('separate','分离人声与伴奏','separate',1);stage['next_hint']='分离完成后先独听，检查残留与起止。'
            if outputs[1]:stage['state']='ready'
        elif index==2:
            if analysis['state']=='stale':stage['state']='stale';action('pitch','按当前参数重新检测','pitch',2);stage['next_hint']=analysis['message']
            elif not pitch:action('pitch','检测当前人声音高','pitch',2);stage['next_hint']='检测结果是估计曲线；手工目标与锁定区域独立保存。'
            else:stage['state']='ready';action('go_convert','检查目标并选择模型版本',target=3);stage['next_hint']='修改目标后需要重新渲染试听，旧音频不会假装实时改变。'
        elif index==3:
            if project['task'] in ('cover','rewrite') and source.get('stem_role')=='original' and not(project['task']=='cover' and params.get('skip_separation')):
                stage['blockers'].append({'code':'needs_prepared_vocal','message':'当前是原曲混音，尚未确认干人声输入。','repair':'先在人声整理分离，或以干人声/导唱角色导入。'});action('prepare_vocal','准备人声输入',target=1)
            elif not params.get('model_id'):
                stage['blockers'].append({'code':'missing_model','message':'还没有选择目标声音与具体版本。','repair':'在声音选择器中选择实验和版本。'});action('choose_model','选择声音与版本',target=3)
            elif not model or model.get('status') in ('missing','quarantined','incompatible','hash_changed') or model.get('ok') is False:
                stage['blockers'].append({'code':'invalid_model','message':(model or {}).get('error','模型缺失或不兼容。'),'repair':'查看模型诊断，重新链接相同哈希版本或选择兼容版本。'});action('models','查看模型诊断',target=3)
            elif not model.get('model_hash') or model.get('status') in ('discovered','pending','unvalidated'):
                stage['blockers'].append({'code':'needs_validation','message':'该版本已发现，尚未完成受限兼容校验。','repair':'校验所选版本后试听，不会一次加载所有权重。'});action('validate_model','校验所选版本',target=3,model_id=params['model_id'])
            elif float(params.get('index_rate',0))>0 and (not model.get('index_path') or model.get('index_match',{}).get('status') not in ('verified',)):
                stage['blockers'].append({'code':'index_mismatch','message':'检索比例大于零，但没有已验证匹配的索引。','repair':'明确选择无检索（0），或绑定经验证的索引。'});action('fix_index','修复索引选择',target=3)
            elif analysis['state']=='stale':
                action('refresh_pitch_preview','按当前参数重新分析并试听',target=3);stage['state']='stale';stage['next_hint']=analysis['message']
            elif not preview_fresh:
                action('preview','更新当前参数试听' if candidates else '生成代表片段试听','convert',3);stage['state']='preview_stale' if candidates else 'editable';stage['next_hint']='旧参数候选仍可回听；新模型/参数需要新的试听。'
            elif params.get('accepted_preview_id')==preview_fresh['id'] and not selected_fresh:
                stage['state']='preview_accepted';action('render_full','按已采纳试听设置生成完整人声','convert',3);stage['next_hint']='完整生成不会把短试听当作全长候选。'
            elif not selected_fresh:
                stage['state']='preview_ready';action('audition','对齐试听并采用候选',target=3,candidate_id=preview_fresh['id']);stage['next_hint']='短片段候选只供试听；完整制作需生成覆盖源时长的版本。'
            else:stage['state']='ready';action('mix','前往混音',target=4);stage['next_hint']='采用的模型、索引和参数随候选保存，不随“最新版本”替换。'
            if stage['blockers']:stage['state']='blocked'
        elif index==4:
            stage['input'].update(name='当前混音：'+('、'.join(t.get('name',t['role']) for t in enabled_tracks[:3]) or '所有轨道已静音'),track_ids=[t['id'] for t in enabled_tracks],valid=bool(enabled_tracks))
            if not selected and not project.get('voice_parts'):
                stage['state']='needs_review';stage['next_hint']='尚未采用转换候选，可先返回试听；直接混音会使用现有原声。';action('audition','返回选择人声候选',target=3)
            elif mix and not graph_stale(project,mix['id']):stage['state']='ready';action('go_export','检查成品并导出',target=5)
            else:stage['state']='editable';action('mix','渲染当前轨道混音','mix',4);stage['next_hint']='只混入启用轨道；修改推子或采用候选后需重算混音。'
            if not enabled_tracks:stage['recommendation']['disabled']=True;stage['next_hint']='当前试听为静音；启用一条轨道后再生成完整混音。'
        elif index==5:
            if mix and graph_stale(project,mix['id']):
                stage['state']='blocked';stage['blockers'].append({'code':'stale_mix','message':'最后的混音对应旧设置。','repair':'重新渲染混音后导出当前版本。'});action('mix','更新混音','mix',4)
            elif mix or selected:
                stage['state']='ready';action('export','导出当前有效版本','export',5);stage['next_hint']='先检查峰值；同名文件会另存版本，配方不附模型权重。'
            else:stage['state']='needs_review';action('go_mix','准备要导出的音频',target=4);stage['next_hint']='没有模型推理血缘时，只能导出如实命名的原音处理版。'
        result.append(stage)
        if index==3:
            for item,candidate in zip(stage['outputs'],candidates):
                item['current']=current_candidate(candidate);item['freshness']='当前设置' if item['current'] else '旧参数预览';item['preview_only']=bool(candidate.get('preview_only'))
    prepared=source_valid and (source.get('stem_role') in ('vocal','guide','synthesized','lead_vocal','backing_vocal') or project['task']=='speech' or params.get('skip_separation'))
    gates=[(setup_confirmed and bool(raw) and Path(raw['path']).is_file(),'先确认任务/角色并导入有效音频。'),
           (prepared,'先准备干人声，或明确选择跳过分离。'),
           (analysis['state']=='ready','先按当前参数检测音高。'),
           (selected_fresh and not selected.get('preview_only',False) if selected else False,'先生成并采用覆盖输入的完整候选。'),
           (bool(mix and not graph_stale(project,mix['id'])),'先渲染当前设置的完整混音。')]
    for i,(allowed,reason) in enumerate(gates):
        result[i]['continuation']={'id':'continue','kind':'navigate','target_stage':i+1,'label':'继续 · '+STAGES[i+1][1],'disabled':not allowed,'reason':'' if allowed else reason}
    for stage in result:
        stage['completed']=stage['id']<5 and not stage.get('continuation',{}).get('disabled',True) or stage['id']==5 and bool(project['exports'])
    return {'project_id':project['id'],'revision':project['revision'],'stages':result,'parameter_sources':params.get('parameter_sources',{}),'voice_part':part if binding else None,'pitch_analysis':analysis}
