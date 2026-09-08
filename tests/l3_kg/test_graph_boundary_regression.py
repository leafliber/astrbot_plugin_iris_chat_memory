"""数字昵称与私聊关系扩展的实际 SQLite 边界回归。"""

import json
import sqlite3
import unittest

from iris_memory.l3_kg.adapter import L3KGAdapter


class TestGraphBoundaryRegression(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = L3KGAdapter()
        self.adapter._db = sqlite3.connect(":memory:")
        self.adapter._db.row_factory = sqlite3.Row
        self.adapter._create_schema_unlocked()
        self.adapter._is_available = True
        self.addCleanup(self.adapter._db.close)

    def add(self, node_id, name, group="group_A", properties=None):
        self.adapter._db.execute(
            "INSERT INTO nodes(id,label,name,group_id,properties) VALUES(?,?,?,?,?)",
            (node_id, "Person", name, group, json.dumps(properties or {})),
        )

    def edge(self, source, target):
        self.adapter._db.execute(
            "INSERT INTO edges(source_id,target_id,relation_type) VALUES(?,?,?)",
            (source, target, "KNOWS"),
        )

    async def ids(self, query):
        rows = await self.adapter.search_nodes_detailed(
            query, label="Person", group_id="group_A"
        )
        return {row["id"] for row in rows}

    async def test_digits_inside_names_and_aliases_are_not_ids(self):
        names = [
            "Alice12345", "阿蓝12345", "Alice_12345", "user-12345",
            "user.12345", "12345Alice", "bot12345@example.org",
        ]
        for index, name in enumerate(names):
            self.add(f"name_{index}", name)
            self.add(f"alias_{index}", f"owner_{index}", properties={"aliases": [name]})
        for index, name in enumerate(names):
            with self.subTest(name=name):
                self.assertEqual(await self.ids(name), {f"name_{index}", f"alias_{index}"})

    async def test_separate_id_takes_precedence_over_numeric_nickname(self):
        self.add("nickname", "Alice12345")
        self.add("owner", "1111122222", properties={"user_id": "1111122222"})
        self.assertEqual(await self.ids("Alice12345 1111122222"), {"owner"})

    async def test_unknown_explicit_id_does_not_match_numeric_nickname(self):
        self.add("nickname", "Alice12345")
        self.assertEqual(await self.ids("Alice12345 9999988888"), set())

    async def test_plain_and_delimited_ids(self):
        self.add("owner", "1111122222", properties={"user_id": "1111122222"})
        for query in ["1111122222", "QQ:1111122222", "用户: 1111122222", "Alice（1111122222）"]:
            with self.subTest(query=query):
                self.assertEqual(await self.ids(query), {"owner"})

    async def test_long_digit_sequence_is_not_split_into_id(self):
        name = "123456789012345678901"
        self.add("long_name", name)
        self.assertEqual(await self.ids(name), {"long_name"})

    async def test_unicode_escaped_alias_array_is_decoded(self):
        self.add("owner", "1111122222", properties={"aliases": ["阿蓝", "鲸12345"]})
        self.assertEqual(await self.ids("阿蓝"), {"owner"})
        self.assertEqual(await self.ids("鲸12345"), {"owner"})

    async def test_numeric_non_person_alias_without_label(self):
        self.add("standard", "质量管理规范", properties={"aliases": "ISO 12345"})
        self.adapter._db.execute("UPDATE nodes SET label='Concept' WHERE id='standard'")
        rows = await self.adapter.search_nodes_detailed("ISO 12345", group_id="group_A")
        self.assertEqual({row["id"] for row in rows}, {"standard"})

    async def test_exact_alias_survives_content_candidate_limit(self):
        self.add("target", "1111122222", properties={"aliases": ["Alice"]})
        self.adapter._db.execute("UPDATE nodes SET confidence=0.1 WHERE id='target'")
        self.adapter._db.executemany(
            "INSERT INTO nodes(id,label,name,content,confidence,group_id) VALUES(?,?,?,?,?,?)",
            [(f"noise_{i}", "Person", f"Unrelated {i}", "Alice was mentioned", 0.9, "group_A") for i in range(2048)],
        )
        rows = await self.adapter.search_nodes_detailed("Alice", group_id="group_A")
        self.assertEqual(rows[0]["id"], "target")

    async def test_exact_alias_survives_partial_alias_candidate_limit(self):
        self.add("target", "1111122222", properties={"aliases": "Other, Alice, Last"})
        self.adapter._db.execute("UPDATE nodes SET confidence=0.1 WHERE id='target'")
        self.adapter._db.executemany(
            "INSERT INTO nodes(id,label,name,properties,confidence,group_id) VALUES(?,?,?,?,?,?)",
            [(f"noise_{i}", "Person", f"Unrelated {i}", json.dumps({"aliases": [f"AliceFan{i}"]}), 0.9, "group_A") for i in range(2048)],
        )
        rows = await self.adapter.search_nodes_detailed("Alice", group_id="group_A")
        self.assertEqual(rows[0]["id"], "target")

    async def test_whitespace_normalized_alias_survives_candidate_pages(self):
        self.add("target", "1111122222", properties={"aliases": "\tAlice\t"})
        self.adapter._db.execute("UPDATE nodes SET confidence=0.1 WHERE id='target'")
        self.adapter._db.executemany(
            "INSERT INTO nodes(id,label,name,properties,confidence,group_id) VALUES(?,?,?,?,?,?)",
            [(f"noise_{i}", "Person", f"Unrelated {i}", json.dumps({"aliases": [f"Alice suffix{i}"]}), 0.9, "group_A") for i in range(2048)],
        )
        rows = await self.adapter.search_nodes_detailed("Alice", group_id="group_A")
        self.assertEqual(rows[0]["id"], "target")

    async def test_explicit_id_survives_non_person_noise(self):
        self.add("target", "1111122222", properties={"aliases": "RealName", "user_id": "1111122222"})
        self.adapter._db.execute("UPDATE nodes SET confidence=0.1 WHERE id='target'")
        self.adapter._db.executemany(
            "INSERT INTO nodes(id,label,name,content,confidence,group_id) VALUES(?,?,?,?,?,?)",
            [(f"noise_{i}", "Concept", f"Unrelated {i}", "OldName 1111122222", 0.9, "group_A") for i in range(2048)],
        )
        rows = await self.adapter.search_nodes_detailed("OldName 1111122222", group_id="group_A")
        self.assertEqual(rows[0]["id"], "target")

    async def test_pagination_continues_after_zero_score_pages(self):
        self.add("z_target", "Alice")
        self.adapter._db.executemany(
            "INSERT INTO nodes(id,label,name,confidence,group_id) VALUES(?,?,?,?,?)",
            [(f"a_noise_{i:05}", "Person", f"AliceSuffix{i}", 0.9, "group_A") for i in range(4096)],
        )
        # 前两页被 SQL 的 Alice 分词预选，但完整查询/词项都不匹配其名称。
        rows = await self.adapter.search_nodes_detailed("find Alice now", group_id="group_A")
        self.assertEqual([row["id"] for row in rows], ["z_target"])

    async def test_private_expansion_excludes_foreign_neighbors_at_all_depths(self):
        self.add("private_seed", "Seed", group="")
        self.add("private_neighbor", "Neighbor", group="")
        self.add("foreign", "Foreign", group="group_B")
        self.edge("private_seed", "private_neighbor")
        self.edge("private_seed", "foreign")
        self.edge("private_neighbor", "foreign")
        for depth in [1, 2]:
            with self.subTest(depth=depth):
                nodes, edges = await self.adapter.expand_from_nodes(
                    ["private_seed"], max_depth=depth, group_id=""
                )
                self.assertEqual({row["id"] for row in nodes}, {"private_seed", "private_neighbor"})
                self.assertEqual({row["group_id"] for row in nodes}, {""})
                self.assertEqual({(edge["source"], edge["target"]) for edge in edges}, {("private_seed", "private_neighbor")})

    async def test_private_rejects_foreign_seed_even_with_private_neighbor(self):
        self.add("private", "Private", group="")
        self.add("foreign", "Foreign", group="group_B")
        self.edge("private", "foreign")
        nodes, edges = await self.adapter.expand_from_nodes(["foreign"], group_id="")
        self.assertEqual((nodes, edges), ([], []))

    async def test_mixed_seeds_keep_only_requested_scope(self):
        self.add("private", "Private", group="")
        self.add("foreign", "Foreign", group="group_B")
        nodes, edges = await self.adapter.expand_from_nodes(["private", "foreign"], group_id="")
        self.assertEqual({row["id"] for row in nodes}, {"private"})
        self.assertEqual(edges, [])

    async def test_group_scope_still_excludes_other_group(self):
        self.add("seed", "Seed")
        self.add("neighbor", "Neighbor")
        self.add("foreign", "Foreign", group="group_B")
        self.edge("seed", "neighbor")
        self.edge("neighbor", "foreign")
        nodes, edges = await self.adapter.expand_from_nodes(["seed"], group_id="group_A")
        self.assertEqual({row["id"] for row in nodes}, {"seed", "neighbor"})
        self.assertEqual(len(edges), 1)

    async def test_none_still_means_explicit_global_mode(self):
        self.add("private", "Private", group="")
        self.add("foreign", "Foreign", group="group_B")
        self.edge("private", "foreign")
        nodes, edges = await self.adapter.expand_from_nodes(["private"], group_id=None)
        self.assertEqual({row["id"] for row in nodes}, {"private", "foreign"})
        self.assertEqual(len(edges), 1)

    async def test_empty_and_missing_seeds_are_safe(self):
        for seeds in [[], ["missing"]]:
            with self.subTest(seeds=seeds):
                result = await self.adapter.expand_from_nodes(seeds, group_id="")
                self.assertEqual(result, ([], []))
