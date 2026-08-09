"""
ConsolidationPhase 合并重复项测试

测试核心功能：
- 并查集构建
- 话题级归拢
- LLM 合并调用
- 批量处理
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch

from iris_memory.dream.consolidation import ConsolidationPhase


def _mock_config():
    mock = Mock()
    mock.get = Mock(
        side_effect=lambda key, default=None: {
            "dream_consolidation_similarity_threshold": 0.85,
            "dream_consolidation_batch_size": 10,
            "dream_consolidation_scan_budget": 500,
            "dream_consolidation_query_batch_size": 50,
            "dream_consolidation_max_group_size": 5,
        }.get(key, default)
    )
    return mock


class TestConsolidationPhase:
    @pytest.fixture
    def phase(self):
        return ConsolidationPhase()

    @pytest.mark.asyncio
    async def test_execute_l2_unavailable(self, phase):
        l2 = Mock()
        l2.is_available = False
        l3 = None
        llm = None

        with patch(
            "iris_memory.dream.consolidation.get_config", return_value=_mock_config()
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["merged_groups"] == 0
        assert result["deleted_entries"] == 0

    @pytest.mark.asyncio
    async def test_execute_no_entries(self, phase):
        l2 = Mock()
        l2.is_available = True
        l2.get_all_entries = AsyncMock(return_value=[])
        l3 = None
        llm = Mock()

        with patch(
            "iris_memory.dream.consolidation.get_config", return_value=_mock_config()
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["merged_groups"] == 0
        assert result["deleted_entries"] == 0

    @pytest.mark.asyncio
    async def test_execute_no_llm(self, phase):
        l2 = Mock()
        l2.is_available = True
        l3 = None
        llm = None

        with patch(
            "iris_memory.dream.consolidation.get_config", return_value=_mock_config()
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["merged_groups"] == 0
        assert result["deleted_entries"] == 0

    @pytest.mark.asyncio
    async def test_merge_memories_success(self, phase):
        llm = Mock()
        llm.generate_direct = AsyncMock(return_value="合并后的记忆内容")

        result = await phase._merge_memories("记忆1", "记忆2", llm)

        assert result == "合并后的记忆内容"
        llm.generate_direct.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_memories_llm_failure(self, phase):
        llm = Mock()
        llm.generate_direct = AsyncMock(return_value=None)

        result = await phase._merge_memories("记忆1", "记忆2", llm)

        assert result is None

    @pytest.mark.asyncio
    async def test_merge_memories_llm_exception(self, phase):
        llm = Mock()
        llm.generate_direct = AsyncMock(side_effect=Exception("LLM error"))

        result = await phase._merge_memories("记忆1", "记忆2", llm)

        assert result is None

    @staticmethod
    def _make_entry(entry_id, content, metadata):
        entry = Mock()
        entry.id = entry_id
        entry.content = content
        entry.metadata = metadata
        return entry

    @staticmethod
    def _make_l2(add_result="merged_id"):
        l2 = Mock()
        l2.add_memory = AsyncMock(return_value=add_result)
        l2.delete_entries = AsyncMock(return_value=True)
        return l2

    @pytest.mark.asyncio
    async def test_merge_group_writes_before_deleting(self, phase):
        entries = [
            self._make_entry("e1", "张三喜欢孙权", {"user_id": "u1"}),
            self._make_entry("e2", "张三偏爱孙权", {"user_id": "u1"}),
        ]
        l2 = self._make_l2(add_result=None)
        llm = Mock()
        llm.generate_direct = AsyncMock(return_value="张三喜欢孙权")

        merged, deleted = await phase._merge_group(entries, l2, llm)

        assert (merged, deleted) == (0, 0)
        l2.delete_entries.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_group_preserves_single_user_id(self, phase):
        entries = [
            self._make_entry(
                "e1",
                "张三喜欢孙权",
                {"user_id": "u1", "active_users": "u1,u2", "confidence": 0.8},
            ),
            self._make_entry(
                "e2",
                "张三偏爱孙权",
                {"user_id": "u1", "active_users": "u2", "confidence": 0.7},
            ),
        ]
        l2 = self._make_l2()
        llm = Mock()
        llm.generate_direct = AsyncMock(return_value="张三喜欢孙权")

        merged, deleted = await phase._merge_group(entries, l2, llm)

        assert (merged, deleted) == (1, 2)
        metadata = l2.add_memory.call_args.kwargs["metadata"]
        assert metadata["user_id"] == "u1"
        assert metadata["active_users"] == "u1,u2"
        assert "subjectless" not in metadata
        l2.delete_entries.assert_awaited_once_with(["e1", "e2"])

    @pytest.mark.asyncio
    async def test_merge_group_marks_conflicting_users_subjectless(self, phase):
        entries = [
            self._make_entry("e1", "张三喜欢孙权", {"user_id": "u1"}),
            self._make_entry("e2", "李四喜欢孙权", {"user_id": "u2"}),
        ]
        l2 = self._make_l2()
        llm = Mock()
        llm.generate_direct = AsyncMock(return_value="张三和李四都喜欢孙权")

        await phase._merge_group(entries, l2, llm)

        metadata = l2.add_memory.call_args.kwargs["metadata"]
        assert "user_id" not in metadata
        assert metadata["subjectless"] is True
