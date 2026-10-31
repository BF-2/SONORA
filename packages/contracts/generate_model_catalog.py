"""Generate catalog TS/Python types from the adjacent JSON Schema (stdlib only)."""
import hashlib
import json
from pathlib import Path
root=Path(__file__).resolve().parent
source=root/'model-catalog.schema.json'
schema=json.loads(source.read_text(encoding='utf-8'))
digest=hashlib.sha256(source.read_bytes()).hexdigest()


def field_type(spec,language):
    if '$ref' in spec:return spec['$ref'].rsplit('/',1)[-1]
    if 'enum' in spec:
        values=[json.dumps(v,ensure_ascii=False) for v in spec['enum']]
        return ' | '.join(values) if language=='ts' else 'Literal['+', '.join(values)+']'
    typ=spec.get('type','object')
    if isinstance(typ,list):return ' | '.join(field_type({**spec,'type':v},language) for v in typ)
    if typ=='array':
        inner=field_type(spec.get('items',{}),language)
        return f'Array<{inner}>' if language=='ts' else f'list[{inner}]'
    return ({'string':'string','number':'number','integer':'number','boolean':'boolean','null':'null','object':'Record<string, unknown>'} if language=='ts' else {'string':'str','number':'float','integer':'int','boolean':'bool','null':'None','object':'dict[str, Any]'}).get(typ,'unknown' if language=='ts' else 'Any')


ts=[f'// Generated from model-catalog.schema.json; SHA-256 {digest}.']
py=[f'# Generated from model-catalog.schema.json; SHA-256 {digest}.','from __future__ import annotations','from typing import Any, Literal, Required, TypedDict','']
for name,spec in schema['$defs'].items():
    required=set(spec.get('required',[]));ts.append('export interface '+name+' {');py.append('class '+name+'(TypedDict, total=False):')
    for key,value in spec.get('properties',{}).items():
        ts.append(f'  {key}{"" if key in required else "?"}: {field_type(value,"ts")};')
        ptype=field_type(value,'py');py.append(f'    {key}: '+(f'Required[{ptype}]' if key in required else ptype))
    ts.append('}\n');py.append('')
(root/'model-catalog.types.ts').write_text('\n'.join(ts).rstrip()+'\n',encoding='utf-8')
(root/'model_catalog_types.py').write_text('\n'.join(py).rstrip()+'\n',encoding='utf-8')
