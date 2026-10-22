"""系统维护：一键更新与进度查询。

安全说明：仅管理员可用；更新目标限定为固定枚举，路径全部取自服务端环境，
不接受客户端传入的命令或路径，避免变成任意命令执行入口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import security
from ..services import changelog, updater, wb2api

router = APIRouter(prefix='/api/system', tags=['system'])


class UpdateIn(BaseModel):
    target: str = Field(pattern='^(manager|upstream|both)$')


class UpstreamRefIn(BaseModel):
    """固定上游版本。ref 为空串表示取消固定、恢复跟随分支。

    这里只做长度限制，格式校验交给 updater.set_upstream_ref——
    该值最终会作为 git 参数，必须严格校验（不能接受任意字符串）。
    """
    ref: str = Field(max_length=80)


@router.get('/update-status')
def update_status(user: dict = Depends(security.current_user)) -> dict:
    """更新进度与当前版本（含运行中的日志）。"""
    status = updater.read_status()
    status['log_tail'] = updater.tail_log(80)
    return status


@router.post('/update')
def start_update(body: UpdateIn, user: dict = Depends(security.require_session_admin)) -> dict:
    """启动一键更新。

    更新会在后台执行并可能重启本服务，因此立即返回，由前端轮询
    `/api/system/update-status` 查看进度。
    """
    ok, message = updater.start_update(body.target)
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@router.post('/upstream-ref')
def set_upstream_ref(body: UpstreamRefIn, user: dict = Depends(security.require_session_admin)) -> dict:
    """固定上游版本（或取消固定），用于上游某提交自身有问题时回退。"""
    try:
        ref = updater.set_upstream_ref(body.ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'ok': True, 'upstream_ref': ref}


@router.get('/check-update')
def check_update(force: bool = False, user: dict = Depends(security.current_user)) -> dict:
    """检测是否有新版本（管理端与上游）。

    结果缓存 6 小时以免触发 GitHub 限流；`force=true` 可强制刷新，
    但会打 GitHub API（未认证配额有限），因此只允许管理员强制刷新。
    """
    if force and user.get('role') != 'admin':
        force = False
    return updater.check_updates(force=force)


@router.get('/versions')
async def versions(user: dict = Depends(security.current_user)) -> dict:
    """展示当前版本，便于确认更新是否生效。"""
    try:
        status = await wb2api.get_status()
        upstream_ok = bool(status.get('connected'))
        upstream_total = status.get('total')
    except Exception:  # noqa: BLE001
        upstream_ok, upstream_total = False, None

    return {
        'manager': updater.current_version(),
        'upstream_connected': upstream_ok,
        'upstream_accounts': upstream_total,
        'upstream_dir': str(updater._upstream_dir()),
    }


@router.get('/changelog')
def get_changelog(user: dict = Depends(security.current_user)) -> dict:
    """更新日志（解析仓库根目录的 CHANGELOG.md）。

    离线环境同样可用：文件随发布包一起分发，无需访问 GitHub。
    """
    data = changelog.load_changelog()
    data['current'] = updater.current_version()
    return data
