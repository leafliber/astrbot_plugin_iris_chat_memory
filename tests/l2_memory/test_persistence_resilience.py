"""L2 持久化韧性回归测试。

覆盖以下修复的回归保护：
- FAISS 索引原子写（tmp + os.replace，不留半截文件）
- 索引损坏后的启动恢复（改名保留 + 从 SQLite 对账重建）
- 索引/DB 脱同步时的 ID 集对账重建（增量修复 + 嵌入重试退避）
- checkpoint 任务强引用与 _checkpointing 复位
- 嵌入模型迁移失败时备份保留（不删唯一全量副本）
- 周期索引审计任务：运行期自愈脱同步、shutdown 干净取消
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from iris_memory.config import init_config
from iris_memory.config.config import reset_config
from iris_memory.l2_memory.adapter import L2MemoryAdapter

DIM = 8


@pytest.fixture(autouse=True)
def _reset_iris_config():
    yield
    reset_config()


@pytest.fixture
def adapter(tmp_path: Path) -> L2MemoryAdapter:
    """带真实 SQLite 与真实 FAISS 的最小可用适配器。"""
    from iris_memory.config import get_config

    init_config(
        {
            "l1_buffer": {"enable": True},
            "l2_memory": {
                "enable": True,
                "embedding_source": "provider",
            },
        },
        tmp_path,
    )
    # checkpoint 阈值是 hidden 键，走隐藏配置通道
    get_config().set_hidden("l2_checkpoint_writes", 3)
    adapter = L2MemoryAdapter()
    adapter._is_available = True
    adapter._persist_dir = tmp_path / "faiss" / "memory_default"
    adapter._persist_dir.mkdir(parents=True)
    adapter._embedding_dimensions = DIM
    adapter._actual_embedding_model = "test-model"
    adapter._db = adapter._open_db(adapter._persist_dir / "metadata.db")
    # _embed 默认返回固定向量（各测试可再覆盖）
    adapter._embed = AsyncMock(side_effect=lambda texts: [[0.1] * DIM for _ in texts])
    # 嵌入重试退避置 0，避免重试路径的真实 sleep 拖慢测试
    adapter._embed_retry_backoff = 0.0
    return adapter


def _seed_db(adapter: L2MemoryAdapter, ids: list[int]) -> None:
    """直接向 SQLite 插入 memories 行（绕过 FAISS 写入）。"""
    with adapter._lock:
        for faiss_idx in ids:
            adapter._db.execute(
                "INSERT OR REPLACE INTO memories"
                " (faiss_idx, memory_id, content, metadata, persona_id)"
                " VALUES (?, ?, ?, ?, 'default')",
                (faiss_idx, f"mem_{faiss_idx:04d}", f"内容 {faiss_idx}", "{}"),
            )
        adapter._db.commit()


class TestAtomicIndexWrite:
    def test_write_and_read_back_roundtrip(self, adapter: L2MemoryAdapter):
        import faiss
        import numpy as np

        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM, [0.2] * DIM], dtype=np.float32),
            np.array([1, 2], dtype=np.int64),
        )
        path = adapter._persist_dir / "index.faiss"
        adapter._write_index_atomic(index, path)

        loaded = faiss.read_index(str(path))
        assert loaded.ntotal == 2
        # 不残留临时文件
        leftovers = list(adapter._persist_dir.glob("index.faiss.*.tmp"))
        assert leftovers == []

    def test_failed_write_keeps_old_file_intact(self, adapter: L2MemoryAdapter):
        import faiss
        import numpy as np

        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM], dtype=np.float32),
            np.array([1], dtype=np.int64),
        )
        path = adapter._persist_dir / "index.faiss"
        adapter._write_index_atomic(index, path)

        with patch("faiss.write_index", side_effect=OSError("磁盘已满")):
            with pytest.raises(OSError):
                adapter._write_index_atomic(index, path)

        # 旧文件仍是完整可读的旧内容
        loaded = faiss.read_index(str(path))
        assert loaded.ntotal == 1
        assert list(adapter._persist_dir.glob("index.faiss.*.tmp")) == []


class TestCorruptIndexRecovery:
    @pytest.mark.asyncio
    async def test_corrupt_index_renamed_and_rebuilt_from_db(
        self, adapter: L2MemoryAdapter
    ):
        _seed_db(adapter, [1, 2, 3])
        # 写入截断的伪索引文件
        (adapter._persist_dir / "index.faiss").write_bytes(b"\x00garbage")

        await adapter._load_existing(0)

        # 损坏文件保留现场
        assert (adapter._persist_dir / "index.faiss.corrupt").exists()
        # 索引已按 DB 行重建
        assert adapter._index is not None
        assert adapter._index.ntotal == 3

    @pytest.mark.asyncio
    async def test_valid_index_with_drift_reconciled(self, adapter: L2MemoryAdapter):
        import faiss
        import numpy as np

        _seed_db(adapter, [1, 2, 3])
        # 索引只含 1、2，缺 3（模拟 checkpoint 丢失窗口）
        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM, [0.2] * DIM], dtype=np.float32),
            np.array([1, 2], dtype=np.int64),
        )
        faiss.write_index(index, str(adapter._persist_dir / "index.faiss"))

        await adapter._load_existing(0)

        assert adapter._index.ntotal == 3
        # 增量修复：只重嵌入缺失的第 3 行，不触碰存量向量
        embedded = [t for call in adapter._embed.await_args_list for t in call.args[0]]
        assert embedded == ["内容 3"]
        # 修复结果已原子落盘
        on_disk = faiss.read_index(str(adapter._persist_dir / "index.faiss"))
        assert on_disk.ntotal == 3

    @pytest.mark.asyncio
    async def test_index_with_stale_extra_vectors_reconciled(
        self, adapter: L2MemoryAdapter
    ):
        import faiss
        import numpy as np

        _seed_db(adapter, [1])
        # 索引含已删除的 99（模拟删除已 commit、索引未 checkpoint）
        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM, [0.2] * DIM], dtype=np.float32),
            np.array([1, 99], dtype=np.int64),
        )
        faiss.write_index(index, str(adapter._persist_dir / "index.faiss"))

        await adapter._load_existing(0)

        assert adapter._index.ntotal == 1
        # 摘除脏向量无需嵌入
        adapter._embed.assert_not_awaited()
        on_disk = faiss.read_index(str(adapter._persist_dir / "index.faiss"))
        assert on_disk.ntotal == 1

    @pytest.mark.asyncio
    async def test_consistent_index_untouched(self, adapter: L2MemoryAdapter):
        import faiss
        import numpy as np

        _seed_db(adapter, [1, 2])
        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM, [0.2] * DIM], dtype=np.float32),
            np.array([1, 2], dtype=np.int64),
        )
        faiss.write_index(index, str(adapter._persist_dir / "index.faiss"))
        embed = adapter._embed

        await adapter._load_existing(0)

        assert adapter._index.ntotal == 2
        embed.assert_not_awaited()


class TestReconcileRetryAndRuntimeHeal:
    """启动对账失败后，适配器保持可用并在稍后重试时自愈。"""

    @pytest.mark.asyncio
    async def test_transient_embed_failure_retried_then_repaired(
        self, adapter: L2MemoryAdapter
    ):
        _seed_db(adapter, [1, 2, 3])
        # 无索引文件：_open_storage 建空索引，全部行进入缺失集
        calls = {"n": 0}

        async def flaky(texts):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("provider 503")
            return [[0.1] * DIM for _ in texts]

        adapter._embed = AsyncMock(side_effect=flaky)

        await adapter._load_existing(0)

        # 前两次失败经退避重试后成功，索引完整重建
        assert adapter._index.ntotal == 3
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_reconcile_failure_keeps_partial_progress(
        self, adapter: L2MemoryAdapter
    ):
        import faiss

        _seed_db(adapter, [1, 2, 3])
        calls = {"n": 0}

        async def provider_down(texts):
            # 前 3 次调用全部失败：单批重试耗尽，对账以失败告终
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("provider down")
            return [[0.1] * DIM for _ in texts]

        adapter._embed = AsyncMock(side_effect=provider_down)

        # 对账失败但不得抛出（适配器保持可用，检索退化为漏召回）
        await adapter._load_existing(0)
        assert adapter._index.ntotal == 0

        # Provider 恢复后再次对账（等价于周期审计触发）：全量补回
        adapter._embed = AsyncMock(
            side_effect=lambda texts: [[0.1] * DIM for _ in texts]
        )
        await adapter._reconcile_index_with_db()

        assert adapter._index.ntotal == 3
        on_disk = faiss.read_index(str(adapter._persist_dir / "index.faiss"))
        assert on_disk.ntotal == 3

    @pytest.mark.asyncio
    async def test_late_write_race_does_not_duplicate_ids(
        self, adapter: L2MemoryAdapter
    ):
        import numpy as np

        _seed_db(adapter, [1, 2])
        adapter._index = adapter._create_index(DIM)
        rows = [(1, "内容 1"), (2, "内容 2")]

        # 模拟：重嵌入期间槽位 1 已被其他写入路径补上
        adapter._index.add_with_ids(
            np.array([[0.5] * DIM], dtype=np.float32), np.array([1], dtype=np.int64)
        )

        added = await asyncio.to_thread(
            adapter._add_missing_vectors_locked,
            rows,
            [[0.1] * DIM, [0.2] * DIM],
        )

        assert added == 1
        assert adapter._index.ntotal == 2


class TestIndexAuditLifecycle:
    """周期审计任务的生命周期与自愈行为。"""

    @pytest.mark.asyncio
    async def test_audit_disabled_by_config(self, adapter: L2MemoryAdapter):
        from iris_memory.config import get_config

        get_config().set_hidden("l2_index_audit_interval_sec", 0)
        adapter._start_index_audit()

        assert adapter._audit_task is None

    @pytest.mark.asyncio
    async def test_audit_task_started_then_cancelled_on_shutdown(
        self, adapter: L2MemoryAdapter
    ):
        from iris_memory.config import get_config

        get_config().set_hidden("l2_index_audit_interval_sec", 60)
        adapter._index = adapter._create_index(DIM)

        adapter._start_index_audit()
        task = adapter._audit_task
        assert task is not None and not task.done()

        # 幂等：重复启动不产生第二个任务
        adapter._start_index_audit()
        assert adapter._audit_task is task

        await adapter.shutdown()

        assert adapter._audit_task is None
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_audit_loop_repairs_runtime_drift(self, adapter: L2MemoryAdapter):
        """运行期脱同步（如 checkpoint 写失败）由审计轮次自动修复。"""
        import faiss
        import numpy as np

        from iris_memory.config import get_config

        _seed_db(adapter, [1, 2])
        index = faiss.IndexIDMap(faiss.IndexFlatIP(DIM))
        index.add_with_ids(
            np.array([[0.1] * DIM], dtype=np.float32), np.array([1], dtype=np.int64)
        )
        faiss.write_index(index, str(adapter._persist_dir / "index.faiss"))
        adapter._index = index

        # 快速走完一轮审计（跳过循环 sleep，直接执行单轮对账逻辑）
        get_config().set_hidden("l2_index_audit_interval_sec", 60)
        await adapter._reconcile_index_with_db()

        assert adapter._index.ntotal == 2
        embedded = [t for call in adapter._embed.await_args_list for t in call.args[0]]
        assert embedded == ["内容 2"]


class TestCheckpointTaskLifecycle:
    @pytest.mark.asyncio
    async def test_checkpoint_task_referenced_and_flag_resets(
        self, adapter: L2MemoryAdapter
    ):
        adapter._index = adapter._create_index(DIM)
        adapter._dirty = True
        # 配置阈值为 3
        adapter._pending_writes = 0
        for _ in range(3):
            adapter._mark_dirty()

        task = adapter._checkpoint_task
        assert task is not None, "checkpoint 任务必须保存强引用"
        await asyncio.wait_for(task, timeout=5)

        assert adapter._checkpointing is False
        assert adapter._checkpoint_task is None or adapter._checkpoint_task.done()
        assert (adapter._persist_dir / "index.faiss").exists()

    @pytest.mark.asyncio
    async def test_stale_done_callback_does_not_reset_new_checkpoint(
        self, adapter: L2MemoryAdapter
    ):
        loop = asyncio.get_running_loop()
        old_task = loop.create_future()
        old_task.set_result(None)
        new_task = loop.create_future()
        adapter._checkpoint_task = new_task
        adapter._checkpointing = True

        adapter._on_checkpoint_done(old_task)

        assert adapter._checkpointing is True
        assert adapter._checkpoint_task is new_task
        new_task.cancel()

    @pytest.mark.asyncio
    async def test_checkpoint_exception_surfaces_in_log_and_flag_resets(
        self, adapter: L2MemoryAdapter
    ):
        adapter._index = adapter._create_index(DIM)
        adapter._dirty = True
        with patch.object(
            adapter,
            "_write_index_atomic",
            side_effect=OSError("写盘失败"),
        ):
            adapter._pending_writes = 0
            for _ in range(3):
                adapter._mark_dirty()
            task = adapter._checkpoint_task
            assert task is not None
            await asyncio.wait_for(task, timeout=5)

        assert adapter._checkpointing is False


class TestMigrationBackupPreservation:
    @pytest.mark.asyncio
    async def test_import_failure_keeps_backups(self, adapter: L2MemoryAdapter):
        _seed_db(adapter, [1, 2, 3])
        adapter._save_meta()

        with patch(
            "iris_memory.l2_memory.io.MemoryImporter.import_from_file",
            new=AsyncMock(side_effect=RuntimeError("Embedding Provider 超时")),
        ):
            ok = await adapter._migrate_on_model_change("new-model", DIM)

        assert ok is False
        backup_dir = adapter._persist_dir.parent / "migration_backup"
        backups = list(backup_dir.glob("*_migration_backup.json"))
        assert backups, "迁移失败时全量备份必须保留"

    @pytest.mark.asyncio
    async def test_successful_migration_cleans_backups(self, adapter: L2MemoryAdapter):
        _seed_db(adapter, [1, 2])
        adapter._save_meta()

        ok = await adapter._migrate_on_model_change("new-model", DIM)

        assert ok is True
        backup_dir = adapter._persist_dir.parent / "migration_backup"
        assert list(backup_dir.glob("*_migration_backup.json")) == []


class TestDbFetchallDiscipline:
    @pytest.mark.asyncio
    async def test_get_all_entries_returns_rows(self, adapter: L2MemoryAdapter):
        _seed_db(adapter, [1, 2, 5])
        entries = await adapter.get_all_entries()
        assert sorted(e.id for e in entries) == ["mem_0001", "mem_0002", "mem_0005"]


class TestSyncRecoveryRaces:
    @pytest.mark.asyncio
    async def test_delete_during_embedding_does_not_resurrect_vector(self, adapter):
        _seed_db(adapter, [1, 2])
        adapter._index = adapter._create_index(DIM)

        async def embed_then_delete(texts):
            await adapter.delete_entries(["mem_0001"])
            return [[0.1] * DIM for _ in texts]

        adapter._embed = AsyncMock(side_effect=embed_then_delete)
        await adapter._reconcile_index_with_db()
        assert adapter._index_ids_unlocked() == {2}
        assert adapter._count_db() == 1

    @pytest.mark.asyncio
    async def test_edit_during_embedding_does_not_restore_old_content(self, adapter):
        _seed_db(adapter, [1])
        adapter._index = adapter._create_index(DIM)

        async def embed_then_edit(texts):
            adapter._db_write(
                "UPDATE memories SET content = '新内容' WHERE faiss_idx = 1"
            )
            return [[0.1] * DIM for _ in texts]

        adapter._embed = AsyncMock(side_effect=embed_then_edit)
        await adapter._reconcile_index_with_db()
        assert adapter._index.ntotal == 0
        adapter._embed = AsyncMock(return_value=[[0.2] * DIM])
        await adapter._reconcile_index_with_db()
        adapter._embed.assert_awaited_once_with(["新内容"])
        assert adapter._index_ids_unlocked() == {1}

    @pytest.mark.asyncio
    async def test_consistent_ids_still_remove_stale_free_slots(self, adapter):
        _seed_db(adapter, [1])
        adapter._index = adapter._create_index(DIM)
        adapter._add_missing_vectors_locked([(1, "内容 1")], [[0.1] * DIM])
        adapter._free_list = [1, 3, 3]
        await adapter._reconcile_index_with_db()
        assert adapter._free_list == [3]
        adapter._embed.assert_not_awaited()

    def test_extra_vector_recheck_preserves_reused_slot(self, adapter):
        _seed_db(adapter, [1])
        adapter._index = adapter._create_index(DIM)
        adapter._add_missing_vectors_locked([(1, "内容 1")], [[0.1] * DIM])
        adapter._remove_index_ids_locked([1])
        assert adapter._index_ids_unlocked() == {1}

    @pytest.mark.asyncio
    async def test_recovery_validates_dimensions_before_native_faiss(self, adapter):
        _seed_db(adapter, [1])
        adapter._index = adapter._create_index(DIM)
        adapter._embed = AsyncMock(return_value=[[0.1] * (DIM + 1)])
        await adapter._reconcile_index_with_db()
        assert adapter._index.ntotal == 0
        assert adapter._count_db() == 1
        adapter._embed = AsyncMock(return_value=[[0.1] * DIM])
        await adapter._reconcile_index_with_db()
        assert adapter._index.ntotal == 1

    @pytest.mark.asyncio
    async def test_loaded_dimension_mismatch_defers_repair_to_migration(self, adapter):
        import faiss

        _seed_db(adapter, [1])
        wrong_index = adapter._create_index(DIM + 1)
        faiss.write_index(wrong_index, str(adapter._persist_dir / "index.faiss"))
        await adapter._load_existing(0)
        assert adapter._embedding_dimensions == DIM
        assert adapter._index.d == DIM + 1
        adapter._embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_later_batch_keeps_first_batch_on_disk(self, adapter):
        import faiss

        _seed_db(adapter, list(range(65)))
        adapter._index = adapter._create_index(DIM)

        async def embed(texts):
            if len(texts) == 1:
                raise asyncio.CancelledError()
            return [[0.1] * DIM for _ in texts]

        adapter._embed = AsyncMock(side_effect=embed)
        with pytest.raises(asyncio.CancelledError):
            await adapter._reconcile_index_with_db()
        loaded = faiss.read_index(str(adapter._persist_dir / "index.faiss"))
        assert loaded.ntotal == 64
        assert adapter._count_db() == 65

    @pytest.mark.asyncio
    async def test_partial_migration_preserves_full_backup(self, adapter):
        from types import SimpleNamespace
        import json

        _seed_db(adapter, [1, 2, 3])
        stats = SimpleNamespace(
            total_count=3, imported_count=1, skipped_count=0, error_count=2
        )
        with patch(
            "iris_memory.l2_memory.io.MemoryImporter.import_from_file",
            new=AsyncMock(return_value=stats),
        ):
            assert not await adapter._migrate_on_model_change("new-model", DIM)
        backups = list(
            (adapter._persist_dir.parent / "migration_backup").glob(
                "*_migration_backup.json"
            )
        )
        assert len(backups) == 1
        data = json.loads(backups[0].read_text())
        assert len(data["entries"]) == 3

    @pytest.mark.asyncio
    async def test_failed_final_save_keeps_migration_backup(self, adapter):
        _seed_db(adapter, [1, 2])
        with patch.object(
            adapter, "_write_index_atomic", side_effect=OSError("disk full")
        ):
            assert not await adapter._migrate_on_model_change("new-model", DIM)
        assert list(
            (adapter._persist_dir.parent / "migration_backup").glob(
                "*_migration_backup.json"
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,args,expected",
    [
        ("update_access", ("m1",), False),
        ("batch_update_access", (["m1"],), 0),
        ("delete_by_group", ("g1",), 0),
        ("delete_by_user", ("u1",), 0),
    ],
)
async def test_shutdown_connection_race_returns_failure(
    adapter, operation, args, expected
):
    """可用标志尚未重置但连接已关闭时，操作应正常退回失败结果。"""
    adapter._index = adapter._create_index(DIM)
    adapter._db.close()
    adapter._db = None
    assert await getattr(adapter, operation)(*args) == expected
