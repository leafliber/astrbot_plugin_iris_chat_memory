"""图谱工具返回值必须能直接解释每条关系，不能要求模型猜测节点 ID。"""

from copy import deepcopy
import unittest

from iris_memory.l3_kg.tool_formatter import format_graph_results
from iris_memory.tools.search_knowledge_graph import SearchKnowledgeGraphTool


def node(node_id, name, label="Person", content=""):
    return dict(id=node_id, name=name, label=label, content=content, confidence=0.8)


class TestGraphResultFormat(unittest.TestCase):
    def setUp(self):
        self.person = node("person_test", "小明")
        self.belief = node("belief_test", "游戏设计", "Belief", "玩法比画面重要")
        self.edge = dict(_src="person_test", _dst="belief_test", relation_type="HAS_BELIEF")

    def render(self, edges=None, expanded=None):
        return SearchKnowledgeGraphTool()._format_results(
            [self.person],
            [self.person, self.belief] if expanded is None else expanded,
            [self.edge] if edges is None else edges,
            "小明",
        )

    def test_actual_tool_resolves_sqlite_edge_ids(self):
        relation = self.render().split("**关联关系**", 1)[1]
        self.assertIn("小明 —[相信 (HAS_BELIEF)]→ [Belief] 游戏设计：玩法比画面重要", relation)
        self.assertNotIn("person_test", relation)
        self.assertNotIn("belief_test", relation)

    def test_preference_is_readable(self):
        self.edge["relation_type"] = "HAS_PREFERENCE"
        self.assertIn("偏好 (HAS_PREFERENCE)", self.render())

    def test_native_source_target_fields(self):
        edge = dict(source="person_test", target="belief_test", relation_type="HAS_BELIEF")
        self.assertIn("玩法比画面重要", self.render([edge]).split("**关联关系**")[1])

    def test_source_id_target_id_fields(self):
        edge = dict(source_id="person_test", target_id="belief_test", relation_type="HAS_BELIEF")
        self.assertIn("玩法比画面重要", self.render([edge]).split("**关联关系**")[1])

    def test_truncated_entity_still_resolves_in_edge(self):
        extras = [node(f"extra_{i}", f"额外实体{i}") for i in range(12)]
        text = self.render(expanded=[self.person, *extras, self.belief])
        entities, relations = text.split("**关联关系**", 1)
        self.assertNotIn("玩法比画面重要", entities)
        self.assertIn("展示 10 / 共 13 个", entities)
        self.assertIn("玩法比画面重要", relations)

    def test_edge_count_reports_shown_and_total(self):
        text = self.render([dict(self.edge) for _ in range(43)])
        self.assertIn("展示 15 / 共 43 条", text)
        self.assertEqual(text.count("—["), 15)

    def test_missing_node_keeps_explicit_fallback(self):
        text = self.render(expanded=[self.person])
        self.assertIn("未知实体（ID: belief_test；本次未返回详情）", text)
        self.assertNotIn("玩法比画面重要", text)

    def test_pre_named_edge_is_supported(self):
        self.edge["target_name"] = "已返回的名称"
        self.assertIn("已返回的名称", self.render(expanded=[]))

    def test_null_edge_name_falls_back_to_id(self):
        self.edge["target_name"] = None
        self.assertIn("ID: belief_test", self.render(expanded=[]))

    def test_missing_id_does_not_invent_entity(self):
        self.assertIn("未知实体（缺少节点标识）", self.render([{}]))

    def test_custom_relation_is_preserved(self):
        self.edge["relation_type"] = "CUSTOM_RELATION"
        self.assertIn("—[CUSTOM_RELATION]→", self.render())

    def test_null_node_fields_and_invalid_confidence(self):
        self.person.update(name=None, content=None, confidence="invalid")
        self.belief.update(content=None)
        text = self.render()
        self.assertIn("未命名实体", text)
        self.assertIn("置信度: 0.00", text)
        self.assertNotIn("None", text)

    def test_long_endpoint_content_is_bounded(self):
        self.belief["content"] = "长" * 1000
        relation = self.render().split("**关联关系**")[1]
        self.assertIn("长" * 100 + "...", relation)
        self.assertNotIn("长" * 101, relation)

    def test_does_not_modify_input(self):
        before = deepcopy((self.person, self.belief, self.edge))
        self.render()
        self.assertEqual(before, (self.person, self.belief, self.edge))

    def test_no_expansion_has_no_relation_section(self):
        self.assertNotIn("关联关系", self.render([], []))

    def test_empty_results_are_safe(self):
        self.assertIn("0 个", format_graph_results([], [], [], "无结果"))
