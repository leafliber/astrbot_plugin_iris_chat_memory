"""直接测试上下文日志函数及请求钩子中的日志异常隔离。"""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from iris_memory.core import llm_request_hook as hook


class TestContextLoggingRegression(unittest.TestCase):
    def run_log(self, content, enabled=True):
        request = SimpleNamespace(
            system_prompt="system", extra_user_content_parts=[],
            contexts=[{"role": "assistant", "content": content}], functions=None,
        )
        before = deepcopy(request.contexts)
        with patch("iris_memory.config.get_config", return_value={"enable_context_logging": enabled}), patch.object(hook.logger, "debug") as debug:
            hook._log_final_context(request)
        self.assertEqual(request.contexts, before)
        return debug

    def test_tool_call_with_null_content(self):
        debug = self.run_log(None)
        self.assertIn("无正文", debug.call_args.args[0])

    def test_long_multimodal_list_is_safe(self):
        debug = self.run_log([{"type": "text", "text": "内容" * 200}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}}])
        self.assertIn("...", debug.call_args.args[0])
        self.assertNotIn("base64,private", debug.call_args.args[0])

    def test_multimodal_image_is_placeholder(self):
        debug = self.run_log([{"type": "image_url", "image_url": {"url": "secret"}}])
        self.assertIn("[image_url]", debug.call_args.args[0])
        self.assertNotIn("secret", debug.call_args.args[0])

    def test_scalar_content(self):
        self.assertIn("42", self.run_log(42).call_args.args[0])

    def test_disabled_logging_does_nothing(self):
        self.run_log(None, enabled=False).assert_not_called()


class TestLoggingFailureIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_logging_failure_preserves_injected_context(self):
        request = SimpleNamespace(system_prompt="keep", prompt="question", contexts=[], extra_user_content_parts=[])
        event = SimpleNamespace(message_str="question")
        with ExitStack() as stack:
            for name, result in [
                ("_parse_images_if_related_mode", None),
                ("_collect_l1_context", "对话内容"),
                ("_collect_user_profile", "画像内容"),
                ("_collect_l2_memory", ("记忆内容", [])),
                ("_collect_l3_knowledge_graph", "图谱内容"),
            ]:
                stack.enter_context(patch.object(hook, name, new=AsyncMock(return_value=result)))
            stack.enter_context(patch.object(hook, "_log_final_context", side_effect=RuntimeError("logging failed")))
            warning = stack.enter_context(patch.object(hook.logger, "warning"))
            await hook.preprocess_llm_request(event, request, object())
        self.assertTrue(request.extra_user_content_parts)
        text = request.extra_user_content_parts[0].text
        for content in ["对话内容", "画像内容", "记忆内容", "图谱内容"]:
            self.assertIn(content, text)
        self.assertEqual(request.system_prompt, "keep")
        self.assertEqual(request.contexts, [])
        warning.assert_called_once()
