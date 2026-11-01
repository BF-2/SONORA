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
    mode=p.get('separation_algorithm','single')
    if mode not in ('single','tta'):raise ValueError('不支持的分离推理算法。')
    return {'segment_size':number('segment_size',801 if harmony else 256,64,1024,True),
            'overlap_step_seconds':number('overlap_step_seconds',p.get('overlap',4),.25,16),
            'separation_algorithm':mode, 'primary_gain':number('primary_gain',1,.5,1.5),
            'cpu_threads':number('cpu_threads',4,1,8,True)}
