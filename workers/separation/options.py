"""Parameters for the pinned 0.44.2 RoFormer adapter (overlap is seconds)."""
import math

def separation_options(parameters):
    harmony=parameters.get('separation_task')=='lead_backing'
    settings=parameters.get('harmony_settings' if harmony else 'separation_settings',{})
    if not isinstance(settings,dict):raise ValueError('分离设置必须为对象。')
    p={**parameters,**settings}
    def number(key,default,lo,hi,integer=False):
        value=p.get(key,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi or integer and int(value)!=value:
            raise ValueError(f'无效分离参数 {key}：需要 {lo}–{hi}'+(' 的整数' if integer else ''))
        return int(value) if integer else float(value)
    mode=p.get('separation_algorithm','ensemble' if harmony else 'single')
    if mode not in ('single','tta','ensemble'):raise ValueError('不支持的分离推理算法。')
    if mode=='ensemble' and not harmony:raise ValueError('双模型增强仅用于二级主唱/和声分离。')
    return {'segment_size':number('segment_size',801 if harmony else 256,64,1024,True),
            'overlap_step_seconds':number('overlap_step_seconds',p.get('overlap',4),.25,16),
            'separation_algorithm':mode, 'primary_gain':number('primary_gain',1,.5,1.5),
            'cpu_threads':number('cpu_threads',4,1,8,True),
            'secondary_weight':number('secondary_weight',.5,0,1),
            'spatial_protection':number('spatial_protection',.8,0,1),
            'backing_suppression':number('backing_suppression',0,0,1)}
