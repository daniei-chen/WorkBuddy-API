"""模型中心：展示账号实际可用的模型及其细节。

与「上游配置 → 可用模型」的区别：那里只是上游 /v1/models 的简表；这里直连
腾讯模型接口，能给出**显示名、真实上下文、最大输出、推理档位**，并支持搜索、
按能力筛选与按系列分组。

**按版本（realm）区分**：国内版与国际版的模型几乎不重叠，两边的清单分开取、
分开缓存；不传 realm 默认国内版（与上游「裸名默认 CN」口径一致）。

只读接口。来源与缓存状态如实返回，前端据此标注（不把回退数据说成实时数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import security
from ..services import modelcatalog

router = APIRouter(prefix='/api', tags=['models'])


@router.get('/model-catalog')
async def model_catalog(
    realm: str = 'cn',
    force: bool = False,
    user: dict = Depends(security.current_user),
) -> dict:
    """指定版本的模型清单 + 统计。

    force=true 会**直连腾讯**重新拉取（跳过 5 分钟缓存）。它消耗账号池的调用
    额度、也有风控风险，因此只允许管理员触发；普通登录用户仍可用缓存版本。
    """
    if force and user.get('role') != 'admin':
        force = False
    r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    data = await modelcatalog.catalog(r, force=force)
    models = data.get('models') or []
    return {
        **data,
        'realm': r,
        'summary': modelcatalog.summarize(models),
    }
