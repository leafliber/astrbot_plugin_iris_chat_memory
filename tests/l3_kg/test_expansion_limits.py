"""关系扩展应同时遵守节点/边预算，且所有边端点都在返回节点中。"""

import sqlite3
import unittest

from iris_memory.l3_kg.adapter import L3KGAdapter


class TestExpansionLimits(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = L3KGAdapter()
        self.adapter._db = sqlite3.connect(":memory:")
        self.adapter._db.row_factory = sqlite3.Row
        self.adapter._create_schema_unlocked()
        self.adapter._is_available = True
        self.addCleanup(self.adapter._db.close)

    def node(self, node_id, group="test_group"):
        self.adapter._db.execute(
            "INSERT INTO nodes(id,label,name,group_id) VALUES(?,?,?,?)",
            (node_id, "Concept", node_id, group),
        )

    def edge(self, source, target, relation="RELATED_TO"):
        self.adapter._db.execute(
            "INSERT INTO edges(source_id,target_id,relation_type) VALUES(?,?,?)",
            (source, target, relation),
        )

    async def expand(self, seeds, **kwargs):
        return await self.adapter.expand_from_nodes(
            seeds, group_id=kwargs.pop("group_id", "test_group"), **kwargs
        )

    def assert_closed(self, nodes, edges, max_nodes, max_edges):
        ids = {node["id"] for node in nodes}
        self.assertLessEqual(len(nodes), max_nodes)
        self.assertEqual(len(nodes), len(ids))
        self.assertLessEqual(len(edges), max_edges)
        for edge in edges:
            self.assertIn(edge["source"], ids)
            self.assertIn(edge["target"], ids)

    async def test_two_hundred_neighbors_respect_node_cap(self):
        self.node("seed")
        for index in range(200):
            self.node(f"n{index}")
            self.edge("seed", f"n{index}")
        nodes, edges = await self.expand(["seed"], max_nodes=100, max_edges=200)
        self.assertEqual(len(nodes), 100)
        self.assertEqual(len(edges), 99)
        self.assert_closed(nodes, edges, 100, 200)

    async def test_budget_shared_by_multiple_frontier_nodes(self):
        for value in ["a", "b", *[f"n{i}" for i in range(10)]]:
            self.node(value)
        for value in [f"n{i}" for i in range(10)]:
            self.edge("a", value)
            self.edge("b", value)
        nodes, edges = await self.expand(["a", "b"], max_nodes=5)
        self.assertEqual(len(nodes), 5)
        self.assert_closed(nodes, edges, 5, 200)

    async def test_seed_cap_preserves_caller_order_after_scope_filter(self):
        for value in ["a", "b", "c"]:
            self.node(value)
        self.node("foreign", group="another_group")
        nodes, edges = await self.expand(["foreign", "c", "c", "a", "b"], max_nodes=2, max_depth=0)
        self.assertEqual([node["id"] for node in nodes], ["c", "a"])
        self.assert_closed(nodes, edges, 2, 200)

    async def test_full_seed_budget_keeps_edges_between_retained_seeds(self):
        self.node("a")
        self.node("b")
        self.node("outside")
        self.edge("a", "b")
        self.edge("a", "outside")
        nodes, edges = await self.expand(["a", "b"], max_nodes=2)
        self.assertEqual({(edge["source"], edge["target"]) for edge in edges}, {("a", "b")})
        self.assert_closed(nodes, edges, 2, 200)

    async def test_multi_level_expansion_never_exceeds_cap(self):
        self.node("seed")
        for index in range(4):
            parent = f"p{index}"
            self.node(parent)
            self.edge("seed", parent)
            for child_index in range(10):
                child = f"c{index}_{child_index}"
                self.node(child)
                self.edge(parent, child)
        nodes, edges = await self.expand(["seed"], max_nodes=8, max_depth=2)
        self.assertEqual(len(nodes), 8)
        self.assert_closed(nodes, edges, 8, 200)

    async def test_zero_and_negative_node_budgets_are_empty(self):
        self.node("seed")
        for budget in [0, -1]:
            with self.subTest(budget=budget):
                self.assertEqual(await self.expand(["seed"], max_nodes=budget), ([], []))

    async def test_edge_budget_remains_independent(self):
        self.node("seed")
        for index in range(20):
            self.node(f"n{index}")
            self.edge("seed", f"n{index}")
        nodes, edges = await self.expand(["seed"], max_nodes=10, max_edges=3)
        self.assertEqual(len(edges), 3)
        self.assert_closed(nodes, edges, 10, 3)

    async def test_zero_edge_budget_still_returns_bounded_seeds(self):
        for value in ["a", "b", "c"]:
            self.node(value)
        nodes, edges = await self.expand(["c", "a", "b"], max_nodes=2, max_edges=0)
        self.assertEqual([node["id"] for node in nodes], ["c", "a"])
        self.assertEqual(edges, [])

    async def test_no_dangling_edge_when_neighbor_details_are_missing(self):
        self.node("seed")
        self.edge("seed", "missing")
        nodes, edges = await self.expand(["seed"], max_nodes=2, group_id=None)
        self.assertEqual(edges, [])
        self.assert_closed(nodes, edges, 2, 200)

    async def test_private_scope_and_limits_work_together(self):
        for value in ["seed", "a", "b", "c"]:
            self.node(value, group="")
        self.node("foreign", group="another_group")
        for value in ["a", "b", "c", "foreign"]:
            self.edge("seed", value)
        nodes, edges = await self.expand(["seed"], group_id="", max_nodes=3)
        self.assertEqual(len(nodes), 3)
        self.assertEqual({node["group_id"] for node in nodes}, {""})
        self.assert_closed(nodes, edges, 3, 200)

    async def test_parallel_relations_do_not_consume_multiple_node_slots(self):
        self.node("a")
        self.node("b")
        self.edge("a", "b", "KNOWS")
        self.edge("a", "b", "RELATED_TO")
        nodes, edges = await self.expand(["a"], max_nodes=2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(edges), 2)
        self.assert_closed(nodes, edges, 2, 200)
