from iris_memory.commands.parser import CommandParser


class TestParseAtForms:
    """三种 @ 形式的解析"""

    def test_outline_at_form(self):
        """outline 渲染的真实 @（[At:123456]）直接得到目标用户 ID"""
        parsed = CommandParser.parse("iris_mem l2 clear [At:123456]")

        assert parsed.is_valid
        assert parsed.module == "l2"
        assert parsed.sub_command == "clear"
        assert parsed.args.target_user_id == "123456"
        assert parsed.args.target_user_name is None

    def test_message_str_at_form(self):
        """message_str 渲染的 @名字(123456) 同时得到 ID 与名称"""
        parsed = CommandParser.parse("iris_mem l2 clear @张三(123456)")

        assert parsed.is_valid
        assert parsed.args.target_user_id == "123456"
        assert parsed.args.target_user_name == "张三"

    def test_plain_text_at_form(self):
        """纯文本 @张三 仅得到名称（ID 由 extract_target_user_id 反查）"""
        parsed = CommandParser.parse("iris_mem l2 clear @张三")

        assert parsed.is_valid
        assert parsed.args.target_user_name == "张三"
        assert parsed.args.target_user_id is None

    def test_outline_at_without_subcommand(self):
        """[At:...] 不应被误认为子指令"""
        parsed = CommandParser.parse("iris_mem l2 [At:123456]")

        assert parsed.is_valid
        assert parsed.module == "l2"
        assert parsed.sub_command is None
        assert parsed.args.target_user_id == "123456"

    def test_outline_at_conflicts_with_group_scope(self):
        parsed = CommandParser.parse("iris_mem l2 clear [At:123456] --group")

        assert not parsed.is_valid
        assert "不能同时使用" in parsed.error_message

    def test_outline_at_conflicts_with_all_scope(self):
        parsed = CommandParser.parse("iris_mem l2 clear [At:123456] --all")

        assert not parsed.is_valid


class TestParseBasics:
    """基础解析回归"""

    def test_module_and_sub_command(self):
        parsed = CommandParser.parse("iris_mem l1 clear")

        assert parsed.is_valid
        assert parsed.module == "l1"
        assert parsed.sub_command == "clear"

    def test_group_and_all_conflict(self):
        parsed = CommandParser.parse("iris_mem l2 clear --group --all")

        assert not parsed.is_valid

    def test_plain_name_conflicts_with_group_scope(self):
        parsed = CommandParser.parse("iris_mem l2 clear @张三 --group")

        assert not parsed.is_valid

    def test_subcommand_case_insensitive(self):
        parsed = CommandParser.parse("iris_mem L2 CLEAR")

        assert parsed.sub_command == "clear"

    def test_not_a_command(self):
        parsed = CommandParser.parse("今天天气不错")

        assert not parsed.is_valid
