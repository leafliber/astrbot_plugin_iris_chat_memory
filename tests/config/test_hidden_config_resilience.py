import json
from pathlib import Path
from unittest.mock import patch


class TestHiddenConfigResilience:
    def test_dirty_flag_restored_when_persist_fails(self, tmp_path: Path):
        from iris_memory.config.hidden_config import HiddenConfigManager
        from iris_memory.config.defaults import HiddenConfig

        manager = HiddenConfigManager(tmp_path / "hidden_config.json", HiddenConfig())
        manager.set("l2_checkpoint_writes", 99)
        # 第一次写盘成功后再次制造失败场景
        with patch(
            "iris_memory.utils.persistence.atomic_write_text",
            side_effect=OSError("磁盘满"),
        ):
            manager.set("l2_checkpoint_writes", 99)
            assert manager._dirty is True, "写盘失败必须保留脏标志"

        # 磁盘恢复后，下一次变更能把挂起的值写出
        manager.set("l2_checkpoint_writes", 60)
        data = json.loads((tmp_path / "hidden_config.json").read_text())
        assert data["l2_checkpoint_writes"] == 60

    def test_load_sanitizes_wrong_types(self, tmp_path: Path):
        from iris_memory.config.hidden_config import HiddenConfigManager
        from iris_memory.config.defaults import HiddenConfig

        defaults = HiddenConfig()
        int_key = next(
            f.name
            for f in defaults.__dataclass_fields__.values()
            if f.type in ("int", "float") or "int" in str(f.type)
        )
        path = tmp_path / "hidden_config.json"
        path.write_text(
            json.dumps({int_key: "not-a-number", "__foreign__": {"ok": 1}}),
            encoding="utf-8",
        )

        manager = HiddenConfigManager(path, defaults)
        # 错误类型回退默认值而不是污染运行时
        assert manager.get(int_key) == getattr(defaults, int_key)
        # 外部槽位按设计原样保留
        assert manager.get("__foreign__") == {"ok": 1}

    def test_reset_to_defaults_notifies_observers(self, tmp_path: Path):
        from iris_memory.config.hidden_config import HiddenConfigManager
        from iris_memory.config.defaults import HiddenConfig

        manager = HiddenConfigManager(tmp_path / "hidden_config.json", HiddenConfig())
        manager.set("l2_checkpoint_writes", 99)

        notified: list[tuple[str, object, object]] = []
        manager.add_observer(lambda k, old, new: notified.append((k, old, new)))

        manager.reset_to_defaults()

        # 观察者必须真正收到被覆盖键的重置通知（此前遍历空 dict 是死代码）
        reset_keys = {key for key, _old, _new in notified}
        assert "l2_checkpoint_writes" in reset_keys
        old_values = {key: old for key, old, _new in notified}
        assert old_values["l2_checkpoint_writes"] == 99
