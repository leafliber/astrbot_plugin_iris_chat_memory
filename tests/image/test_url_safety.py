"""测试 URL 安全校验模块（防 SSRF）"""

import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from iris_memory.image.url_safety import (
    GlobalOnlyTransport,
    host_all_global,
    is_safe_url,
    safe_download,
)


class TestHostAllGlobal:
    """host_all_global：IP 字面量与域名解析的全局性判定"""

    def test_public_ip_literal(self):
        assert host_all_global("8.8.8.8") is True

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # 环回
            "10.0.0.1",  # 私网 A
            "172.16.0.1",  # 私网 B
            "192.168.1.1",  # 私网 C
            "169.254.169.254",  # 云元数据（链路本地）
            "0.0.0.0",  # 未指定
            "::1",  # IPv6 环回
            "fe80::1",  # IPv6 链路本地
            "fd00::1",  # IPv6 ULA
        ],
    )
    def test_non_global_ip_literals_rejected(self, ip):
        assert host_all_global(ip) is False

    def test_domain_with_private_resolution_rejected(self):
        """域名解析结果含任一非全局地址即拒绝"""
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_infos):
            assert host_all_global("evil.example.com") is False

    def test_domain_all_global_accepted(self):
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]
        with patch("socket.getaddrinfo", return_value=fake_infos):
            assert host_all_global("ok.example.com") is True

    def test_unresolvable_domain_rejected(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror):
            assert host_all_global("nonexistent.invalid") is False


class TestIsSafeUrl:
    """is_safe_url：scheme 与主机校验"""

    @pytest.mark.asyncio
    async def test_public_https_accepted(self):
        assert await is_safe_url("https://8.8.8.8/img.jpg") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost/internal",
            "http://169.254.169.254/latest/meta-data",  # AWS 元数据
            "http://192.168.1.1/router",
            "http://[::1]/x",
            "ftp://8.8.8.8/x",  # 非 http/https
            "file:///etc/passwd",
            "http://",  # 无主机
            "not-a-url",
        ],
    )
    async def test_unsafe_urls_rejected(self, url):
        assert await is_safe_url(url) is False


class TestGlobalOnlyTransport:
    """GlobalOnlyTransport：连接前强制校验（防 DNS rebinding）"""

    @pytest.mark.asyncio
    async def test_private_host_raises_connect_error(self):
        wrapped = AsyncMock(spec=httpx.AsyncBaseTransport)
        transport = GlobalOnlyTransport(wrapped)
        request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data")

        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(request)
        wrapped.handle_async_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_global_host_passes_through(self):
        response = httpx.Response(200, content=b"ok")
        wrapped = AsyncMock(spec=httpx.AsyncBaseTransport)
        wrapped.handle_async_request = AsyncMock(return_value=response)
        transport = GlobalOnlyTransport(wrapped)
        request = httpx.Request("GET", "https://8.8.8.8/img.jpg")

        result = await transport.handle_async_request(request)
        assert result is response


class TestSafeDownload:
    """safe_download：校验 + 下载双保险"""

    @pytest.mark.asyncio
    async def test_unsafe_url_no_network_call(self):
        """不安全 URL 直接拒绝，不发起任何网络请求"""
        with patch("httpx.AsyncClient") as MockClient:
            result = await safe_download("http://169.254.169.254/latest/meta-data")
        assert result is None
        MockClient.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_success(self):
        mock_resp = httpx.Response(200, content=b"\xff\xd8jpeg-bytes")
        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await safe_download("https://8.8.8.8/img.jpg")
        assert result == b"\xff\xd8jpeg-bytes"

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        mock_resp = httpx.Response(404, content=b"not found")
        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await safe_download("https://8.8.8.8/img.jpg")
        assert result is None

    @pytest.mark.asyncio
    async def test_oversized_content_returns_none(self):
        mock_resp = httpx.Response(200, content=b"x" * 1024)
        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await safe_download("https://8.8.8.8/big.bin", max_bytes=100)
        assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await safe_download("https://8.8.8.8/img.jpg")
        assert result is None
