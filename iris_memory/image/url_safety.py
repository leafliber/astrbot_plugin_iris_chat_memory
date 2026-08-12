"""
URL 安全校验模块（防 SSRF）

所有「下载聊天消息中的网络资源」的代码路径必须经由本模块，
与 image/parser.py 的 LLM 解析路径采用一致的 SSRF 判据：

- 仅允许 http/https scheme；
- 主机的全部 DNS 解析地址必须为全局可达地址，拒绝任何私网、环回、
  链路本地、云元数据、保留、组播、未指定地址；
- 下载时通过 GlobalOnlyTransport 在每次连接前重新校验，防 DNS rebinding；
- 重定向的每一跳都会经过 transport 校验，因此允许 follow_redirects。
"""

import asyncio
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

from iris_memory.core import get_logger

logger = get_logger("url_safety")


def host_all_global(host: str) -> bool:
    """主机的全部解析地址是否均为全局可达地址。

    IP 字面量直接判定；域名解析所有地址，任一非全局（私网/环回/链路本地/
    云元数据/保留/组播/未指定）即返回 False。供 is_safe_url 与下载 transport
    共用，确保「校验」与「实际连接」采用一致的 SSRF 判据。
    """
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if not ip.is_global:
            return False
    return True


class GlobalOnlyTransport(httpx.AsyncBaseTransport):
    """包装另一个 transport，在每次请求前重新校验目标主机全部解析地址为全局。

    防 DNS rebinding：is_safe_url 的可达性校验与实际下载各自独立解析 DNS，
    攻击者可能在校验通过后、下载连接前把解析切到内网。本 transport 在连接前
    再次强制校验，任一解析结果非全局即拒绝，从根本上堵死向内网的 rebinding。
    重定向产生的每一跳请求同样经过本校验。
    """

    def __init__(self, wrapped: httpx.AsyncBaseTransport):
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host and not await asyncio.to_thread(host_all_global, host):
            raise httpx.ConnectError(f"目标主机解析含非全局地址，拒绝连接: {host}")
        return await self._wrapped.handle_async_request(request)

    async def aclose(self) -> None:
        await self._wrapped.aclose()


async def is_safe_url(url: str) -> bool:
    """校验 URL 是否安全（防 SSRF）。

    仅允许 http/https；对主机的全部 DNS 解析地址要求为全局可达地址，
    拒绝任何私网、环回、链路本地、云元数据、保留、组播、未指定等地址。
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    return await asyncio.to_thread(host_all_global, hostname)


async def safe_download(
    url: str,
    *,
    timeout: float = 10.0,
    max_bytes: int = 10 * 1024 * 1024,
    follow_redirects: bool = True,
) -> Optional[bytes]:
    """经过 SSRF 校验地下载 URL 内容并返回字节。

    校验 + 下载双保险：
    1. is_safe_url 前置校验 scheme 与主机解析地址；
    2. GlobalOnlyTransport 在每次实际连接前（含重定向每一跳）再次校验。

    Args:
        url: 目标 URL
        timeout: 超时秒数
        max_bytes: 最大允许字节数，超限返回 None
        follow_redirects: 是否跟随重定向（每一跳均受 transport 校验）

    Returns:
        下载到的字节内容；主机不安全、请求失败、空内容或超限时返回 None
    """
    if not await is_safe_url(url):
        logger.warning(f"URL 主机不安全（内网/保留地址），拒绝下载：{url[:80]}")
        return None
    try:
        transport = GlobalOnlyTransport(httpx.AsyncHTTPTransport(verify=True))
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects, transport=transport
        ) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                logger.debug(f"URL 返回 {resp.status_code}：{url[:80]}")
                return None
            content = resp.content
            if not content:
                return None
            if len(content) > max_bytes:
                logger.warning(f"内容过大（{len(content)} 字节），跳过：{url[:80]}")
                return None
            return content
    except Exception as e:
        logger.debug(f"URL 下载失败：{e}")
        return None
