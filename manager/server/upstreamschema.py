"""Strict, versioned writable schema derived from the preserved gateway source."""
import json
import math
from pathlib import Path
SCHEMA = json.loads(Path(__file__).with_name('upstream-schema.json').read_text())

def validate(patch):
    if not isinstance(patch, dict):
        raise ValueError('上游配置必须是对象')
    sections = SCHEMA['sections']
    for section, values in patch.items():
        if section not in sections:
            raise ValueError(f'未知或不可修改的配置段：{section}')
        if not isinstance(values, dict):
            raise ValueError(f'{section} 必须是对象')
        for key, value in values.items():
            typ = sections[section].get(key)
            if typ is None:
                raise ValueError(f'未知配置字段：{section}.{key}')
            if section == 'upstream' and key == 'device_token' and value is None:
                continue
            valid = ((typ in ('bool','*bool') and isinstance(value,bool)) or
                     (typ == 'int' and isinstance(value,int) and not isinstance(value,bool)) or
                     (typ == 'float64' and isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)) or
                     (typ == 'string' and isinstance(value,str)) or
                     (typ == '[]int' and isinstance(value,list) and all(isinstance(v,int) and not isinstance(v,bool) for v in value)))
            if not valid:
                raise ValueError(f'{section}.{key} 类型错误（要求 {typ}）')
            if isinstance(value,str) and (len(value)>4096 or any(ord(c)<32 for c in value)):
                raise ValueError(f'{section}.{key} 包含控制字符或过长')
