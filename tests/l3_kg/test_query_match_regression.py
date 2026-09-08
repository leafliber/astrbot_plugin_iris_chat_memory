"""真实 SQLite 适配器的身份匹配、范围与工具输出回归检查。"""

import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from iris_memory.l3_kg.adapter import L3KGAdapter
from iris_memory.tools.search_knowledge_graph import SearchKnowledgeGraphTool


class TestQueryMatchRegression(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = L3KGAdapter()
        self.adapter._db = sqlite3.connect(":memory:")
        self.adapter._db.row_factory = sqlite3.Row
        self.adapter._create_schema_unlocked()
        self.adapter._is_available = True
        self.addCleanup(self.adapter._db.close)
        self.add("alice", "1111122222", {"user_id": "1111122222", "aliases": ["Alice（测试版）", "Alice"]})
        self.add("bob", "3333344444", {"user_id": 3333344444, "aliases": "Bob,鲍勃"})
        self.add("legacy", "[用户:1111122222]", {})
        self.add("noise", "Unrelated", {"active_users": ["1111122222"], "source_memory_ids": ["Alice"]})
        self.add("group_b", "1111122222", {"user_id": "1111122222"}, group="group_B")
        self.add("private", "1111122222", {"user_id": "1111122222"}, group="")
        self.add("literal", "rate_100%", {}, label="Concept")
        self.add("wildcard_noise", "rateX100abc", {}, label="Concept")
        self.add("broken", "broken", "not-json")

    def add(self, node_id, name, props, group="group_A", label="Person", content=""):
        self.adapter._db.execute(
            "INSERT INTO nodes(id,name,properties,group_id,label,content,confidence) VALUES(?,?,?,?,?,?,?)",
            (node_id, name, props if isinstance(props, str) else json.dumps(props), group, label, content, 0.8),
        )

    async def rows(self, query, group="group_A", label="Person", limit=15):
        return await self.adapter.search_nodes_detailed(query, label, group, limit)

    async def ids(self, *args, **kwargs):
        return {row["id"] for row in await self.rows(*args, **kwargs)}

    async def test_name_and_id_find_target(self):
        self.assertEqual(await self.ids("Alice 测试版 1111122222"), {"alice", "legacy"})

    async def test_second_name_and_id(self):
        self.assertEqual(await self.ids("Bob 3333344444"), {"bob"})

    async def test_active_users_is_not_identity(self):
        self.assertNotIn("noise", await self.ids("1111122222"))

    async def test_alias_list_case_insensitive(self):
        self.assertEqual(await self.ids("ALICE"), {"alice"})

    async def test_comma_separated_aliases(self):
        self.assertEqual(await self.ids("鲍勃"), {"bob"})

    async def test_unknown_id_cannot_fall_back_to_wrong_person(self):
        self.assertEqual(await self.ids("Alice 9999988888"), set())

    async def test_other_group_isolated(self):
        self.assertEqual(await self.ids("1111122222", group="group_B"), {"group_b"})

    async def test_empty_private_scope_is_not_global(self):
        self.assertEqual(await self.ids("1111122222", group=""), {"private"})

    async def test_global_scope(self):
        self.assertEqual(await self.ids("1111122222", group=None), {"alice", "legacy", "group_b", "private"})

    async def test_wildcards_remain_literal(self):
        self.assertEqual(await self.ids("rate_100%", label="Concept"), {"literal"})

    async def test_content_search_still_works(self):
        self.add("topic", "共同项目", {}, label="Topic", content="一次合作经历")
        self.assertEqual(await self.ids("合作经历", label="Topic"), {"topic"})

    async def test_non_person_numeric_query(self):
        self.add("error", "错误 12345", {}, label="Concept")
        self.assertIn("error", await self.ids("错误 12345", label=None))

    async def test_invalid_json_does_not_abort(self):
        self.assertEqual(await self.ids("broken"), {"broken"})

    async def test_empty_query(self):
        self.assertEqual(await self.rows("  "), [])

    async def test_result_limit(self):
        self.assertEqual(len(await self.rows("1111122222", group=None, limit=1)), 1)

    async def test_nullable_content_and_confidence(self):
        self.adapter._db.execute("UPDATE nodes SET content=NULL,confidence=NULL WHERE id='alice'")
        row = (await self.rows("Alice"))[0]
        self.assertEqual(row["content"], "")
        self.assertEqual(row["confidence"], 0)

    async def test_actual_tool_search_expand_and_format(self):
        self.add("belief", "游戏设计", {}, label="Belief", content="玩法比画面重要")
        self.adapter._db.execute(
            "INSERT INTO edges(source_id,target_id,relation_type) VALUES(?,?,?)",
            ("alice", "belief", "HAS_BELIEF"),
        )
        platform = Mock()
        platform.get_group_id.return_value = "group_A"
        platform.get_user_id.return_value = "requester"
        manager = Mock()
        manager.get_component.return_value = self.adapter
        config = Mock()
        config.get.side_effect = lambda key, default=None: {
            "isolation_config.enable_group_memory_isolation": True,
        }.get(key, default)
        context = SimpleNamespace(context=SimpleNamespace(event=object()))
        before = self.adapter._db.total_changes
        with patch("iris_memory.platform.get_adapter", return_value=platform), patch("iris_memory.config.get_config", return_value=config), patch("iris_memory.tools.search_knowledge_graph.get_component_manager", return_value=manager):
            text = await SearchKnowledgeGraphTool().call(context, query="Alice 1111122222", label="Person", expand_depth=1)
        self.assertIn("相信 (HAS_BELIEF)", text)
        self.assertIn("Alice 1111122222", text)
        self.assertIn("玩法比画面重要", text)
        self.assertNotIn("搜索知识图谱失败", text)
        self.assertEqual(self.adapter._db.total_changes, before)
