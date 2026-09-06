"""
Iris Chat Memory - L2 记忆库 FAISS + SQLite 适配器

实现 L2 记忆库的存储和检索功能，支持：
- FAISS 向量索引（IndexFlatIP，余弦相似度）
- SQLite 元数据存储（文档、元数据、ID 映射）
- 群聊隔离检索
- 人格隔离（独立目录）
- 去重检查
- 超时保护
- 自动降级
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, cast
from pathlib import Path
import uuid

import numpy as np

from iris_memory.core import Component, get_logger, InitMode
from iris_memory.config import get_config
from iris_memory.utils import atomic_write_json
from .models import MemoryEntry, MemorySearchResult

logger = get_logger("l2_memory.adapter")


class EmbeddingRetryError(RuntimeError):
    """索引修复重试耗尽；保留已修复部分供下次继续。"""


SUPPORTED_EMBEDDING_MODELS = {
    "BAAI/bge-small-zh-v1.5": {
        "dimensions": 512,
        "size_mb": 96,
        "description": "BGE v1.5 中文小模型（默认推荐）",
        "language": "zh",
    },
    "moka-ai/m3e-small": {
        "dimensions": 512,
        "size_mb": 96,
        "description": "M3E 中文小模型，s2s 能力强",
        "language": "zh",
    },
    "moka-ai/m3e-base": {
        "dimensions": 768,
        "size_mb": 409,
        "description": "M3E 中英双语，s2p 检索能力强",
        "language": "zh+en",
    },
    "BAAI/bge-base-zh-v1.5": {
        "dimensions": 768,
        "size_mb": 409,
        "description": "BGE v1.5 中文 base 模型，精度更高",
        "language": "zh",
    },
    "shibing624/text2vec-base-chinese": {
        "dimensions": 768,
        "size_mb": 409,
        "description": "text2vec 中文模型，语义匹配强",
        "language": "zh",
    },
    "all-MiniLM-L6-v2": {
        "dimensions": 384,
        "size_mb": 80,
        "description": "英文默认模型，中文效果差",
        "language": "en",
    },
}


class L2MemoryAdapter(Component):
    """L2 记忆库适配器

    使用 FAISS + SQLite 存储和检索记忆向量。

    Attributes:
        _index: FAISS 向量索引
        _db: SQLite 连接
        _embedding_provider: AstrBot Embedding Provider 实例
        _local_model: sentence-transformers 模型实例
        _persist_dir: 数据持久化目录
        _persona_id: 当前人格 ID
        _free_list: 已删除的可复用 FAISS 槽位
        _dirty: 索引是否需要保存
    """

    def __init__(self, persona_id: str = "default", context=None):
        super().__init__()
        # FAISS 的 numpy 方法在运行时替换 SWIG 原生签名。
        self._index: Any = None
        self._db: Optional[sqlite3.Connection] = None
        self._embedding_provider = None
        self._local_model = None
        self._actual_embedding_model: str = ""
        self._embedding_dimensions: int = 0
        self._embedding_source: str = "provider"
        self._persist_dir: Optional[Path] = None
        self._persona_id = persona_id
        self._context = context
        self._free_list: List[int] = []
        self._dirty = False
        self._pending_writes = 0
        self._checkpointing = False
        self._checkpoint_task: Optional[asyncio.Task] = None
        self._audit_task: Optional[asyncio.Task] = None
        self._reconcile_lock = asyncio.Lock()
        self._embed_retry_backoff = 2.0
        self._lock = threading.RLock()
        self._init_mode = InitMode.BACKGROUND
        self._last_recovery_attempt: float = 0.0
        self._recovery_cooldown: float = 60.0

    @property
    def name(self) -> str:
        return "l2_memory"

    # ========================================================================
    # 初始化与关闭
    # ========================================================================

    async def initialize(self) -> None:
        config = get_config()

        if not config.get("l2_memory.enable"):
            logger.info("L2 记忆库未启用，跳过初始化")
            self._is_available = False
            self._init_error = "L2 记忆库未启用"
            return

        try:
            import faiss  # noqa: F401 -- availability check
        except ImportError:
            raise ImportError(
                "faiss-cpu 未安装。请在 AstrBot 管理面板的插件依赖中添加 faiss-cpu，"
                "或在插件目录执行 pip install faiss-cpu"
            )

        try:
            self._persist_dir = config.data_dir / "faiss" / f"memory_{self._persona_id}"
            self._persist_dir.mkdir(parents=True, exist_ok=True)

            # 初始化嵌入源
            self._embedding_source = cast(
                str, config.get("l2_memory.embedding_source", "provider")
            )

            try:
                if self._embedding_source == "provider":
                    await self._init_provider_embedding(config)
                else:
                    await self._init_local_embedding(config)
            except ImportError as emb_err:
                logger.error(
                    f"嵌入模型加载失败，L2 记忆库将不可用：{emb_err}\n"
                    f"  → 解决方法：在插件配置中将「嵌入模型来源」切换为 Provider，"
                    f"并在 AstrBot「模型」页面配置一个 Embedding 类型的 Provider"
                )
                self._is_available = False
                self._init_error = str(emb_err)
                return
            except Exception as emb_err:
                logger.error(
                    f"嵌入模型加载失败，L2 记忆库将不可用：{emb_err}\n"
                    f"  → 当前来源：{self._embedding_source}"
                    f"{'，请检查 Embedding Provider 是否已配置并可用' if self._embedding_source == 'provider' else ''}",
                    exc_info=True,
                )
                self._is_available = False
                self._init_error = f"嵌入模型加载失败：{emb_err}"
                return

            if not self._actual_embedding_model:
                self._actual_embedding_model = "unknown"

            # Provider 声明的维度可能只是 AstrBot 配置值，并不一定会随请求发送给
            # 实际服务（例如 NVIDIA NIM 会返回模型原生维度）。始终以一次真实请求
            # 的输出为准，避免用错误维度创建 FAISS 索引。
            declared_dim = self._embedding_dimensions
            try:
                actual_dim = await self._detect_dimensions()
            except Exception as probe_err:
                # 探测失败多为暂时性网络故障而非永久配置问题；错误信息带上
                # "Provider" 使 _try_recover() 能在后续调用中自动重试初始化，
                # 避免 L2 记忆库因启动瞬断而永久不可用。
                raise RuntimeError(
                    f"Embedding Provider 维度探测失败：{probe_err}"
                ) from probe_err
            if declared_dim and declared_dim != actual_dim:
                logger.warning(
                    f"Embedding 源声明维度({declared_dim})与实际输出维度"
                    f"({actual_dim})不一致，以实际输出为准"
                )
            self._embedding_dimensions = actual_dim

            # 加载或创建索引
            meta = self._load_meta()
            stored_model = meta.get("embedding_model", "")
            stored_dim = meta.get("embedding_dimensions", 0)

            needs_migration = False
            if stored_model and stored_model != self._actual_embedding_model:
                needs_migration = True
                logger.warning(
                    f"嵌入模型已变更：{stored_model} -> {self._actual_embedding_model}，"
                    f"开始自动迁移..."
                )
            elif (
                self._embedding_dimensions
                and stored_dim
                and self._embedding_dimensions != stored_dim
            ):
                needs_migration = True
                logger.warning(
                    f"嵌入维度已变更：{stored_dim} -> {self._embedding_dimensions}，"
                    f"开始自动迁移..."
                )

            if needs_migration:
                ok = await self._migrate_on_model_change(
                    self._actual_embedding_model, self._embedding_dimensions
                )
                if not ok:
                    logger.error(
                        "自动迁移失败，L2 记忆库不可用。\n"
                        "  → 解决方法：检查 Embedding Provider 配置是否变更，"
                        "或手动删除 data/faiss 目录后重启插件重建记忆库"
                    )
                    self._is_available = False
                    self._init_error = "自动迁移失败，旧数据与新嵌入模型不兼容"
                    return
            else:
                # 加载已有索引和数据
                await self._load_existing(stored_dim)

                # 历史 meta 可能没有维度，或其记录与磁盘索引不一致。直接校验
                # FAISS index.d，避免 _load_existing() 以旧索引维度覆盖试算结果。
                loaded_dim = int(self._index.d) if self._index is not None else 0
                if loaded_dim and loaded_dim != actual_dim:
                    logger.warning(
                        f"FAISS 索引维度({loaded_dim})与实际输出维度"
                        f"({actual_dim})不一致，开始自动迁移..."
                    )
                    ok = await self._migrate_on_model_change(
                        self._actual_embedding_model, actual_dim
                    )
                    if not ok:
                        logger.error(
                            "自动迁移失败，L2 记忆库不可用。\n"
                            "  → 解决方法：检查 Embedding Provider 配置是否变更，"
                            "或手动删除 data/faiss 目录后重启插件重建记忆库"
                        )
                        self._is_available = False
                        self._init_error = "自动迁移失败，旧数据与新嵌入模型不兼容"
                        return
                # 补全元数据
                elif not stored_model or (
                    not stored_dim and self._embedding_dimensions
                ):
                    self._save_meta()

            self._is_available = True
            self._init_error = ""
            self._start_index_audit()

            count = self._count_db()
            logger.info(
                f"L2 记忆库初始化成功，persona: {self._persona_id}，"
                f"嵌入来源: {self._embedding_source}，"
                f"嵌入模型: {self._actual_embedding_model}，"
                f"维度: {self._embedding_dimensions}，"
                f"当前条目数: {count}"
            )

        except ImportError as e:
            logger.error(
                f"L2 记忆库初始化失败：{e}\n"
                f"  → 解决方法：请在 AstrBot 管理面板的插件依赖中安装缺少的依赖包"
            )
            self._is_available = False
            self._init_error = str(e)
        except Exception as e:
            logger.error(f"L2 记忆库初始化失败：{e}", exc_info=True)
            self._is_available = False
            self._init_error = f"L2 记忆库初始化失败：{e}"

    async def _load_existing(self, stored_dim: int) -> None:
        """打开存储；实际维度不一致时先交由初始化迁移，避免错误维度补写。"""
        await asyncio.to_thread(self._open_storage, stored_dim)
        if self._index is not None and self._index.d == self._embedding_dimensions:
            await self._reconcile_index_with_db()

    def _open_storage(self, stored_dim: int) -> None:
        """同步打开 SQLite 与 FAISS 存储；索引损坏时降级为空索引待对账重建。"""
        import faiss

        assert self._persist_dir is not None
        db_path = self._persist_dir / "metadata.db"
        self._db = self._open_db(db_path)

        index_path = self._persist_dir / "index.faiss"
        if index_path.exists() and self._count_db() > 0:
            try:
                self._index = faiss.read_index(str(index_path))
            except Exception as e:
                # 非原子写入中断（如 checkpoint 途中断电）会留下截断文件。
                # SQLite 元数据是事实源且完好，改名保留现场后走对账重建。
                corrupt_path = index_path.with_name(index_path.name + ".corrupt")
                logger.error(
                    f"FAISS 索引读取失败（疑似写入中断损坏）：{e}，"
                    f"已将损坏文件移至 {corrupt_path.name}，将从 SQLite 重建向量"
                )
                try:
                    index_path.replace(corrupt_path)
                except OSError:
                    pass
                self._index = self._create_index(self._embedding_dimensions)
            else:
                actual_dim = self._index.d
                if stored_dim and actual_dim != stored_dim:
                    logger.warning(
                        f"FAISS 索引维度({actual_dim})与元数据记录({stored_dim})不一致，"
                        "将按实际 Embedding 输出校验"
                    )
        else:
            self._index = self._create_index(self._embedding_dimensions)

        # 加载 free-list
        meta = self._load_meta()
        self._free_list = meta.get("free_list", [])

    def _index_ids_unlocked(self) -> set:
        """读取当前索引的全部向量 ID（需持锁调用）。"""
        import faiss

        if self._index is None or self._index.ntotal == 0:
            return set()
        try:
            ids = faiss.vector_to_array(self._index.id_map)
        except AttributeError:
            # 非 IndexIDMap 包装的理论形态：退化为计数对账
            return {i for i in range(self._index.ntotal)}
        return {int(i) for i in ids}

    async def _reconcile_index_with_db(self) -> None:
        """对账：以 SQLite 为唯一事实源，增量修复索引与 DB 的漂移。

        SQLite 走 WAL 即时持久，FAISS 索引依赖 checkpoint 落盘；checkpoint
        间隔内进程崩溃/被杀（桌面端直接关闭窗口不走 terminate）会使两者
        脱同步（DB 有行、索引缺向量 → 纯向量检索永久漏召回；反之留下
        已删条目的脏向量）。修复策略为增量：

        - 脏向量（索引有、DB 无）：直接 ``remove_ids``，无需重嵌入；
        - 缺失向量（DB 有、索引无）：仅重嵌入缺失行（带重试退避）补回，
          批间崩溃的进度会在下次对账中续跑；
        - 每轮修复结果原子落盘。

        修复失败保持已修复部分并记录错误，由周期审计
        （``_index_audit_loop``）在运行期自动重试——否则脱同步会一直
        持续到下一次重启且依赖当次启动嵌入全部成功，用户只能靠手动
        「编辑保存」逐条恢复向量。
        """
        async with self._reconcile_lock:
            await self._reconcile_index_with_db_locked()

    async def _reconcile_index_with_db_locked(self) -> None:
        try:
            await self._do_reconcile_index_with_db()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 对账自身的意外异常不得冒泡到 initialize() 把整个 L2 置为
            # 不可用（数据仍在 SQLite，只是暂时漏召回），留待审计重试
            logger.error(f"索引对账异常，本轮放弃待周期审计重试：{e}", exc_info=True)

    async def _do_reconcile_index_with_db(self) -> None:
        with self._lock:
            if self._db is None or self._index is None:
                return
            db_ids = {
                int(row[0])
                for row in self._db.execute("SELECT faiss_idx FROM memories")
            }
            index_ids = self._index_ids_unlocked()

        if db_ids == index_ids:
            with self._lock:
                stale_free = bool(set(self._free_list) & db_ids)
            if stale_free:
                await asyncio.to_thread(self._persist_repaired_index_locked, db_ids)
            return

        missing = db_ids - index_ids
        extra = index_ids - db_ids
        logger.warning(
            f"FAISS 索引与 SQLite 脱同步（缺 {len(missing)} 条向量、"
            f"多 {len(extra)} 条脏向量），开始增量修复"
        )

        # 1) 脏向量：对应 DB 行已删除，直接从索引摘除
        if extra:
            await asyncio.to_thread(self._remove_index_ids_locked, sorted(extra))

        # 2) 缺失向量：仅重嵌入缺失行
        repaired = 0
        embed_failed = False
        if missing:
            rows = await asyncio.to_thread(self._fetch_reconcile_rows, missing)
            if rows is None:
                return
            try:
                repaired = await self._reembed_missing_rows(rows)
            except EmbeddingRetryError as e:
                embed_failed = True
                logger.error(
                    f"缺失向量重嵌入失败（{len(missing) - repaired}/"
                    f"{len(missing)} 条未恢复）：{e}。索引将在周期审计中自动重试，"
                    f"重试期间这些记忆无法被向量检索命中"
                )

        # 3) 落盘修复结果（部分修复也保存，收敛崩溃丢失窗口）
        await asyncio.to_thread(self._persist_repaired_index_locked, db_ids)

        if embed_failed:
            return
        logger.info(
            f"索引对账修复完成：补回 {repaired} 条缺失向量、摘除 {len(extra)} 条脏向量"
        )

    async def _reembed_missing_rows(self, rows: List[tuple]) -> int:
        """分批重嵌入缺失行并补回索引，返回成功补回的条数。

        批间实时复查索引 ID：与并发写入路径（add/编辑保存）竞争时，
        已被补上的槽位直接跳过，避免同一 ID 重复写入 id_map。
        """
        batch = 64
        repaired = 0
        for start in range(0, len(rows), batch):
            chunk = rows[start : start + batch]
            vectors = await self._embed_with_retry([content for _, content in chunk])
            if vectors is None:
                raise EmbeddingRetryError(
                    f"批次 {start // batch} 重试耗尽（含 {len(chunk)} 条）"
                )
            if len(vectors) != len(chunk):
                raise EmbeddingRetryError(
                    f"嵌入返回数量不匹配：期望 {len(chunk)}，实际 {len(vectors)}"
                )
            repaired += await asyncio.to_thread(
                self._add_missing_vectors_locked, chunk, vectors
            )
            await asyncio.to_thread(self._persist_repaired_index_locked, set())
            if start and start % (batch * 10) == 0:
                logger.info(f"索引修复进行中：{start}/{len(rows)} 条")
        return repaired

    async def _embed_with_retry(
        self, texts: list[str], max_retries: int = 3
    ) -> Optional[list[list[float]]]:
        """带指数退避的嵌入调用；重试耗尽返回 None（由调用方决定后续）。"""
        delay = self._embed_retry_backoff
        for attempt in range(1, max_retries + 1):
            try:
                return await self._embed(texts)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt == max_retries:
                    logger.error(f"嵌入调用重试 {max_retries} 次后仍失败：{e}")
                    return None
                if delay > 0:
                    logger.warning(
                        f"嵌入失败（{attempt}/{max_retries}），{delay:.0f}s 后重试：{e}"
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 16.0)
                else:
                    logger.warning(
                        f"嵌入失败（{attempt}/{max_retries}），立即重试：{e}"
                    )
        return None

    def _remove_index_ids_locked(self, ids: List[int]) -> None:
        """持锁从索引摘除脏向量（供线程池执行）。"""
        with self._lock:
            if self._index is None or not ids:
                return
            if self._db is None:
                return
            live_ids = {
                row[0] for row in self._db.execute("SELECT faiss_idx FROM memories")
            }
            stale_ids = [idx for idx in ids if idx not in live_ids]
            if stale_ids:
                self._index.remove_ids(np.array(stale_ids, dtype=np.int64))
                self._dirty = True

    def _add_missing_vectors_locked(
        self, rows: List[tuple], vectors: list[list[float]]
    ) -> int:
        """持锁把重嵌入向量补回索引（供线程池执行），跳过期间已被补上的槽位。"""
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            if self._index is None or self._db is None:
                return 0
            present = self._index_ids_unlocked()
            added = 0
            for (faiss_idx, _content), vector in zip(rows, vectors):
                if faiss_idx in present:
                    continue
                current = self._db.execute(
                    "SELECT content FROM memories WHERE faiss_idx = ?", (faiss_idx,)
                ).fetchone()
                if current is None or current[0] != _content:
                    continue
                matrix = np.array([vector], dtype=np.float32)
                self._validate_index_dimensions_unlocked(matrix, "恢复记忆")
                cast(Any, self._index).add_with_ids(
                    matrix,
                    np.array([faiss_idx], dtype=np.int64),
                )
                present.add(faiss_idx)
                self._dirty = True
                added += 1
            return added

    def _persist_repaired_index_locked(self, db_ids: set) -> None:
        """持锁原子落盘修复后的索引与元数据（供线程池执行）。"""
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            if self._index is None or self._db is None or self._persist_dir is None:
                return
            # meta 落后于索引时 free-list 可能残留已占用槽位，过滤避免
            # 后续写入复用活动槽位覆盖既有记忆
            db_ids = {
                row[0] for row in self._db.execute("SELECT faiss_idx FROM memories")
            }
            self._free_list = sorted({i for i in self._free_list if i not in db_ids})
            self._write_index_atomic(self._index, self._persist_dir / "index.faiss")
            self._save_meta()
            self._dirty = False

    def _fetch_reconcile_rows(
        self, missing: Optional[set] = None
    ) -> Optional[List[tuple]]:
        """持锁读取对账所需的 (faiss_idx, content) 行。

        missing 为 None 时取全量；否则仅取缺失 ID（分片规避 SQLite
        绑定变量上限）。供线程池执行。
        """
        with self._lock:
            if self._db is None:
                return None
            if missing is None:
                return self._db.execute(
                    "SELECT faiss_idx, content FROM memories ORDER BY faiss_idx"
                ).fetchall()
            ids = sorted(missing)
            if not ids:
                return []
            rows: List[tuple] = []
            # SQLite 绑定变量上限旧版为 999，取 500 留余量
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                placeholders = ",".join("?" * len(chunk))
                rows.extend(
                    self._db.execute(
                        f"SELECT faiss_idx, content FROM memories "
                        f"WHERE faiss_idx IN ({placeholders}) "
                        f"ORDER BY faiss_idx",
                        chunk,
                    ).fetchall()
                )
            return rows

    @staticmethod
    def _write_index_atomic(index, path: Path) -> None:
        """原子化写 FAISS 索引：先写同目录临时文件再 os.replace 覆盖。

        faiss.write_index 直接覆盖目标文件，写入中途崩溃会留下截断文件；
        与 atomic_write_json 相同的 tmp + replace 模式保证目标文件
        任意时刻都是完整内容。
        """
        import faiss

        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            faiss.write_index(index, str(tmp_path))
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _index_audit_interval(self) -> float:
        """读取审计间隔（秒）；0 或配置不可用时禁用。"""
        try:
            return float(
                cast(Any, get_config().get("l2_index_audit_interval_sec")) or 0
            )
        except Exception:
            return 0.0

    def _start_index_audit(self) -> None:
        """启动周期索引审计任务（幂等；无事件循环或间隔为 0 时跳过）。

        启动对账只在 initialize() 时执行一次，任何一次重嵌入失败都会让
        脱同步的索引保持残缺——直到下次重启且当次嵌入全部成功为止，期间
        记忆检索持续漏召回、只能靠手动编辑保存逐条恢复。周期审计把对账
        变成运行期持续自愈：每轮做一次廉价的 ID 集比较，发现漂移才修复。
        """
        if self._audit_task is not None and not self._audit_task.done():
            return
        interval = self._index_audit_interval()
        if interval <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
            self._audit_task = loop.create_task(
                self._index_audit_loop(interval), name="l2_index_audit"
            )
        except RuntimeError:
            # 无运行中的事件循环（同步测试上下文）
            self._audit_task = None

    async def _index_audit_loop(self, interval: float) -> None:
        """周期对账循环：首轮延后 min(30s, interval) 给启动期 Provider 预热。"""
        delay = min(30.0, interval)
        while True:
            await asyncio.sleep(delay)
            delay = interval
            try:
                if not self._is_available or self._db is None:
                    continue
                await self._reconcile_index_with_db()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"L2 索引周期审计异常（下轮重试）：{e}")

    async def _shutdown_audit_task(self) -> None:
        """取消并等待审计任务退出。"""
        task = self._audit_task
        self._audit_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"L2 索引审计任务退出异常（忽略）：{e}")

    def _shutdown_locked(self) -> None:
        """同步关闭流程（供线程池执行）。"""
        with self._lock:
            if (
                self._dirty
                and self._index is not None
                and self._persist_dir is not None
            ):
                try:
                    self._write_index_atomic(
                        self._index, self._persist_dir / "index.faiss"
                    )
                    self._save_meta()
                    logger.info("FAISS 索引已保存")
                except Exception as e:
                    logger.error(f"保存 FAISS 索引失败：{e}")

            if self._db:
                try:
                    self._db.close()
                except Exception:
                    pass

            self._index = None
            self._db = None
            self._embedding_provider = None
            self._local_model = None
            self._actual_embedding_model = ""
            self._embedding_source = "provider"
            self._reset_state()

    def _on_checkpoint_done(self, task: "asyncio.Task") -> None:
        """checkpoint 任务收尾：复位标志、清理引用并上报异常。"""
        if self._checkpoint_task is task:
            self._checkpointing = False
            self._checkpoint_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"FAISS checkpoint 任务异常：{exc}")

    async def _detect_dimensions(self) -> int:
        """通过试算检测嵌入维度"""
        vecs = await self._embed(["dimension probe"])
        if len(vecs) != 1 or not vecs[0]:
            raise RuntimeError("Embedding Provider 未返回有效的测试向量")
        return len(vecs[0])

    def _create_index(self, dim: int):
        """创建 FAISS IndexIDMap(IndexFlatIP) 索引"""
        import faiss

        base = faiss.IndexFlatIP(dim)
        return faiss.IndexIDMap(base)

    def _open_db(self, path: Path) -> sqlite3.Connection:
        """打开 SQLite 数据库并确保表结构"""
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                faiss_idx INTEGER PRIMARY KEY,
                memory_id TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                group_id TEXT,
                user_id TEXT,
                timestamp TEXT,
                kg_processed INTEGER DEFAULT 0,
                persona_id TEXT NOT NULL DEFAULT 'default'
            )
        """)
        # 向后兼容：旧库无 persona_id 列时补列（默认 'default'）
        cols = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
        if "persona_id" not in cols:
            db.execute(
                "ALTER TABLE memories ADD COLUMN persona_id TEXT NOT NULL DEFAULT 'default'"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_group_id ON memories(group_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_kg_processed ON memories(kg_processed)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_persona_id ON memories(persona_id)"
        )
        db.commit()
        return db

    def _load_meta(self) -> Dict[str, Any]:
        """加载 index_meta.json"""
        if self._persist_dir is None:
            raise RuntimeError("L2 持久化目录尚未初始化")
        meta_path = self._persist_dir / "index_meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_meta(self) -> None:
        """保存 index_meta.json"""
        meta = {
            "version": 1,
            "embedding_model": self._actual_embedding_model,
            "embedding_dimensions": self._embedding_dimensions,
            "persona_id": self._persona_id,
            "free_list": self._free_list,
        }
        if self._persist_dir is None:
            raise RuntimeError("L2 持久化目录尚未初始化")
        meta_path = self._persist_dir / "index_meta.json"
        atomic_write_json(meta_path, meta, ensure_ascii=False, indent=2)

    async def shutdown(self) -> None:
        """关闭数据库连接并保存 FAISS 索引

        获取锁后再操作，确保不会有其他线程在使用 FAISS 或 SQLite。
        全量落盘可能耗时数秒，整体下放线程池避免阻塞事件循环。
        """
        await self._shutdown_audit_task()
        if self._checkpoint_task is not None:
            await asyncio.gather(self._checkpoint_task, return_exceptions=True)
        async with self._reconcile_lock:
            await asyncio.to_thread(self._shutdown_locked)
        logger.info("L2 记忆库已关闭")

    def _mark_dirty(self) -> None:
        """标记索引已修改；累计写入达到阈值时安排异步落盘。

        FAISS 索引此前仅在 shutdown 时整体持久化，运行期进程崩溃会丢失自启动
        以来的全部向量增量（而 SQLite 走 WAL、commit 即耐久），导致重启后
        SQLite 有记录但 FAISS 缺向量、索引与元数据不一致。定期 checkpoint 把
        丢失窗口收敛到最近若干次写入。可在持锁或非持锁上下文中调用。
        """
        self._dirty = True
        self._pending_writes += 1
        try:
            threshold = int(cast(int, get_config().get("l2_checkpoint_writes")) or 0)
        except Exception:
            # config 未就绪（初始化早期或测试环境）：仅标记脏，跳过 checkpoint
            return
        if threshold <= 0 or self._pending_writes < threshold:
            return
        if self._checkpointing:
            return
        self._pending_writes = 0
        self._checkpointing = True
        try:
            # 事件循环对 task 只持弱引用：必须保存强引用，并由当前任务的
            # done callback 统一复位 _checkpointing，避免旧任务回调覆盖新任务。
            loop = asyncio.get_running_loop()
            task = loop.create_task(asyncio.to_thread(self._checkpoint_locked))
            self._checkpoint_task = task
            task.add_done_callback(self._on_checkpoint_done)
        except RuntimeError:
            # 无运行中的事件循环（如同步测试上下文），退化为等待下次触发
            self._checkpointing = False

    def _checkpoint_locked(self) -> None:
        """持锁将脏 FAISS 索引落盘（供线程池执行）。"""
        try:
            with self._lock:
                if (
                    self._index is not None
                    and self._persist_dir is not None
                    and self._dirty
                ):
                    self._write_index_atomic(
                        self._index, self._persist_dir / "index.faiss"
                    )
                    self._save_meta()
                    self._dirty = False
                    logger.debug("FAISS 索引已 checkpoint")
        except Exception as e:
            logger.error(f"FAISS checkpoint 失败：{e}")

    # ========================================================================
    # 嵌入源初始化
    # ========================================================================

    async def _init_provider_embedding(self, config) -> None:
        """初始化 AstrBot Embedding Provider 嵌入源

        带指数退避重试：AstrBot 启动时 Embedding Provider 可能尚未注册，
        后台初始化期间等待 Provider 就绪。
        """
        import asyncio

        provider_id = config.get("l2_memory.embedding_provider", "")
        max_retries = 5
        base_interval = 2.0

        provider = None
        for attempt in range(1, max_retries + 1):
            provider = None

            if provider_id:
                provider = self._get_embedding_provider_by_id(provider_id)
                if not provider and attempt == 1:
                    logger.warning(
                        f"指定的 Embedding Provider '{provider_id}' 不可用，"
                        f"请检查 ID 是否正确"
                    )

            if not provider:
                provider = self._get_first_embedding_provider()

            if provider:
                break

            if attempt < max_retries:
                delay = min(base_interval * (2 ** (attempt - 1)), 16.0)
                logger.info(
                    f"Embedding Provider 未就绪，"
                    f"{delay:.0f}s 后重试 ({attempt}/{max_retries})"
                )
                await asyncio.sleep(delay)
            else:
                raise ImportError(
                    "未找到可用的 AstrBot Embedding Provider\n"
                    "  → 建议：在 AstrBot「模型」页面添加 Embedding 类型的 Provider"
                )

        if provider is None:
            raise RuntimeError("Embedding Provider 不可用")
        model_name = getattr(provider, "model_name", None)
        if not model_name and hasattr(provider, "meta"):
            model_name = getattr(getattr(provider, "meta", None), "model_name", None)
        model_name = model_name or provider_id

        dim = 0
        try:
            dim = provider.get_dim()
        except Exception:
            pass

        logger.info(
            f"使用 AstrBot Embedding Provider: {provider_id}，"
            f"模型: {model_name}，维度: {dim}"
        )

        self._embedding_provider = provider
        self._actual_embedding_model = f"provider:{provider_id}/{model_name}"
        self._embedding_dimensions = int(dim)

    async def _init_local_embedding(self, config) -> None:
        """初始化本地 sentence-transformers 嵌入源"""
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers 未安装。"
                "请在 AstrBot 管理面板安装插件依赖，"
                "或将嵌入来源切换为 Provider 模式"
            )

        model_name = config.get("l2_memory.embedding_model", "BAAI/bge-small-zh-v1.5")
        model_info = SUPPORTED_EMBEDDING_MODELS.get(model_name)
        dim = model_info["dimensions"] if model_info else 0

        if model_info:
            logger.info(
                f"加载嵌入模型：{model_name} "
                f"(维度={model_info['dimensions']}, "
                f"大小≈{model_info['size_mb']}MB, "
                f"{model_info['description']})"
            )
        else:
            logger.info(f"加载自定义嵌入模型：{model_name}")

        import os

        loop = asyncio.get_running_loop()
        try:
            self._local_model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(model_name)
            )
        except Exception as first_err:
            logger.warning(
                f"加载嵌入模型 {model_name} 失败：{first_err}，尝试离线模式..."
            )
            old_offline = os.environ.get("HF_HUB_OFFLINE")
            try:
                os.environ["HF_HUB_OFFLINE"] = "1"
                self._local_model = await loop.run_in_executor(
                    None, lambda: SentenceTransformer(model_name)
                )
                logger.info(f"离线模式加载嵌入模型 {model_name} 成功")
            except Exception as offline_err:
                raise ImportError(
                    f"加载嵌入模型 {model_name} 失败"
                    f"（在线：{first_err}，离线：{offline_err}）。"
                    f"请确保模型已下载，或切换为 Provider 模式"
                )
            finally:
                if old_offline is not None:
                    os.environ["HF_HUB_OFFLINE"] = old_offline
                else:
                    os.environ.pop("HF_HUB_OFFLINE", None)

        # 如果没有预知维度，从模型推断
        if not dim:
            dim = self._local_model.get_sentence_embedding_dimension() or 0

        self._actual_embedding_model = model_name
        self._embedding_dimensions = int(dim)

    def _get_embedding_provider_by_id(self, provider_id: str):
        if self._context is None:
            return None
        try:
            if hasattr(self._context, "get_provider_by_id"):
                provider = self._context.get_provider_by_id(provider_id)
                if provider and hasattr(provider, "get_embeddings"):
                    return provider

            if hasattr(self._context, "provider_manager"):
                pm = self._context.provider_manager
                if hasattr(pm, "inst_map"):
                    p = pm.inst_map.get(provider_id)
                    if p and hasattr(p, "get_embeddings"):
                        return p
        except Exception as e:
            logger.debug(f"通过 ID 获取 Embedding Provider 失败: {e}")
        return None

    def _get_first_embedding_provider(self):
        if self._context is None:
            return None
        try:
            if hasattr(self._context, "provider_manager"):
                pm = self._context.provider_manager
                if hasattr(pm, "embedding_provider_insts"):
                    providers = pm.embedding_provider_insts
                    if providers:
                        return providers[0]
                if hasattr(pm, "inst_map"):
                    for pid, p in pm.inst_map.items():
                        if hasattr(p, "get_embeddings"):
                            return p
        except Exception as e:
            logger.debug(f"获取 Embedding Provider 失败: {e}")
        return None

    # ========================================================================
    # 懒加载恢复
    # ========================================================================

    async def _try_recover(self) -> bool:
        """尝试从初始化失败中恢复

        仅当失败原因是 Embedding Provider 未就绪时才重试，
        带冷却时间防止频繁重试。适用于 Provider 在启动后才注册的场景。
        """
        if self._is_available:
            return True

        if not self._init_error or "Provider" not in self._init_error:
            return False

        import time

        now = time.monotonic()
        if now - self._last_recovery_attempt < self._recovery_cooldown:
            return False

        self._last_recovery_attempt = now
        logger.info("L2 记忆库尝试恢复：重新初始化...")

        try:
            await self.initialize()
            if self._is_available:
                logger.info("L2 记忆库恢复成功")
                return True
        except Exception as e:
            logger.debug(f"L2 记忆库恢复失败：{e}")

        return False

    # ========================================================================
    # 嵌入计算
    # ========================================================================

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """计算文本嵌入向量并 L2 归一化"""
        if self._embedding_source == "provider" and self._embedding_provider:
            vectors = await self._embedding_provider.get_embeddings(texts)
        elif self._local_model:
            local_model = self._local_model
            loop = asyncio.get_event_loop()
            vectors = await loop.run_in_executor(
                None, lambda: local_model.encode(texts).tolist()
            )
        else:
            raise RuntimeError("没有可用的嵌入源")

        # L2 归一化，使内积 = 余弦相似度
        normalized = []
        for v in vectors:
            arr = np.array(v, dtype=np.float32)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            normalized.append(arr.tolist())

        return normalized

    def _validate_index_dimensions_unlocked(
        self, vectors: np.ndarray, operation: str
    ) -> None:
        """校验二维向量矩阵与当前 FAISS 索引的维度一致。

        调用方必须持有 ``_lock``，使索引不会在读取 ``d`` 后被并发替换。
        """
        if vectors.ndim != 2 or vectors.shape[0] == 0 or vectors.shape[1] == 0:
            raise ValueError(f"{operation}收到无效嵌入向量形状: {tuple(vectors.shape)}")
        if self._index is None:
            raise RuntimeError(f"{operation}时 FAISS 索引尚未初始化")

        actual_dim = int(vectors.shape[1])
        index_dim = int(self._index.d)
        if actual_dim != index_dim:
            raise ValueError(
                f"{operation}的嵌入向量维度与 FAISS 索引不一致："
                f"实际输出={actual_dim}，索引={index_dim}。"
                "请重启插件以按实际维度迁移索引"
            )

    # ========================================================================
    # 记忆存储
    # ========================================================================

    async def add_memory(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        skip_dedup: bool = False,
        persona_id: str = "default",
    ) -> Optional[str]:
        if not self._is_available:
            await self._try_recover()
        if not self._is_available:
            logger.warning("L2 记忆库不可用，跳过添加记忆")
            return None

        if metadata is None:
            metadata = {}

        if "timestamp" not in metadata:
            metadata["timestamp"] = datetime.now().isoformat()
        if "access_count" not in metadata:
            metadata["access_count"] = 0
        if "confidence" not in metadata:
            metadata["confidence"] = 0.5
        if "last_access_time" not in metadata:
            metadata["last_access_time"] = datetime.now().isoformat()

        try:
            # 计算嵌入（在锁外，嵌入计算是无状态的）
            vectors = await self._embed([content])
            vector_np = np.array([vectors[0]], dtype=np.float32)

            memory_id = f"mem_{uuid.uuid4().hex[:12]}"

            # 去重检查、FAISS 写入、SQLite 写入必须在同一把锁内完成，
            # 消除“检查通过 → 释放锁 → 另一并发写入通过检查 → 重新取锁写入”
            # 的 TOCTOU 竞态；同时避免原先 _check_similarity 与写入各 embed 一次的重复计算。
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                if self._index is None or self._db is None:
                    return None

                self._validate_index_dimensions_unlocked(vector_np, "添加记忆")

                if not skip_dedup:
                    existing_id = self._find_similar_unlocked(vector_np, persona_id)
                    if existing_id:
                        logger.debug(f"发现相似记忆，跳过存储：{content[:50]}...")
                        return existing_id

                # 分配 FAISS 槽位
                if self._free_list:
                    faiss_idx = self._free_list.pop(0)
                else:
                    # ntotal 是当前向量数量，不是最大 ID + 1。索引存在删除空洞且
                    # free-list 丢失时直接使用 ntotal 可能覆盖仍在使用的向量 ID。
                    row = self._db.execute(
                        "SELECT MAX(faiss_idx) FROM memories"
                    ).fetchone()
                    max_idx = row[0] if row and row[0] is not None else -1
                    faiss_idx = max_idx + 1

                # 添加到 FAISS
                self._index.add_with_ids(
                    vector_np,
                    np.array([faiss_idx], dtype=np.int64),
                )

                # 添加到 SQLite
                self._upsert_db_unlocked(
                    faiss_idx, memory_id, content, metadata, persona_id
                )

            self._mark_dirty()
            logger.debug(f"已添加记忆：{memory_id}")
            return memory_id

        except Exception as e:
            logger.error(f"添加记忆失败：{e}", exc_info=True)
            return None

    def _find_similar_unlocked(
        self, vector: np.ndarray, persona_id: str = "default"
    ) -> Optional[str]:
        """在已持有 ``_lock`` 的情况下查找相似记忆（供 add_memory 去重）。

        与写入操作处于同一临界区，保证“检查相似 → 写入”的原子性，
        避免并发写入相同内容时双双通过去重。去重限定在同一 persona 命名空间内。
        """
        if self._index is None or self._index.ntotal == 0 or self._db is None:
            return None

        similarity_threshold = float(
            cast(float, get_config().get("l2_similarity_threshold"))
        )
        search_k = min(self._index.ntotal, 64)
        scores, indices = self._index.search(vector, search_k)

        for score, faiss_idx in zip(scores[0], indices[0]):
            if faiss_idx < 0:
                continue
            if float(score) < similarity_threshold:
                break

            row = self._db.execute(
                "SELECT memory_id FROM memories WHERE faiss_idx = ? AND persona_id = ?",
                (int(faiss_idx), persona_id),
            ).fetchone()
            if row:
                return row[0]
        return None

    # ========================================================================
    # 记忆检索
    # ========================================================================

    async def retrieve(
        self,
        query: str,
        group_id: Optional[str] = None,
        top_k: int = 10,
        persona_id: str = "default",
    ) -> List[MemorySearchResult]:
        if not self._is_available:
            await self._try_recover()
        if not self._is_available:
            return []

        config = get_config()
        timeout_ms = cast(int, config.get("l2_timeout_ms"))
        timeout_sec = timeout_ms / 1000.0

        async def _embed_and_search():
            # embedding 经 provider 可能走外部 API，与 FAISS 检索一并纳入超时，
            # 避免 embedding provider 卡死时在 on_llm_request 会话锁内无限挂起
            vector = await self._embed([query])
            vector_np = np.array([vector[0]], dtype=np.float32)
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._search_with_vector(
                    vector_np, group_id, top_k, persona_id
                ),
            )

        try:
            return await asyncio.wait_for(_embed_and_search(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.warning(f"L2 记忆检索超时（{timeout_sec}s），跳过")
            return []
        except Exception as e:
            logger.error(f"L2 记忆检索失败：{e}", exc_info=True)
            return []

    def _search_with_vector(
        self,
        vector: np.ndarray,
        group_id: Optional[str],
        top_k: int,
        persona_id: str = "default",
    ) -> List[MemorySearchResult]:
        """在锁保护下执行 FAISS 搜索 + SQLite 查询

        调用方（retrieve/batch_retrieve）通过 run_in_executor 在线程池中
        调用此方法。使用 RLock 保证 FAISS 和 SQLite 操作的线程安全，
        同时 RLock 允许 _db_execute 等内部方法重入。
        """
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            if self._index is None or self._db is None:
                logger.debug("FAISS 索引或 DB 为 None，跳过检索")
                return []
            self._validate_index_dimensions_unlocked(vector, "检索记忆")
            if self._index.ntotal == 0:
                logger.debug("FAISS 索引为空（ntotal=0），跳过检索")
                return []

            if group_id:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM memories WHERE group_id = ? AND persona_id = ?",
                    (group_id, persona_id),
                ).fetchone()
                group_count = row[0]
                if group_count == 0:
                    logger.debug(
                        f"隔离过滤：group_id='{group_id}' persona='{persona_id}' 在 DB 中无记忆 "
                        f"(总计 {self._index.ntotal} 条)，跳过检索"
                    )
                    return []
                n_probe = min(max(group_count, top_k), self._index.ntotal)
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM memories WHERE persona_id = ?", (persona_id,)
                ).fetchone()
                persona_count = row[0]
                if persona_count == 0:
                    logger.debug(
                        f"隔离过滤：persona='{persona_id}' 在 DB 中无记忆，跳过检索"
                    )
                    return []
                n_probe = min(max(persona_count, top_k), self._index.ntotal)

            scores, indices = self._index.search(vector, n_probe)

            valid_count = sum(1 for idx in indices[0] if idx >= 0)
            top_score = float(scores[0][0]) if valid_count > 0 else 0.0
            logger.debug(
                f"FAISS 搜索：ntotal={self._index.ntotal}, n_probe={n_probe}, "
                f"有效结果={valid_count}, 最高分={top_score:.4f}"
            )

            db_miss = 0
            group_filtered = 0
            results = []
            for i in range(len(indices[0])):
                faiss_idx = int(indices[0][i])
                if faiss_idx < 0:
                    continue

                score = float(scores[0][i])
                row = self._db.execute(
                    "SELECT memory_id, content, metadata, group_id, persona_id FROM memories WHERE faiss_idx = ?",
                    (faiss_idx,),
                ).fetchone()

                if not row:
                    db_miss += 1
                    continue

                (
                    row_memory_id,
                    row_content,
                    row_metadata_json,
                    row_group_id,
                    row_persona_id,
                ) = row

                if row_persona_id != persona_id:
                    group_filtered += 1
                    continue

                if group_id and row_group_id != group_id:
                    group_filtered += 1
                    continue

                entry = MemoryEntry(
                    id=row_memory_id,
                    content=row_content,
                    metadata=json.loads(row_metadata_json),
                    persona_id=row_persona_id,
                )
                results.append(
                    MemorySearchResult(entry=entry, score=score, distance=1.0 - score)
                )

                if len(results) >= top_k:
                    break

            if not results:
                logger.debug(
                    f"FAISS 检索最终 0 条：db_miss={db_miss}, "
                    f"group_filtered={group_filtered}, group_id={group_id}"
                )

            return results

    def _batch_search_with_vectors(
        self,
        vector_matrix: np.ndarray,
        group_id: Optional[str],
        top_k: int,
        persona_id: str = "default",
    ) -> List[List[MemorySearchResult]]:
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            if self._index is None or self._db is None:
                return [[] for _ in range(len(vector_matrix))]
            self._validate_index_dimensions_unlocked(vector_matrix, "批量检索记忆")
            if self._index.ntotal == 0:
                return [[] for _ in range(len(vector_matrix))]

            if group_id:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM memories WHERE group_id = ? AND persona_id = ?",
                    (group_id, persona_id),
                ).fetchone()
                group_count = row[0]
                if group_count == 0:
                    return [[] for _ in range(len(vector_matrix))]
                n_probe = min(max(group_count, top_k), self._index.ntotal)
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM memories WHERE persona_id = ?", (persona_id,)
                ).fetchone()
                persona_count = row[0]
                if persona_count == 0:
                    return [[] for _ in range(len(vector_matrix))]
                n_probe = min(max(persona_count, top_k), self._index.ntotal)

            all_scores, all_indices = self._index.search(vector_matrix, n_probe)

            all_results: List[List[MemorySearchResult]] = []
            for q_idx in range(len(vector_matrix)):
                results: List[MemorySearchResult] = []
                for i in range(len(all_indices[q_idx])):
                    faiss_idx = int(all_indices[q_idx][i])
                    if faiss_idx < 0:
                        continue

                    score = float(all_scores[q_idx][i])
                    row = self._db.execute(
                        "SELECT memory_id, content, metadata, group_id, persona_id FROM memories WHERE faiss_idx = ?",
                        (faiss_idx,),
                    ).fetchone()

                    if not row:
                        continue

                    (
                        row_memory_id,
                        row_content,
                        row_metadata_json,
                        row_group_id,
                        row_persona_id,
                    ) = row

                    if row_persona_id != persona_id:
                        continue

                    if group_id and row_group_id != group_id:
                        continue

                    entry = MemoryEntry(
                        id=row_memory_id,
                        content=row_content,
                        metadata=json.loads(row_metadata_json),
                        persona_id=row_persona_id,
                    )
                    results.append(
                        MemorySearchResult(
                            entry=entry, score=score, distance=1.0 - score
                        )
                    )

                    if len(results) >= top_k:
                        break

                all_results.append(results)

            return all_results

    async def batch_retrieve(
        self,
        queries: List[str],
        group_id: Optional[str] = None,
        top_k: int = 10,
        persona_id: str = "default",
    ) -> List[List[MemorySearchResult]]:
        if not self._is_available or not queries:
            return [[] for _ in queries]

        config = get_config()
        base_timeout_ms = cast(int, config.get("l2_timeout_ms"))
        timeout_sec = base_timeout_ms / 1000.0 * max(1, len(queries) // 10 + 1)

        try:
            vectors = await self._embed(queries)
            vector_matrix = np.array(vectors, dtype=np.float32)

            loop = asyncio.get_event_loop()
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._batch_search_with_vectors(
                        vector_matrix, group_id, top_k, persona_id
                    ),
                ),
                timeout=timeout_sec,
            )
            return results
        except asyncio.TimeoutError:
            logger.warning(
                f"批量检索超时（{timeout_sec:.1f}s），跳过 {len(queries)} 条查询"
            )
            return [[] for _ in queries]
        except Exception as e:
            logger.error(f"批量检索失败：{e}", exc_info=True)
            return [[] for _ in queries]

    async def batch_retrieve_by_ids(
        self,
        memory_ids: List[str],
        group_id: Optional[str] = None,
        top_k: int = 10,
        persona_id: str = "default",
    ) -> List[List[MemorySearchResult]]:
        """按已存记忆 ID 批量检索近邻（零 embedding 调用）

        查询对象本身已在库中时（如梦境离线加工），直接通过
        ``faiss_idx`` 从 FAISS 索引 ``reconstruct`` 出已存向量进行检索，
        不再对文本重新计算 embedding。

        注意：依赖底层索引支持 reconstruct（当前 IndexFlatIP 支持，
        若未来更换为 IVF 等压缩索引需重新评估）。

        Args:
            memory_ids: 已在库中的记忆 ID 列表
            group_id: 群 ID 过滤（None 表示不过滤）
            top_k: 每条返回的近邻数
            persona_id: 人格 ID

        Returns:
            与 memory_ids 等长的结果列表；ID 不存在或向量缺失时对应位置为空列表
        """
        if not self._is_available or not memory_ids:
            return [[] for _ in memory_ids]

        config = get_config()
        base_timeout_ms = cast(int, config.get("l2_timeout_ms"))
        timeout_sec = base_timeout_ms / 1000.0 * max(1, len(memory_ids) // 10 + 1)

        try:
            loop = asyncio.get_event_loop()
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._batch_search_by_ids(
                        memory_ids, group_id, top_k, persona_id
                    ),
                ),
                timeout=timeout_sec,
            )
            return results
        except asyncio.TimeoutError:
            logger.warning(
                f"按 ID 批量检索超时（{timeout_sec:.1f}s），跳过 {len(memory_ids)} 条查询"
            )
            return [[] for _ in memory_ids]
        except Exception as e:
            logger.error(f"按 ID 批量检索失败：{e}", exc_info=True)
            return [[] for _ in memory_ids]

    def _batch_search_by_ids(
        self,
        memory_ids: List[str],
        group_id: Optional[str],
        top_k: int,
        persona_id: str,
    ) -> List[List[MemorySearchResult]]:
        """按 memory_id 取已存向量并检索（在 executor 中运行）

        ``_lock`` 为 RLock，此处持锁后调用 ``_batch_search_with_vectors``
        再次取锁是安全的。
        """
        results: List[List[MemorySearchResult]] = [[] for _ in memory_ids]

        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            if self._index is None or self._db is None:
                return results
            if self._index.ntotal == 0:
                return results

            placeholders = ",".join("?" for _ in memory_ids)
            rows = self._db.execute(
                f"SELECT memory_id, faiss_idx FROM memories "
                f"WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()
            faiss_by_id = {r[0]: r[1] for r in rows}

            vectors = []
            positions = []
            for pos, mid in enumerate(memory_ids):
                faiss_idx = faiss_by_id.get(mid)
                if faiss_idx is None:
                    continue
                try:
                    # faiss stub 中 reconstruct 声明了输出参数 recons，
                    # Python 绑定实际支持单参数调用并直接返回向量
                    vec = self._index.reconstruct(  # pyright: ignore[reportCallIssue]
                        int(faiss_idx)
                    )
                except RuntimeError:
                    # 槽位已被删除/索引未实现 reconstruct
                    continue
                vectors.append(vec)
                positions.append(pos)

            if not vectors:
                return results

            vector_matrix = np.array(vectors, dtype=np.float32)
            found = self._batch_search_with_vectors(
                vector_matrix, group_id, top_k, persona_id
            )
            for pos, res in zip(positions, found):
                results[pos] = res

        return results

    # ========================================================================
    # 访问更新
    # ========================================================================

    async def update_access(self, memory_id: str) -> bool:
        if not self._is_available:
            return False

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                row = self._db.execute(
                    "SELECT metadata FROM memories WHERE memory_id = ?", (memory_id,)
                ).fetchone()

                if not row:
                    logger.warning(f"记忆不存在：{memory_id}")
                    return False

                metadata = json.loads(row[0])
                metadata["access_count"] = metadata.get("access_count", 0) + 1
                metadata["last_access_time"] = datetime.now().isoformat()

                self._db.execute(
                    "UPDATE memories SET metadata = ? WHERE memory_id = ?",
                    (json.dumps(metadata, ensure_ascii=False), memory_id),
                )
                self._db.commit()
            logger.debug(f"记忆访问更新成功：{memory_id}")
            return True
        except Exception as e:
            logger.error(f"更新记忆访问失败：{e}", exc_info=True)
            return False

    async def batch_update_access(self, memory_ids: List[str]) -> int:
        """批量更新记忆访问计数

        使用单条 SQL 批量递增，避免逐条 SELECT+UPDATE 的 round trip。

        Args:
            memory_ids: 需要更新访问的记忆 ID 列表

        Returns:
            成功更新的数量
        """
        if not self._is_available or not memory_ids:
            return 0

        now = datetime.now().isoformat()

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                placeholders = ",".join("?" * len(memory_ids))
                cursor = self._db.execute(
                    f"""UPDATE memories
                        SET metadata = json_set(
                                json_set(metadata, '$.access_count',
                                    COALESCE(json_extract(metadata, '$.access_count'), 0) + 1),
                                '$.last_access_time', ?)
                        WHERE memory_id IN ({placeholders})""",
                    (now, *memory_ids),
                )
                self._db.commit()
                updated = cursor.rowcount

            logger.debug(f"批量更新记忆访问：{updated}/{len(memory_ids)}")
            return updated
        except Exception as e:
            logger.error(f"批量更新记忆访问失败：{e}", exc_info=True)
            return 0

    # ========================================================================
    # 内容与元数据更新
    # ========================================================================

    async def update_metadata(self, memory_id: str, metadata: Dict[str, Any]) -> bool:
        if not self._is_available or not memory_id:
            return False

        try:
            group_id = metadata.get("group_id")
            user_id = metadata.get("user_id")
            timestamp = metadata.get("timestamp")
            kg_processed = 1 if metadata.get("kg_processed") else 0

            self._db_write(
                "UPDATE memories SET metadata = ?, group_id = ?, user_id = ?, timestamp = ?, kg_processed = ? WHERE memory_id = ?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    group_id,
                    user_id,
                    timestamp,
                    kg_processed,
                    memory_id,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"更新元数据失败：{e}", exc_info=True)
            return False

    async def update_content(self, memory_id: str, new_content: str) -> bool:
        if not self._is_available or self._index is None or not memory_id:
            return False

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                row = self._db.execute(
                    "SELECT faiss_idx, metadata, persona_id FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()

                if not row:
                    logger.warning(f"记忆不存在：{memory_id}")
                    return False

                faiss_idx, metadata_json, persona_id = row
                metadata = json.loads(metadata_json)
                metadata["timestamp"] = datetime.now().isoformat()

            # 重新计算嵌入（在锁外，嵌入计算是无状态的）
            vectors = await self._embed([new_content])
            new_vector = np.array([vectors[0]], dtype=np.float32)

            # FAISS + SQLite 操作在同一个锁内完成
            with self._lock:
                # 重新校验 faiss_idx 归属：嵌入计算在锁外完成，期间该记忆可能被
                # delete_entries 删除且其槽位被 add_memory 经 free-list 复用。
                # 若直接写回旧 faiss_idx，会误删并覆盖复用方的新向量，导致
                # FAISS 与 SQLite 错乱。校验不一致则放弃本次更新。
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                row_now = self._db.execute(
                    "SELECT faiss_idx FROM memories WHERE memory_id = ?",
                    (memory_id,),
                ).fetchone()
                if not row_now or row_now[0] != faiss_idx:
                    logger.warning(
                        f"记忆 {memory_id} 更新期间被并发删除或槽位变更，放弃更新"
                    )
                    return False

                self._validate_index_dimensions_unlocked(new_vector, "更新记忆")
                self._index.remove_ids(np.array([faiss_idx], dtype=np.int64))
                self._index.add_with_ids(
                    new_vector, np.array([faiss_idx], dtype=np.int64)
                )
                self._upsert_db_unlocked(
                    faiss_idx, memory_id, new_content, metadata, persona_id
                )

            self._mark_dirty()
            logger.info(f"已更新记忆内容：{memory_id}")
            return True
        except Exception as e:
            logger.error(f"更新记忆内容失败：{e}", exc_info=True)
            return False

    # ========================================================================
    # 删除操作
    # ========================================================================

    async def delete_entries(self, memory_ids: List[str]) -> bool:
        if not self._is_available or self._index is None or not memory_ids:
            return False

        try:
            with self._lock:
                # 获取对应的 faiss_idx
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                placeholders = ",".join("?" for _ in memory_ids)
                rows = self._db.execute(
                    f"SELECT faiss_idx FROM memories WHERE memory_id IN ({placeholders})",
                    memory_ids,
                ).fetchall()

                if not rows:
                    return False

                faiss_indices = [row[0] for row in rows]

                # 从 FAISS 移除
                self._index.remove_ids(np.array(faiss_indices, dtype=np.int64))

                # 加入 free-list
                self._free_list.extend(faiss_indices)
                self._free_list.sort()

                # 从 SQLite 删除
                self._db.execute(
                    f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                    memory_ids,
                )
                self._db.commit()

            self._mark_dirty()
            logger.info(f"已删除 {len(faiss_indices)} 条记忆")
            return True
        except Exception as e:
            logger.error(f"删除记忆失败：{e}", exc_info=True)
            return False

    async def delete_collection(self) -> bool:
        if self._persist_dir is None:
            return False

        try:
            # 取锁后再关闭/清理，避免与并发的检索/写入交错导致 SQLite 错误
            with self._lock:
                # 关闭数据库
                if self._db:
                    self._db.close()
                    self._db = None

                # 删除所有文件
                import shutil

                if self._persist_dir.exists():
                    shutil.rmtree(self._persist_dir)

                self._index = None
                self._free_list = []
                self._dirty = False
            logger.info(f"已删除 collection: {self._persona_id}")
            return True
        except Exception as e:
            logger.error(f"删除 collection 失败：{e}", exc_info=True)
            return False

    # ========================================================================
    # 容量管理
    # ========================================================================

    async def get_entry_by_id(
        self,
        memory_id: str,
        persona_id: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """按 memory_id 精确查询单条记忆。

        供 correct_memory 等需要按 ID 定位（而非语义检索）的场景使用，
        避免用 memory_id 作为语义查询导致误命中不相关记忆。若指定
        persona_id 则同时限定命名空间。
        """
        if not self._is_available or not self._db:
            return None

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                if persona_id is not None:
                    row = self._db.execute(
                        "SELECT memory_id, content, metadata, persona_id FROM memories WHERE memory_id = ? AND persona_id = ?",
                        (memory_id, persona_id),
                    ).fetchone()
                else:
                    row = self._db.execute(
                        "SELECT memory_id, content, metadata, persona_id FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()

            if not row:
                return None

            return MemoryEntry(
                id=row[0],
                content=row[1],
                metadata=json.loads(row[2]),
                persona_id=row[3],
            )
        except Exception as e:
            logger.error(f"按 ID 查询记忆失败：{e}")
            return None

    async def get_entry_count(self) -> int:
        if not self._is_available or not self._db:
            return 0
        try:
            return self._count_db()
        except Exception as e:
            logger.error(f"获取条目数失败：{e}")
            return 0

    async def get_all_entries(
        self, persona_id: Optional[str] = None
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            if persona_id is not None:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE persona_id = ?",
                    (persona_id,),
                ).fetchall()
            else:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories"
                ).fetchall()

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取所有条目失败：{e}")
            return []

    async def get_all_persona_ids(self) -> List[str]:
        """获取库中所有出现过的 persona_id（供 dream 按人格遍历）"""
        if not self._is_available or not self._db:
            return []
        try:
            rows = self._db_execute(
                "SELECT DISTINCT persona_id FROM memories"
            ).fetchall()
            return [row[0] for row in rows if row[0]]
        except Exception as e:
            logger.error(f"获取 persona 列表失败：{e}")
            return []

    async def get_entries_by_group(
        self, group_id: str, persona_id: str = "default"
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            rows = self._db_execute(
                "SELECT memory_id, content, metadata, persona_id FROM memories WHERE group_id = ? AND persona_id = ?",
                (group_id, persona_id),
            ).fetchall()

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取群聊条目失败：{e}")
            return []

    async def get_entries_by_user(
        self, user_id: str, persona_id: str = "default"
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            rows = self._db_execute(
                "SELECT memory_id, content, metadata, persona_id FROM memories WHERE user_id = ? AND persona_id = ?",
                (user_id, persona_id),
            ).fetchall()

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取用户条目失败：{e}")
            return []

    async def get_stats(self) -> Dict[str, Any]:
        if not self._is_available or not self._db:
            return {"total_count": 0, "group_count": 0}

        try:
            # 走线程池，避免在事件循环线程持同步锁、与 executor 中的长 FAISS
            # 检索争用而阻塞整个插件
            row = await asyncio.to_thread(
                self._db_fetchone,
                "SELECT COUNT(*), COUNT(DISTINCT group_id) FROM memories",
            )
            if row is None:
                return {"total_count": 0, "group_count": 0}
            return {"total_count": row[0], "group_count": row[1]}
        except Exception as e:
            logger.error(f"获取L2统计失败：{e}", exc_info=True)
            return {"total_count": 0, "group_count": 0}

    async def delete_by_group(self, group_id: str, persona_id: str = "default") -> int:
        if not self._is_available or self._index is None:
            return 0

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                rows = self._db.execute(
                    "SELECT faiss_idx FROM memories WHERE group_id = ? AND persona_id = ?",
                    (group_id, persona_id),
                ).fetchall()

                if not rows:
                    logger.debug(f"群聊 {group_id} (persona {persona_id}) 没有记忆记录")
                    return 0

                faiss_indices = [row[0] for row in rows]

                self._index.remove_ids(np.array(faiss_indices, dtype=np.int64))
                self._free_list.extend(faiss_indices)
                self._free_list.sort()

                self._db.execute(
                    "DELETE FROM memories WHERE group_id = ? AND persona_id = ?",
                    (group_id, persona_id),
                )
                self._db.commit()

            self._mark_dirty()
            logger.info(
                f"已删除群聊 {group_id} (persona {persona_id}) 的 {len(faiss_indices)} 条记忆"
            )
            return len(faiss_indices)
        except Exception as e:
            logger.error(f"删除群聊记忆失败: {e}", exc_info=True)
            return 0

    async def delete_by_user(
        self, user_id: str, group_id: Optional[str] = None, persona_id: str = "default"
    ) -> int:
        if not self._is_available or self._index is None:
            return 0

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                if group_id:
                    rows = self._db.execute(
                        "SELECT faiss_idx, memory_id, metadata FROM memories WHERE group_id = ? AND persona_id = ?",
                        (group_id, persona_id),
                    ).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT faiss_idx, memory_id, metadata FROM memories WHERE persona_id = ?",
                        (persona_id,),
                    ).fetchall()

                if not rows:
                    return 0

                ids_to_delete = []
                faiss_indices_to_delete = []
                for faiss_idx, memory_id, metadata_json in rows:
                    try:
                        metadata = json.loads(metadata_json)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    users = [
                        u.strip()
                        for u in str(metadata.get("active_users") or "").split(",")
                        if u.strip()
                    ]
                    if (
                        str(metadata.get("user_id") or "") == user_id
                        or user_id in users
                    ):
                        ids_to_delete.append(memory_id)
                        faiss_indices_to_delete.append(faiss_idx)

                if not ids_to_delete:
                    logger.debug(f"用户 {user_id} 没有记忆记录")
                    return 0

                self._index.remove_ids(
                    np.array(faiss_indices_to_delete, dtype=np.int64)
                )
                self._free_list.extend(faiss_indices_to_delete)
                self._free_list.sort()

                placeholders = ",".join("?" for _ in ids_to_delete)
                self._db.execute(
                    f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                    ids_to_delete,
                )
                self._db.commit()

            self._mark_dirty()
            logger.info(f"已删除用户 {user_id} 的 {len(ids_to_delete)} 条记忆")
            return len(ids_to_delete)
        except Exception as e:
            logger.error(f"删除用户记忆失败: {e}", exc_info=True)
            return 0

    async def delete_all(self, persona_id: Optional[str] = None) -> int:
        if not self._is_available:
            return 0

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                if persona_id is not None:
                    rows = self._db.execute(
                        "SELECT faiss_idx FROM memories WHERE persona_id = ?",
                        (persona_id,),
                    ).fetchall()
                    count = len(rows)
                    if count == 0:
                        return 0
                    faiss_indices = [row[0] for row in rows]
                    if self._index is not None:
                        self._index.remove_ids(np.array(faiss_indices, dtype=np.int64))
                    self._free_list.extend(faiss_indices)
                    self._free_list.sort()
                    self._db.execute(
                        "DELETE FROM memories WHERE persona_id = ?", (persona_id,)
                    )
                    self._db.commit()
                    self._mark_dirty()
                    logger.info(f"已删除 persona {persona_id} 的 {count} 条记忆")
                    return count

                count = self._count_db()
                if count == 0:
                    return 0

                # 重建空索引
                self._index = self._create_index(self._embedding_dimensions)

                self._db_write("DELETE FROM memories")

                self._free_list = []
                self._mark_dirty()
                logger.info(f"已删除所有记忆，共 {count} 条")
                return count
        except Exception as e:
            logger.error(f"删除所有记忆失败: {e}", exc_info=True)
            return 0

    async def evict_memories(self, memory_ids: List[str]) -> int:
        if not self._is_available or not memory_ids:
            return 0

        try:
            placeholders = ",".join("?" for _ in memory_ids)
            docs = self._db_execute(
                f"SELECT content FROM memories WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()

            success = await self.delete_entries(memory_ids)

            if success and docs:
                logger.info(
                    f"已淘汰 {len(memory_ids)} 条记忆：\n"
                    + "\n".join(f"  - {doc[0][:100]}..." for doc in docs[:5])
                )
                return len(memory_ids)
            return 0
        except Exception as e:
            logger.error(f"淘汰记忆失败：{e}", exc_info=True)
            return 0

    # ========================================================================
    # 知识图谱处理相关
    # ========================================================================

    async def get_unprocessed_count(self, persona_id: Optional[str] = None) -> int:
        if not self._is_available or not self._db:
            return 0

        try:
            if persona_id is not None:
                row = self._db_execute(
                    "SELECT COUNT(*) FROM memories WHERE kg_processed = 0 AND persona_id = ?",
                    (persona_id,),
                ).fetchone()
            else:
                row = self._db_execute(
                    "SELECT COUNT(*) FROM memories WHERE kg_processed = 0"
                ).fetchone()
            return row[0]
        except Exception as e:
            logger.error(f"获取未处理记忆数量失败: {e}")
            return 0

    async def get_unprocessed_memories(
        self, limit: int = 20, persona_id: Optional[str] = None
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            if persona_id is not None:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE kg_processed = 0 AND persona_id = ? LIMIT ?",
                    (persona_id, limit),
                ).fetchall()
            else:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE kg_processed = 0 LIMIT ?",
                    (limit,),
                ).fetchall()

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取未处理记忆失败: {e}")
            return []

    async def mark_memories_processed(self, memory_ids: List[str]) -> bool:
        if not self._is_available or not memory_ids:
            return False

        try:
            with self._lock:
                if self._db is None:
                    raise RuntimeError("L2 数据库未初始化或已关闭")
                for memory_id in memory_ids:
                    row = self._db.execute(
                        "SELECT metadata FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()
                    if not row:
                        continue

                    metadata = json.loads(row[0])
                    metadata["kg_processed"] = True

                    self._db.execute(
                        "UPDATE memories SET metadata = ?, kg_processed = 1 WHERE memory_id = ?",
                        (json.dumps(metadata, ensure_ascii=False), memory_id),
                    )

                self._db.commit()
            logger.info(f"已标记 {len(memory_ids)} 条记忆为已处理")
            return True
        except Exception as e:
            logger.error(f"标记记忆失败: {e}", exc_info=True)
            return False

    async def get_latest_memories(
        self,
        limit: int = 20,
        group_id: Optional[str] = None,
        persona_id: str = "default",
    ) -> List[MemorySearchResult]:
        if not self._is_available:
            return []

        try:
            if group_id:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE group_id = ? AND persona_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (group_id, persona_id, limit),
                ).fetchall()
            else:
                rows = self._db_execute(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE persona_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (persona_id, limit),
                ).fetchall()

            return [
                MemorySearchResult(
                    entry=MemoryEntry(
                        id=row[0],
                        content=row[1],
                        metadata=json.loads(row[2]),
                        persona_id=row[3],
                    ),
                    score=1.0,
                    distance=0.0,
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取最新记忆失败: {e}", exc_info=True)
            return []

    # ========================================================================
    # 模型迁移
    # ========================================================================

    async def _migrate_on_model_change(self, new_model: str, new_dim: int) -> bool:
        from .io import MemoryExporter, MemoryImporter

        if self._persist_dir is None:
            return False
        # 确保数据库已打开：initialize() 在检测到模型变更时直接调用本方法，
        # 此时 _load_existing() 尚未执行，self._db 为 None。
        # 若不打开数据库，_count_db() 会返回 0，导致迁移被静默跳过。
        db_path = self._persist_dir / "metadata.db"
        if not self._db and db_path.exists():
            self._db = self._open_db(db_path)

        # initialize() 在迁移完成前尚未将组件标记为可用，但导出、导入和
        # add_memory 等公共 API 都受该标志保护。迁移期间临时启用这些 API。
        self._is_available = True

        old_count = self._count_db()
        if old_count == 0:
            # 空库，直接创建新索引
            if not self._db:
                self._db = self._open_db(db_path)
            self._index = self._create_index(new_dim)
            self._embedding_dimensions = new_dim
            self._save_meta()
            return True

        logger.info(
            f"开始迁移 {old_count} 条记忆（模型: {new_model}，维度: {new_dim}）"
        )

        # delete_collection() 会删除整个持久化目录，因此迁移备份必须放在目录外。
        backup_dir = self._persist_dir.parent / "migration_backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_fd, backup_name = tempfile.mkstemp(
            suffix="_migration_backup.json", prefix="iris_l2_", dir=str(backup_dir)
        )
        os.close(backup_fd)
        backup_path = Path(backup_name)

        try:
            # 1. 导出所有记忆
            exporter = MemoryExporter(self)
            export_stats = await exporter.export_all(backup_path)
            logger.info(
                f"迁移步骤 1/4：导出完成，"
                f"共 {export_stats.total_count} 条，导出 {export_stats.exported_count} 条"
            )

            if export_stats.exported_count != old_count:
                logger.error(f"导出不完整，停止迁移并保留旧库及备份：{backup_path}")
                return False

            # 2. 删除旧数据
            deleted = await self.delete_collection()
            if not deleted:
                logger.error("迁移步骤 2/4：删除旧数据失败")
                return False
            logger.info("迁移步骤 2/4：已删除旧数据")

            # 3. 重新初始化（使用新模型）
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            db_path = self._persist_dir / "metadata.db"
            self._db = self._open_db(db_path)
            self._index = self._create_index(new_dim)
            self._embedding_dimensions = new_dim
            self._free_list = []
            self._save_meta()
            logger.info("迁移步骤 3/4：已创建新索引")

            # 4. 重新导入记忆
            importer = MemoryImporter(self)
            import_stats = await importer.import_from_file(
                backup_path, skip_duplicates=False
            )
            logger.info(
                f"迁移步骤 4/4：导入完成，"
                f"共 {import_stats.total_count} 条，导入 {import_stats.imported_count} 条，"
                f"跳过 {import_stats.skipped_count} 条，错误 {import_stats.error_count} 条"
            )

            success = (
                import_stats.imported_count == export_stats.exported_count
                and import_stats.error_count == 0
                and import_stats.skipped_count == 0
            )
            if success:
                with self._lock:
                    self._write_index_atomic(
                        self._index, self._persist_dir / "index.faiss"
                    )
                    self._save_meta()
                    self._dirty = False
                backup_path.unlink(missing_ok=True)
                logger.info(
                    f"迁移成功：{export_stats.exported_count} -> {import_stats.imported_count} 条"
                )
            else:
                logger.error(f"迁移导入不完整，保留全量备份供恢复：{backup_path}")

            return success

        except Exception as e:
            logger.error(f"迁移过程异常：{e}", exc_info=True)

            # 尝试恢复
            if self._index is None and backup_path.exists():
                try:
                    self._persist_dir.mkdir(parents=True, exist_ok=True)
                    db_path = self._persist_dir / "metadata.db"
                    self._db = self._open_db(db_path)
                    self._index = self._create_index(new_dim)
                    self._embedding_dimensions = new_dim
                    self._save_meta()

                    importer = MemoryImporter(self)
                    await importer.import_from_file(backup_path, skip_duplicates=False)
                    logger.info("已从备份恢复数据")
                except Exception as restore_err:
                    logger.error(f"恢复数据失败：{restore_err}", exc_info=True)

            logger.error(f"迁移失败，保留全量备份供恢复：{backup_path}")
            return False

    # ========================================================================
    # 内部辅助
    # ========================================================================

    def _db_execute(self, sql: str, params=()):
        """线程安全的 DB 执行（用于 SELECT）"""
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            return self._db.execute(sql, params)

    def _db_fetchone(self, sql: str, params=()) -> Optional[tuple]:
        """线程安全的 DB 查询：持锁内完成 execute + fetchone，返回数据行。

        与 _db_execute 不同，数据在锁内取出，避免 cursor 在锁外 fetch 时
        受并发写影响；适合配合 asyncio.to_thread 把读路径移出事件循环。
        """
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            return self._db.execute(sql, params).fetchone()

    def _db_fetchall(self, sql: str, params=()) -> List[tuple]:
        """线程安全的 DB 查询：持锁内完成 execute + fetchall，返回数据行列表。"""
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            return self._db.execute(sql, params).fetchall()

    def _db_write(self, sql: str, params=()):
        """线程安全的 DB 写入（INSERT/UPDATE/DELETE + COMMIT）"""
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            self._db.execute(sql, params)
            self._db.commit()

    def _count_db(self) -> int:
        if not self._db:
            return 0
        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            row = self._db.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0]

    def _upsert_db(
        self,
        faiss_idx: int,
        memory_id: str,
        content: str,
        metadata: Dict[str, Any],
        persona_id: str = "default",
    ) -> None:
        """插入或更新 SQLite 记录"""
        group_id = metadata.get("group_id")
        user_id = metadata.get("user_id")
        timestamp = metadata.get("timestamp")
        kg_processed = 1 if metadata.get("kg_processed") else 0
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        with self._lock:
            if self._db is None:
                raise RuntimeError("L2 数据库未初始化或已关闭")
            self._db.execute(
                """INSERT OR REPLACE INTO memories
                   (faiss_idx, memory_id, content, metadata, group_id, user_id, timestamp, kg_processed, persona_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    faiss_idx,
                    memory_id,
                    content,
                    metadata_json,
                    group_id,
                    user_id,
                    timestamp,
                    kg_processed,
                    persona_id,
                ),
            )
            self._db.commit()

    def _upsert_db_unlocked(
        self,
        faiss_idx: int,
        memory_id: str,
        content: str,
        metadata: Dict[str, Any],
        persona_id: str = "default",
    ) -> None:
        """插入或更新 SQLite 记录（调用方需持有 _lock）"""
        group_id = metadata.get("group_id")
        user_id = metadata.get("user_id")
        timestamp = metadata.get("timestamp")
        kg_processed = 1 if metadata.get("kg_processed") else 0
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        if self._db is None:
            raise RuntimeError("L2 数据库未初始化或已关闭")
        self._db.execute(
            """INSERT OR REPLACE INTO memories
               (faiss_idx, memory_id, content, metadata, group_id, user_id, timestamp, kg_processed, persona_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                faiss_idx,
                memory_id,
                content,
                metadata_json,
                group_id,
                user_id,
                timestamp,
                kg_processed,
                persona_id,
            ),
        )
        self._db.commit()
