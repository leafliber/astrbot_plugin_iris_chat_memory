"""显式目标用户不能改变当前群和人格范围；读取不存在的画像不能创建空画像。"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from iris_memory.profile.models import UserProfile
from iris_memory.tools.get_profile import GetProfileTool


class TestProfileScopeRegression(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage = Mock(is_available=True)
        self.storage.get_user_profile = AsyncMock(
            return_value=UserProfile(user_id="target", user_name="测试人物", interests=["编程"])
        )
        self.manager = Mock()
        self.manager.get_component.return_value = self.storage
        self.adapter = Mock()
        self.adapter.get_user_id.return_value = "sender"
        self.adapter.get_group_id.return_value = "group_A"
        self.event = object()
        self.context = SimpleNamespace(context=SimpleNamespace(event=self.event))
        self.enabled = True
        config = Mock()
        config.get.side_effect = lambda key: self.enabled
        for item in [
            patch("iris_memory.tools.get_profile.get_component_manager", return_value=self.manager),
            patch("iris_memory.tools.get_profile.get_config", return_value=config),
            patch("iris_memory.platform.get_adapter", return_value=self.adapter),
            patch("iris_memory.core.persona.resolve_persona", new=AsyncMock(return_value="persona_A")),
        ]:
            item.start()
            self.addCleanup(item.stop)
        self.tool = GetProfileTool()

    async def test_explicit_user_keeps_group_and_persona(self):
        text = await self.tool.call(self.context, target_type="user", target_id="target")
        self.storage.get_user_profile.assert_awaited_once_with("target", "group_A", "persona_A")
        self.assertIn("测试人物", text)
        self.assertIn("编程", text)

    async def test_implicit_user_keeps_group(self):
        await self.tool.call(self.context)
        self.storage.get_user_profile.assert_awaited_once_with("sender", "group_A", "persona_A")

    async def test_group_isolation_disabled_uses_global_scope(self):
        self.enabled = False
        await self.tool.call(self.context, target_id="target")
        self.storage.get_user_profile.assert_awaited_once_with("target", "default", "persona_A")

    async def test_private_group_stays_private(self):
        self.adapter.get_group_id.return_value = None
        await self.tool.call(self.context, target_id="target")
        self.storage.get_user_profile.assert_awaited_once_with("target", "", "persona_A")

    async def test_missing_profile_is_explicit_and_read_only(self):
        self.storage.get_user_profile.return_value = None
        text = await self.tool.call(self.context, target_id="target")
        self.assertIn("尚未找到", text)
        self.assertNotIn("用户昵称", text)
        self.assertEqual([call[0] for call in self.storage.mock_calls], ["get_user_profile"])

    async def test_unavailable_storage_is_reported(self):
        self.storage.is_available = False
        text = await self.tool.call(self.context, target_id="target")
        self.assertIn("不可用", text)
        self.storage.get_user_profile.assert_not_awaited()

    async def test_missing_user_id_is_reported(self):
        self.adapter.get_user_id.return_value = None
        self.assertIn("无法获取用户ID", await self.tool.call(self.context))
        self.storage.get_user_profile.assert_not_awaited()
