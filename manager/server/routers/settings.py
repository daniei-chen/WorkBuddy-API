"""上游配置、模型映射与管理端用户。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, security
from ..iputil import client_ip
from ..services import reload, updater, wb2api

router = APIRouter(prefix='/api', tags=['settings'])


# ── 上游配置 ─────────────────────────────────────────────
@router.get('/settings/upstream')
def get_upstream(user: dict = Depends(security.current_user)) -> dict:
    return wb2api.load_upstream_config()


@router.post('/settings/upstream')
async def save_upstream(body: dict, request: Request,
                        user: dict = Depends(security.require_session_admin)) -> dict:
    # 必须是 async：同步路由会被 FastAPI 放进线程池执行，那里没有事件循环，
    # 无法调度后台重载任务（request_restart 将拿不到 running loop）。
    try:
        result = wb2api.save_upstream_config(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改上游配置会影响全部账号行为，且可能写入 device_token/base 等敏感项，必须留痕
    security.audit(user, 'update_upstream', '',
                   f"段={','.join(sorted(k for k in body if isinstance(body.get(k), dict)))}"
                   f'；来源 {client_ip(request)}')
    # 上游只在启动时读 config.json，保存后自动重载使其生效
    result['reload_scheduled'] = reload.request_restart()
    # 无法自动重载上游时（没装 docker / 容器没挂 docker.sock），要如实告诉
    # 用户去宿主机重启，而不是让他以为改完就生效了。
    #
    # 判据是**实际能力**而非"是否容器"：容器挂了 docker.sock 就能自动重载
    # （与宿主部署等价），宿主没装 docker 反而不能。
    if not updater.can_control_docker():
        result['reload_scheduled'] = False
        result['reload_hint'] = (
            '配置已写入，但当前环境无法操作 docker，不会自动重启上游容器。'
            '请在宿主机执行 docker compose restart wb2api（上游目录下）使其生效。'
        )
    return result


@router.get('/upstream/reload-state')
def reload_state(user: dict = Depends(security.current_user)) -> dict:
    """上游重载状态，供前端展示「正在应用配置」。"""
    return reload.state()


class UpstashTestIn(BaseModel):
    url: str = ''
    # 留空则使用配置文件中已保存的 token
    token: str | None = None


@router.post('/settings/upstash/test')
async def test_upstash(body: UpstashTestIn, user: dict = Depends(security.require_admin)) -> dict:
    """探测 Upstash 是否可用（走其 REST 接口 PING）。"""
    ok, message = await wb2api.test_upstash(body.url, body.token)
    return {'ok': ok, 'message': message}


@router.post('/settings/upstash/reload')
async def reload_upstream(user: dict = Depends(security.require_session_admin)) -> dict:
    """立即重启上游容器（等待结果）。一般无需手动调用——保存配置会自动重载。

    与会话绑定：它和 `/api/restart` 是**同一个动作**（都走 `reload.restart_now()`），
    后者在引入 API Token 时被划为「仅会话」。同一个动作不能因为走的是哪个路由就
    权限不同——否则 token 泄露者能从这里把上游重启掉，那条边界就形同虚设。
    """
    ok, message = await reload.restart_now()
    return {'ok': ok, 'message': message}


# ── 模型别名映射 ─────────────────────────────────────────
@router.get('/settings/model-map')
def get_model_map(user: dict = Depends(security.current_user)) -> dict:
    return db.get_setting('model_map', {}) or {}


@router.post('/settings/model-map')
def save_model_map(body: dict[str, str], user: dict = Depends(security.require_admin)) -> dict:
    clean = {str(k): str(v) for k, v in body.items() if str(k).strip() and str(v).strip()}
    db.set_setting('model_map', clean)
    return clean


# ── 管理端用户 ───────────────────────────────────────────
class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1)
    role: str = Field(pattern='^(admin|viewer)$')


class UserPatch(BaseModel):
    password: str | None = None
    role: str | None = Field(default=None, pattern='^(admin|viewer)$')


def _public(cfg: dict) -> list[dict]:
    return [{'username': u['username'], 'role': u.get('role', 'viewer')} for u in cfg.get('users', [])]


@router.get('/users')
def list_users(user: dict = Depends(security.current_user)) -> list[dict]:
    return _public(security.load_users())


@router.post('/users')
def add_user(body: UserIn, request: Request,
             user: dict = Depends(security.require_session_admin)) -> dict:
    """新建管理用户。

    审计是必须的：上次入侵里攻击者正是「先建自己的账号、再正常登录」，
    而这一步当时没有任何记录。新建用户写随机 sv，避免同名重建后旧 cookie 复活。
    """
    pw = str(body.password)
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail='密码至少 8 位')
    cfg = security.load_users()
    if any(u.get('username') == body.username for u in cfg.get('users', [])):
        raise HTTPException(status_code=409, detail='用户名已存在')
    cfg.setdefault('users', []).append({
        'username': body.username,
        'role': body.role,
        'pwd_hash': security.make_hash(pw),
        # 随机初始 sv：删除同名用户再重建时，不会让旧会话「复活」
        'sv': secrets.randbelow(1 << 30),
    })
    security.save_users(cfg)
    security.audit(user, 'add_user', body.username, f'角色={body.role}；来源 {client_ip(request)}')
    return {'username': body.username, 'role': body.role}


@router.patch('/users/{username}')
def update_user(username: str, body: UserPatch, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    """改密码 / 改角色。

    两种改动都必须**吊销该用户既有会话**：
      - 改密码：否则被盗会话在改密码后仍能用到过期（7 天）——本次事故里攻击者
        改掉 admin 密码后，真正管理员手里的旧会话不该还能管理后台
      - 改角色：权限必须立即生效，不能等 cookie 过期（载荷里的 role 只是快照）

    吊销按用户进行（递增其 sv），不影响其他已登录用户。
    """
    cfg = security.load_users()
    target = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')

    changed_pwd = changed_role = False
    if body.password:
        pw = str(body.password)
        # 现在不再接受空/极短口令：管理端是公网入口，弱口令等于没有防护
        if len(pw) < 8:
            raise HTTPException(status_code=400, detail='密码至少 8 位')
        target['pwd_hash'] = security.make_hash(pw)
        changed_pwd = True
    if body.role and body.role != target.get('role'):
        admins = [u for u in cfg.get('users', []) if u.get('role') == 'admin']
        if target.get('role') == 'admin' and len(admins) <= 1:
            raise HTTPException(status_code=400, detail='至少保留一个最高管理员')
        target['role'] = body.role
        changed_role = True

    if changed_pwd or changed_role:
        try:
            target['sv'] = int(target.get('sv') or 0) + 1
        except (TypeError, ValueError):
            target['sv'] = 1

    security.save_users(cfg)
    if changed_pwd or changed_role:
        parts = []
        if changed_pwd:
            parts.append('改密码')
        if changed_role:
            parts.append('改角色')
        security.audit(user, 'update_user', username, '、'.join(parts) + f'；来源 {client_ip(request)}')
    # 把自己的密码改了，当前会话也会失效——前端据此提示重新登录
    self_revoked = bool(changed_pwd and username == user.get('username'))
    return {
        'username': target['username'],
        'role': target.get('role', 'viewer'),
        'sessions_revoked': changed_pwd or changed_role,
        'relogin_required': self_revoked,
    }


@router.delete('/users/{username}')
def delete_user(username: str, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    cfg = security.load_users()
    users = cfg.get('users', [])
    target = next((u for u in users if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')
    admins = [u for u in users if u.get('role') == 'admin']
    if target.get('role') == 'admin' and len(admins) <= 1:
        raise HTTPException(status_code=400, detail='至少保留一个最高管理员')
    if target.get('username') == user.get('username'):
        raise HTTPException(status_code=400, detail='不能删除当前登录用户')
    cfg['users'] = [u for u in users if u.get('username') != username]
    security.save_users(cfg)
    security.audit(user, 'delete_user', username, f'来源 {client_ip(request)}')
    return {'ok': True}


@router.get('/audit-logs')
def audit_logs(
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(security.require_admin),
) -> dict:
    """管理端审计日志（登录、改密码、增删用户、改安全配置等）。

    仅管理员可读：里面含用户名与来源 IP，属于敏感信息。
    """
    return {
        'items': db.list_audit_logs(limit=limit, offset=offset),
        'total': db.count_audit_logs(),
    }
