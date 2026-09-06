"""
KnowledgeExtractPhase 知识提取测试

测试核心功能：
- 未处理记忆筛选
- 记忆分组
- 实体关系提取
- L3 写入
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch

from iris_memory.dream.knowledge_extract import KnowledgeExtractPhase


def _mock_config():
    mock = Mock()
    mock.get = Mock(
        side_effect=lambda key, default=None: {
            "dream_knowledge_extract_min_unprocessed": 10,
            "dream_knowledge_extract_batch_size": 20,
            "isolation_config.enable_group_memory_isolation": False,
        }.get(key, default)
    )
    return mock


class TestKnowledgeExtractPhase:
    @pytest.fixture
    def phase(self):
        return KnowledgeExtractPhase()

    @pytest.mark.asyncio
    async def test_execute_l3_unavailable(self, phase):
        l2 = Mock()
        l2.is_available = True
        l3 = Mock()
        l3.is_available = False
        llm = Mock()

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["memories_processed"] == 0
        assert result["nodes_extracted"] == 0

    @pytest.mark.asyncio
    async def test_execute_no_llm(self, phase):
        l2 = Mock()
        l2.is_available = True
        l3 = Mock()
        l3.is_available = True
        llm = None

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["memories_processed"] == 0
        assert result["nodes_extracted"] == 0

    @pytest.mark.asyncio
    async def test_execute_no_unprocessed(self, phase):
        l2 = Mock()
        l2.is_available = True
        l2.get_unprocessed_count = AsyncMock(return_value=3)
        l3 = Mock()
        l3.is_available = True
        llm = Mock()

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            result = await phase.execute(l2, l3, llm)

        assert result["memories_processed"] == 0

    @pytest.mark.asyncio
    async def test_all_writes_fail_does_not_mark_processed(self, phase):
        """回归：提取结果非空但全部写入失败时，不标记记忆为已处理

        此前只要 result.nodes 或 result.edges 非空就标记已处理，
        导致 L3 写入全失败的记忆被永久跳过无法重试。
        修复后仅当至少一条节点/边写入成功时才标记。
        """
        from iris_memory.l2_memory.models import MemoryEntry
        from iris_memory.l3_kg.models import GraphNode, GraphEdge, ExtractionResult

        # 构造一条未处理记忆
        mem = MemoryEntry(
            id="mem_1",
            content="Alice 喜欢编程",
            metadata={"group_id": "group_123", "user_id": "u1"},
        )

        l2 = Mock()
        l2.is_available = True
        l2.get_unprocessed_count = AsyncMock(return_value=10)
        l2.get_unprocessed_memories = AsyncMock(return_value=[mem])
        l2.mark_memories_processed = AsyncMock()

        l3 = Mock()
        l3.is_available = True
        l3.add_node = AsyncMock(return_value=False)
        l3.add_edge = AsyncMock(return_value=False)

        llm = Mock()

        # 构造非空提取结果（有节点也有边）
        node = GraphNode(id="", label="Person", name="Alice", content="软件工程师")
        node.id = node.generate_id()
        edge = GraphEdge(source_id="src_id", target_id="tgt_id", relation_type="KNOWS")
        fake_result = ExtractionResult(nodes=[node], edges=[edge])

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            with patch("iris_memory.l3_kg.EntityExtractor") as MockExtractor:
                MockExtractor.return_value.extract_from_memories = AsyncMock(
                    return_value=fake_result
                )
                result = await phase.execute(l2, l3, llm)

        # 全部写入失败，不应标记为已处理
        l2.mark_memories_processed.assert_not_called()
        assert result["memories_processed"] == 0
        assert result["nodes_extracted"] == 0
        assert result["edges_extracted"] == 0

    @pytest.mark.asyncio
    async def test_build_user_aliases_from_memory_metadata(self, phase):
        """从 L2 metadata 的 user_name 构建别名映射（无需 profile）"""
        from iris_memory.l2_memory.models import MemoryEntry

        memories = [
            MemoryEntry(
                id="m1",
                content="庭喜欢吃苹果",
                metadata={"user_id": "234916823", "user_name": "庭"},
            ),
            MemoryEntry(
                id="m2",
                content="李四喜欢编程",
                metadata={"user_id": "user_002", "user_name": "李四"},
            ),
            MemoryEntry(
                id="m3",
                content="无主体内容",
                metadata={},
            ),
        ]

        aliases = await phase._build_user_aliases(memories, "default", None)

        assert aliases["234916823"] == ["庭"]
        assert aliases["user_002"] == ["李四"]
        assert len(aliases) == 2

    @pytest.mark.asyncio
    async def test_build_user_aliases_supplemented_by_profile(self, phase):
        """画像补充历史昵称到别名映射"""
        from iris_memory.l2_memory.models import MemoryEntry
        from iris_memory.profile.models import UserProfile

        memories = [
            MemoryEntry(
                id="m1",
                content="庭喜欢吃苹果",
                metadata={"user_id": "234916823", "user_name": "庭", "group_id": "g1"},
            ),
        ]

        # 构造画像：当前昵称 + 历史昵称
        profile = UserProfile(user_id="234916823")
        profile.user_name = "小庭"
        profile.historical_names = ["庭庭"]

        from iris_memory.profile.storage import ProfileStorage

        profile_storage = Mock(spec=ProfileStorage)
        profile_storage.is_available = True
        profile_storage.get_user_profile = AsyncMock(return_value=profile)

        component_manager = Mock()
        component_manager.get_available_component = Mock(return_value=profile_storage)

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            aliases = await phase._build_user_aliases(
                memories, "default", component_manager
            )

        # metadata 昵称 + 画像当前昵称 + 历史昵称都应在别名中
        assert set(aliases["234916823"]) == {"庭", "小庭", "庭庭"}

    @pytest.mark.asyncio
    async def test_build_user_aliases_no_component_manager(self, phase):
        """无 component_manager 时仅用 metadata 昵称，不报错"""
        from iris_memory.l2_memory.models import MemoryEntry

        memories = [
            MemoryEntry(
                id="m1",
                content="内容",
                metadata={"user_id": "u1"},  # 无 user_name
            ),
        ]

        aliases = await phase._build_user_aliases(memories, "default", None)
        # 即使没有昵称，也保留稳定 ID，供提取器补打 user_id 标记。
        assert aliases == {"u1": []}

    @pytest.mark.asyncio
    async def test_execute_passes_user_aliases_to_extractor(self, phase):
        """execute 应将 user_aliases 注入 context 传给 extractor"""
        from iris_memory.l2_memory.models import MemoryEntry
        from iris_memory.l3_kg.models import ExtractionResult

        mem = MemoryEntry(
            id="mem_1",
            content="庭喜欢吃苹果",
            metadata={
                "group_id": "group_123",
                "user_id": "234916823",
                "user_name": "庭",
            },
        )

        l2 = Mock()
        l2.is_available = True
        l2.get_unprocessed_count = AsyncMock(return_value=10)
        l2.get_unprocessed_memories = AsyncMock(return_value=[mem])
        l2.mark_memories_processed = AsyncMock()

        l3 = Mock()
        l3.is_available = True
        l3.add_node = AsyncMock(return_value=True)

        llm = Mock()

        captured_context = {}

        async def fake_extract(memories, context):
            captured_context.update(context)
            return ExtractionResult(nodes=[], edges=[])

        with patch(
            "iris_memory.dream.knowledge_extract.get_config",
            return_value=_mock_config(),
        ):
            with patch("iris_memory.l3_kg.EntityExtractor") as MockExtractor:
                MockExtractor.return_value.extract_from_memories = AsyncMock(
                    side_effect=fake_extract
                )
                await phase.execute(l2, l3, llm)

        assert captured_context.get("user_aliases") == {"234916823": ["庭"]}
