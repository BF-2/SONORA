import json
from jsonschema import Draft202012Validator
from .config import ROOT
SCHEMA=json.loads((ROOT/'packages/contracts/project.schema.json').read_text(encoding='utf-8'))
VALIDATOR=Draft202012Validator(SCHEMA)
def validate_project(value):
    error=next(VALIDATOR.iter_errors(value),None)
    if error:raise ValueError('工程 schema 校验失败：'+'.'.join(map(str,error.absolute_path))+' '+error.message)
