"""IP 解析与 CIDR 匹配工具。"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from . import config


def _clean_ip(value: str) -> str:
    """去掉端口与方括号，兼容 IPv6 形如 [::1]:1234。"""
    raw = (value or '').strip()
    if not raw:
        return ''
    if raw.startswith('['):  # [::1]:1234
        end = raw.find(']')
        return raw[1:end] if end > 0 else raw
    # 纯 IPv6 不处理；IPv4:port 去掉端口
    if raw.count(':') == 1:
        host, _, port = raw.partition(':')
        if port.isdigit():
            return host
    return raw


def _is_trusted_proxy(peer: str) -> bool:
    """TCP 对端是否落在可信代理网段内。

    仅当对端确实是我们配置的反代（同机回环或指定私网）时，才采信它写入的
    转发头。这样「服务直接暴露」时伪造的 X-Real-IP 不会被采信。
    """
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in config.TRUSTED_PROXY_CIDRS:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """解析真实客户端 IP。

    安全要点（曾有漏洞，且被实测绕过）：`X-Forwarded-For` 与 `X-Real-IP` 都是
    **客户端可伪造**的普通请求头。反向代理以 `$remote_addr` 覆盖写入 X-Real-IP
    只是"通常如此"，前提是**请求确实经过那个反代**。服务直接暴露时
    （systemd 默认监听 0.0.0.0），攻击者加一行 `X-Real-IP: 9.9.9.9`
    就能冒充任意来源 IP，从而绕过全局 IP 白/黑名单、密钥 IP 白名单与 max_ips、
    以及登录失败按 IP 锁定 —— 全部 IP 类管控形同虚设。

    取值优先级（按可信度）：
      1) 仅当 **TCP 对端来自可信代理网段** 时，才采信 `X-Real-IP`
      2) 同上条件下，从 `X-Forwarded-For` **右往左**数第 N 个（N = 可信跳数）；
         右侧是反代追加的真实地址，左侧才是可伪造部分
      3) TCP 对端地址（可信度最高，永远可回退）

    若前面还挂了 CDN，请把 `WB_TRUSTED_PROXY_HOPS` 调成 CDN + 反代的层数，
    并把 `WB_TRUSTED_PROXY_CIDRS` 加上 CDN 的回源网段。
    """
    peer = request.client.host if request.client else ''

    if not config.TRUST_PROXY:
        return peer or '0.0.0.0'
    # 关键：对端不可信时，转发头一律不看（这正是之前被绕过的地方）
    if not _is_trusted_proxy(peer):
        return peer or '0.0.0.0'

    real = _clean_ip(request.headers.get('x-real-ip', ''))
    if real:
        return real

    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        parts = [p for p in (_clean_ip(p) for p in xff.split(',')) if p]
        if parts:
            hops = max(1, config.TRUSTED_PROXY_HOPS)
            idx = len(parts) - hops
            return parts[idx] if idx >= 0 else parts[0]

    return peer or '0.0.0.0'


def ip_matches(ip: str, cidr: str) -> bool:
    """支持单 IP 与 CIDR；非法输入一律不匹配。

    `cidr` 两侧空白会被去掉：写入时已归一化（见 keysvc._norm_cidrs），但
    **存量库里可能已有带空白的条目**（旧版本写入的），而
    `ip_network(' 10.0.0.0/8')` 会解析失败 → 这条规则永远匹配不上 →
    白名单里只要有一条这样的记录，那把密钥就对**所有**来源都拒绝。
    这里兜一层，让老数据也能正确生效。
    """
    try:
        addr = ipaddress.ip_address(ip)
        try:
            net = ipaddress.ip_network(str(cidr or '').strip(), strict=False)
        except ValueError:
            return False
        return addr in net
    except ValueError:
        return False


def evaluate(ip: str, rules: list[dict], mode: str) -> bool:
    """返回 True 表示放行。mode: whitelist（默认拒绝）| blacklist（默认放行）。"""
    if not rules:
        return mode != 'whitelist'
    allow = [r for r in rules if r['kind'] == 'allow']
    deny = [r for r in rules if r['kind'] == 'deny']
    if any(ip_matches(ip, r['cidr']) for r in deny):
        return False
    if mode == 'whitelist':
        return any(ip_matches(ip, r['cidr']) for r in allow)
    return True
