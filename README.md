# Iris Chat Memory

<p align="center">
  <img src="logo.png" alt="Iris Logo" width="128" height="128">
</p>

<p align="center">
  <img src="https://count.getloli.com/get/@astrbot_plugin_iris_chat_memory?theme=moebooru" alt="Visitor Count">
</p>

**让机器人真正"记住你"** — 面向 AstrBot 的智能记忆插件，为群聊场景深度优化。

当前版本：**0.3.0** · [更新日志](CHANGELOG.md) · [版本发布](https://github.com/leafliber/astrbot_plugin_iris_chat_memory/releases)

你的 Bot 能做到这些吗？

- 还记得你上周说过的话，而不是只记住最近 20 条消息？
- 知道你喜欢什么、讨厌什么，而不用你每次重复？
- 能把碎片化的聊天内容串联成结构化的知识网络？
- 在群聊中自动区分不同人的记忆，互不干扰？

Iris 通过三层记忆架构 + 用户画像 + 知识图谱实现这一切。

## 它是如何工作的

```
用户消息 ──→ L1 Buffer（工作记忆）
                  │
                  ├── 超量时自动总结 ──→ L2 记忆库（语义检索）
                  │                           │
                  │                           ├── 梦境任务深度加工
                  │                           │     ├── 合并碎片记忆
                  │                           │     ├── 修正矛盾信息
                  │                           │     ├── 发现行为模式
                  │                           │     └── 提取知识 ──→ L3 知识图谱
                  │                           │
                  │                           └── 语义检索注入上下文
                  │
                  └── LLM 请求时注入：
                        ├── 近期对话上下文（L1）
                        ├── 相关历史记忆（L2）
                        ├── 知识图谱关联（L3）
                        ├── 用户/群聊画像
                        └── 图片内容描述
```

当用户发送消息时，Iris 自动将**最近对话 + 相关记忆 + 用户画像 + 知识图谱**注入 LLM 上下文，让 Bot 的回复带有真正的"记忆"。

## 核心功能

### 三层记忆

| 层级 | 存储 | 职责 | 特点 |
|------|------|------|------|
| **L1 消息缓冲** | 内存 | 短期工作记忆 | 三段式 FIFO，自动总结旧消息写入 L2 |
| **L2 记忆库** | FAISS + SQLite | 中期语义记忆 | 向量检索 + 遗忘算法 + 查询改写 + Token 预算 |
| **L3 知识图谱** | SQLite | 长期结构化知识 | 实体关系提取 + 图增强检索 + 节点淘汰 |

**L2 记忆库**不只是存文本 — 它内置遗忘评分算法（近因性衰减 + 访问频率 + 置信度），自动淘汰不再重要的记忆，保留真正有价值的内容。

**L3 知识图谱**从 L2 记忆中提取实体和关系，构建结构化知识网络。当检索到某条记忆时，会沿图谱路径扩展相关节点，发现隐含关联。

### 用户画像

自动从对话中学习并维护每个用户/群聊的特征画像：

- **用户画像**：性格标签、兴趣爱好、职业、语言风格、沟通偏好、情感基线、禁忌话题
- **群聊画像**：群聊氛围、核心特征、常见话题、禁忌话题
- **三级更新频率**：短期实时更新 → 中期（按总结次数/时间触发）→ 长期（仅显著新信息时更新）
- **字段独立置信度**：每个字段独立追踪置信度，高置信度信息不会被低置信度信息覆盖

### 梦境任务

受人类睡眠记忆巩固机制启发，6 阶段离线记忆加工流水线（每个阶段可独立开关）：

| 阶段 | 做什么 | 为什么需要 |
|------|--------|-----------|
| 合并重复 | 归拢同一话题的碎片记忆 | 减少冗余，提高检索质量 |
| 时间锚定 | "昨天"→"2024-03-15" | 防止相对时间表达随时间失效 |
| 矛盾消解 | 检测并解决记忆间的逻辑冲突 | 避免 Bot 自相矛盾 |
| 模式挖掘 | 发现隐含的行为规律和偏好 | 深度理解用户特征 |
| 知识提取 | 提取实体和关系写入 L3 | 构建结构化知识网络 |
| 遗忘清洗 | 淘汰低价值记忆和图谱节点 | 控制存储和 Token 消耗 |

### 图片理解

Bot 不只"看"文字 — 它也能理解图片内容：

- 视觉模型解析图片内容并注入上下文
- pHash 感知哈希自动去重，避免重复解析
- 无效图过滤（纯色图、过小图片自动跳过）
- 每日解析配额控制，防止 Token 爆炸
- 支持与 [Message Recorder](https://github.com/leafliber/astrbot_plugin_message_recorder) 插件联动获取本地图片，避免链接过期

### LLM 工具

为 LLM 提供 6 个主动记忆管理工具，让 Bot 可以自己管理记忆：

| 工具 | 功能 |
|------|------|
| `save_memory` | 主动保存重要记忆 |
| `search_memory` | 检索相关记忆（可选附带图谱上下文） |
| `correct_memory` | 修正错误记忆或幻觉（同时更新 L2 和 L3） |
| `save_knowledge` | 手动添加实体和关系到知识图谱 |
| `search_knowledge_graph` | 搜索知识图谱中的实体和关系 |
| `get_profile` | 获取用户或群聊画像 |

### Web 管理界面

内置完整的 Web 管理面板：

- **Dashboard**：系统概览、组件状态、Token 使用统计
- **L1 管理**：消息缓冲查看和管理
- **L2 管理**：记忆库浏览、搜索、编辑
- **L3 管理**：知识图谱可视化浏览
- **画像管理**：用户/群聊画像查看和编辑
- **数据管理**：L2/L3/画像的导入导出、全量备份恢复
- **隐藏配置**：运行时热修改内部参数，无需重启

### 其他能力

- **多平台支持**：原生适配 QQ（OneBot11），其他平台自动通过通用适配器降级运行，并支持按需扩展
- **会话隔离**：群聊记忆隔离、群聊画像隔离、Bot 人格隔离
- **上下文控制**：自动清理 AstrBot 内置对话历史，确保上下文完全由插件管理
- **被动触发检测**：区分主动对话和 sampling/主动回复，按需降级图片解析等高消耗操作
- **输入清理**：Prompt 注入过滤 + 输入长度限制
- **置信度分级**：总结时对每条记忆评估 high/medium/low 置信度
- **组件故障隔离**：单组件初始化失败不影响其他组件
- **后台初始化**：重型组件（FAISS、SQLite）后台异步初始化，不阻塞启动
- **灵活嵌入**：L2 支持使用 AstrBot Embedding Provider 或本地 sentence-transformers 模型

## 安装

### 前置要求

| 资源 | 最低要求 | 推荐配置 |
|------|----------|----------|
| **内存** | 2 GB 可用 | 4 GB+ 可用 |
| **磁盘** | 500 MB | 2 GB+（随记忆量增长） |
| **CPU** | 2 核 | 4 核+ |

> L2 FAISS 内存占用与记忆条目正相关（1 万条约 50～150 MB，主要为向量），L3 SQLite 节点 / 边直接落盘（5 万节点约 20～50 MB）。资源受限设备可关闭 L3 或使用 Provider 嵌入以降低开销。

### 安装步骤

1. 在 AstrBot 插件市场搜索安装，或将插件放入 `data/plugins` 目录
2. 确认依赖已自动安装（`faiss-cpu`、`numpy`）
3. L2 嵌入模型默认使用 AstrBot 配置的 Embedding Provider，无需额外下载

### 安装后建议

- **关闭 AstrBot 自带的"上下文感知"**，避免与插件冲突
- **配置管理员 ID**：在 AstrBot WebUI → 配置 → 其他配置 → 管理员 ID 列表中添加你的 ID（可用 `/sid` 获取），`iris_mem` 指令需要管理员权限
- **确保 Embedding Provider 可用**：在 AstrBot 中配置可用的 Embedding Provider（如 OpenAI 兼容服务、Ollama 等），否则 L2 将降级使用本地嵌入模型

### 验证安装

发送以下命令检查各层级是否正常：

```
/iris_mem l1 stats   ← L1 消息缓冲
/iris_mem l2 stats   ← L2 记忆库
/iris_mem l3 stats   ← L3 知识图谱
```

能看到统计数据即表示核心链路正常运行。

## 管理指令

所有指令需要管理员权限，格式：`/iris_mem <模块> <子指令> [参数]`

### L1 消息缓冲

| 命令 | 说明 |
|------|------|
| `/iris_mem l1 stats` | 查看统计 |
| `/iris_mem l1 clear` | 清空当前用户的 L1 |
| `/iris_mem l1 clear @用户` | 清空指定用户的 L1 |
| `/iris_mem l1 clear --group` | 清空当前群聊的 L1 |
| `/iris_mem l1 clear --all` | 清空所有 L1 |

### L2 记忆库

| 命令 | 说明 |
|------|------|
| `/iris_mem l2 stats` | 查看统计 |
| `/iris_mem l2 clear` | 清空当前用户的 L2 记忆 |
| `/iris_mem l2 clear @用户` | 清空指定用户的 L2 记忆 |
| `/iris_mem l2 clear --group` | 清空当前群聊的 L2 记忆 |
| `/iris_mem l2 clear --all` | 清空所有 L2 记忆 |

### L3 知识图谱

| 命令 | 说明 |
|------|------|
| `/iris_mem l3 stats` | 查看统计 |
| `/iris_mem l3 clear` | 清空当前用户的 L3 知识图谱 |
| `/iris_mem l3 clear @用户` | 清空指定用户的 L3 知识图谱 |
| `/iris_mem l3 clear --group` | 清空当前群聊的 L3 知识图谱 |
| `/iris_mem l3 clear --all` | 清空所有 L3 知识图谱 |

### 画像管理

| 命令 | 说明 |
|------|------|
| `/iris_mem profile show` | 显示当前用户画像 |
| `/iris_mem profile show @用户` | 显示指定用户画像 |
| `/iris_mem profile reset` | 重置当前用户画像 |
| `/iris_mem profile reset @用户` | 重置指定用户画像 |
| `/iris_mem profile reset --group` | 重置当前群聊所有用户画像 |
| `/iris_mem profile reset --all` | 重置所有用户画像 |
| `/iris_mem profile group show` | 显示群聊画像 |
| `/iris_mem profile group reset` | 重置群聊画像 |

### 全量操作

| 命令 | 说明 |
|------|------|
| `/iris_mem all clear` | 清空当前用户所有记忆和画像 |
| `/iris_mem all clear @用户` | 清空指定用户所有记忆和画像 |
| `/iris_mem all clear --group` | 清空当前群聊所有记忆和画像 |
| `/iris_mem all clear --all` | 清空所有数据（谨慎使用） |

### 操作粒度

| 粒度 | 说明 |
|------|------|
| 默认（无参数） | 仅操作当前用户在当前群聊的数据 |
| `@用户` | 操作指定用户在当前群聊的数据 |
| `--group` / `-g` | 操作当前群聊的所有数据 |
| `--all` / `-a` | 操作所有数据（全局） |

> 每个模块均支持 `/iris_mem <模块> help`（如 `/iris_mem l2 help`）查看该模块可用命令；不带子命令时，`l1`/`l2`/`l3` 等价于查看统计，`profile` 等价于显示当前用户画像。

## 配置

### 推荐配置

适合大多数用户，兼顾效果与成本：

| 配置项 | 建议值 | 说明 |
|--------|--------|------|
| `l1_buffer.enable` | `true` | 开启 L1 上下文缓冲 |
| `l1_buffer.inject_queue_length` | `50` | 上下文消息条数，越大越完整但 Token 消耗越多 |
| `l1_buffer.image_parsing.enable` | `true` | 开启图片解析（需要视觉模型） |
| `l2_memory.enable` | `true` | 开启 L2 记忆库 |
| `l2_memory.embedding_source` | `"provider"` | 使用 AstrBot Embedding Provider（推荐） |
| `l2_memory.top_k` | `10` | 检索数量，3～10 平衡效果与 Token |
| `l2_memory.relevance_threshold` | `0.3` | 相关性阈值 |
| `l3_kg.enable` | `true` | 开启知识图谱 |
| `profile.enable` | `true` | 开启画像系统 |
| `profile.enable_auto_injection` | `true` | 自动注入画像到上下文 |
| `context_control.enable_conversation_cleanup` | `true` | 自动清理 AstrBot 内置对话历史 |
| `scheduled_tasks.enable_dream` | `true` | 开启梦境任务 |

### 完整配置参考

<details>
<summary>L1 消息缓冲</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `l1_buffer.enable` | 启用 L1 上下文缓冲 | `true` |
| `l1_buffer.summary_provider` | 总结模型（留空使用默认 Provider） | `""` |
| `l1_buffer.inject_queue_length` | 上下文消息条数 | `50` |
| `l1_buffer.image_parsing.enable` | 启用图片解析 | `false` |
| `l1_buffer.image_parsing.provider` | 图片解析模型（需支持视觉能力） | `""` |
| `l1_buffer.image_parsing.mode` | 解析模式（`all` / `related`） | `"related"` |
| `l1_buffer.image_parsing.daily_quota` | 每日解析限额 | `200` |

> 图片解析的并发、超时、缓存等内部参数见下方 「隐藏配置（高级调优）」 的"图片处理"分组。

</details>

<details>
<summary>L2 记忆库</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `l2_memory.enable` | 启用 L2 记忆库 | `true` |
| `l2_memory.embedding_source` | 嵌入模型来源（`"provider"` / `"local"`） | `"provider"` |
| `l2_memory.embedding_provider` | Embedding Provider ID（留空自动选择） | `""` |
| `l2_memory.embedding_model` | 本地嵌入模型（仅 local 模式） | `"BAAI/bge-small-zh-v1.5"` |
| `l2_memory.top_k` | 检索 Top-K | `10` |
| `l2_memory.relevance_threshold` | 相关性阈值 | `0.3` |

> L2 的最大条目数、检索超时、查询改写等内部参数见下方 「隐藏配置（高级调优）」。

> ⚠️ **切换嵌入模型前请先备份数据！** 更换模型后向量维度可能改变，插件会自动重建记忆库，已有记忆可能会丢失。可通过 Web 管理界面导出备份。

</details>

<details>
<summary>L3 知识图谱</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `l3_kg.enable` | 启用 L3 知识图谱 | `true` |
| `l3_kg.extraction_provider` | 实体提取模型（留空使用默认 Provider） | `""` |

> L3 的节点/边上限、检索超时、扩展深度、类型白名单、图谱遗忘等内部参数见下方 「隐藏配置（高级调优）」。

</details>

<details>
<summary>画像系统</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `profile.enable` | 启用画像系统 | `true` |
| `profile.analysis_provider` | 画像分析模型（留空使用默认 Provider） | `""` |
| `profile.enable_auto_injection` | 启用画像自动注入 | `true` |

</details>

<details>
<summary>隔离配置</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `isolation_config.enable_group_memory_isolation` | 群聊记忆隔离 | `false` |
| `isolation_config.enable_group_isolation` | 群聊用户画像隔离 | `false` |
| `isolation_config.enable_persona_isolation` | Bot 人格隔离 | `false` |

</details>

<details>
<summary>梦境任务</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `scheduled_tasks.provider` | 梦境任务模型（留空使用默认 Provider） | `""` |
| `scheduled_tasks.enable_dream` | 启用梦境任务 | `true` |
| `scheduled_tasks.dream_enable_consolidation` | 阶段 1：合并重复项 | `true` |
| `scheduled_tasks.dream_enable_temporal_anchor` | 阶段 2：时间锚定 | `true` |
| `scheduled_tasks.dream_enable_contradiction` | 阶段 3：矛盾消解 | `true` |
| `scheduled_tasks.dream_enable_pattern_discovery` | 阶段 4：模式挖掘 | `true` |
| `scheduled_tasks.dream_enable_knowledge_extract` | 阶段 5：知识提取 | `true` |
| `scheduled_tasks.dream_enable_pruning` | 阶段 6：遗忘清洗 | `true` |

</details>

<details>
<summary>上下文控制</summary>

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `context_control.enable_conversation_cleanup` | 自动清理 AstrBot 内置对话历史 | `true` |

</details>

<details>
<summary>隐藏配置（高级调优）</summary>

隐藏配置不在 WebUI 中展示，用于控制内部行为。存储在 `data/iris_memory/hidden_config.json`，支持运行时热修改。

**Token 预算**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `token_budget_max_tokens` | `2000` | L2 记忆注入上下文最大 Token 数 |

**L1 缓冲**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `l1_segment_1_length` | `10` | L1-1 最新段消息数（始终注入上下文） |
| `l1_segment_3_length` | `10` | L1-3 缓冲段消息数（辅助总结理解） |
| `l1_max_queue_tokens` | `4000` | 队列最大 Token 数，超限触发总结 |
| `l1_max_single_message_tokens` | `500` | 单条消息最大 Token 数，超限丢弃 |
| `l1_inject_max_content_chars` | `200` | 注入时单条消息最大字符数，0 不截断 |
| `l1_max_memories_per_summary` | `10` | 每次总结写入 L2 的最大记忆条数 |

**遗忘算法**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `forgetting_lambda` | `0.1` | 近因性衰减系数 |
| `forgetting_threshold` | `0.3` | 遗忘阈值 |
| `forgetting_immediate_eviction_threshold` | `0.1` | 极端低分直接淘汰阈值 |

**遗忘确认**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `forgetting_llm_confirm_enable` | `false` | 启用 LLM 最终兜底确认遗忘 |
| `forgetting_llm_confirm_provider` | `""` | 确认使用的 Provider（空则使用默认） |
| `forgetting_llm_confirm_threshold` | `0.15` | 评分低于此值才触发 LLM 确认 |

**L2 记忆**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `l2_similarity_threshold` | `0.90` | L2 去重相似度阈值 |
| `l2_max_entries` | `10000` | L2 最大条目数（预留） |
| `l2_timeout_ms` | `4000` | L2 检索超时（毫秒） |

**L2 查询改写**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `l2_query_rewrite_enable` | `true` | 启用 L2 检索查询改写 |
| `l2_query_rewrite_provider` | `""` | 查询改写使用的 Provider（空则使用默认） |
| `l2_query_rewrite_timeout_ms` | `3000` | 查询改写超时（毫秒） |

**L3 知识图谱**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `l3_max_nodes` | `50000` | L3 最大节点数（预留） |
| `l3_max_edges` | `100000` | L3 最大边数（预留） |
| `l3_timeout_ms` | `1500` | L3 检索超时（毫秒） |
| `l3_expansion_depth` | `2` | 图谱检索路径扩展深度 |
| `l3_enable_type_whitelist` | `true` | 启用 LLM 实体类型白名单约束 |
| `l3_max_inject_tokens` | `600` | 知识图谱注入上下文最大 Token 数 |
| `node_confidence_threshold` | `0.3` | 节点最低置信度 |
| `forgetting_threshold_kg` | `0.2` | 知识图谱遗忘阈值 |
| `kg_retention_days` | `30` | 知识图谱保留天数 |
| `kg_extraction_semantic_weight` | `0.5` | 知识提取：语义相似记忆权重 |
| `kg_extraction_same_group_weight` | `0.3` | 知识提取：同群聊记忆权重 |
| `kg_extraction_same_user_weight` | `0.2` | 知识提取：同用户记忆权重 |

**LLM 调用管理**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `call_log_max_entries` | `100` | 调用日志最大保留条数 |
| `llm_call_timeout_ms` | `60000` | LLM 调用全局超时（毫秒，0 不限制），兜底防止 Provider 卡死 |
| `enable_context_logging` | `false` | 启用 LLM 上下文日志输出 |

**梦境任务**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `dream_task_interval_hours` | `24` | 梦境任务执行间隔（小时） |
| `dream_consolidation_similarity_threshold` | `0.85` | 合并阶段相似度阈值 |
| `dream_consolidation_batch_size` | `10` | 合并阶段批次大小 |
| `dream_consolidation_scan_budget` | `500` | 合并阶段每轮扫描条数上限 |
| `dream_consolidation_query_batch_size` | `50` | 合并阶段查询批次大小 |
| `dream_consolidation_max_group_size` | `5` | 合并阶段最大分组大小 |
| `dream_temporal_anchor_batch_size` | `50` | 时间锚定阶段批次大小 |
| `dream_contradiction_similarity_floor` | `0.55` | 矛盾消解相似度下限 |
| `dream_contradiction_similarity_ceiling` | `0.85` | 矛盾消解相似度上限 |
| `dream_contradiction_max_groups` | `20` | 矛盾消解最大分组数 |
| `dream_pattern_sample_size` | `30` | 模式挖掘采样数 |
| `dream_pattern_min_confidence` | `"medium"` | 模式挖掘最低置信度 |
| `dream_knowledge_extract_min_unprocessed` | `10` | 知识提取最小未处理记忆数 |
| `dream_knowledge_extract_batch_size` | `20` | 知识提取批次大小 |
| `eviction_batch_size` | `100` | 遗忘清洗批次大小 |
| `image_cache_cleanup_interval_hours` | `24` | 图片缓存清理任务间隔（小时） |

**画像系统**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `profile_max_messages_for_analysis` | `50` | 分析时最大消息数 |
| `profile_mid_update_interval_summaries` | `5` | 中期更新：每隔 N 次总结触发 |
| `profile_mid_update_interval_hours` | `24.0` | 中期更新：最短间隔（小时） |
| `profile_long_update_interval_hours` | `168.0` | 长期更新：最短间隔（小时，默认 7 天） |

**图片处理**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `image_max_parse_per_request` | `5` | 单次请求最大图片解析数 |
| `image_max_concurrent_parse` | `3` | 最大并发图片解析数 |
| `image_cache_retention_days` | `7` | 图片解析结果缓存保留天数 |
| `image_skip_on_passive_trigger` | `true` | 被动触发时跳过图片解析 |
| `image_parse_timeout_ms` | `30000` | 单次请求图片解析整体超时（毫秒，0 不限制） |
| `image_phash_enable` | `true` | 启用 pHash 感知哈希去重 |
| `image_phash_threshold` | `10` | pHash 汉明距离阈值（越小越严格） |
| `image_filter_enable` | `true` | 启用无效图过滤（纯色/过小） |
| `image_filter_min_size` | `16` | 最小图片尺寸（像素） |
| `image_filter_std_threshold` | `5.0` | 纯色检测标准差阈值 |

**输入清理**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `input_sanitizer_enable` | `true` | 启用 Prompt 注入过滤 |
| `input_sanitizer_max_length` | `10000` | 输入最大长度 |

</details>

## FAQ

### Q1：为什么"记不住"或记忆效果不好？

- 确认 `l1_buffer.enable`、`l2_memory.enable`、`l3_kg.enable` 均为 `true`
- 检查是否配置了可用的 LLM Provider（总结、提取、分析等核心功能依赖 LLM）
- 使用 `/iris_mem l2 stats` 查看记忆库是否有记录
- 确认你查询的是同一会话（群聊记忆隔离时，不同群聊的记忆独立）

### Q2：为什么会出现回复冲突或重复发言？

与 AstrBot 自带上下文功能重叠。建议关闭 AstrBot 同类能力，保留 `context_control.enable_conversation_cleanup = true`。

### Q3：与其他依赖 `req.contexts` 的插件冲突？

本插件开启上下文接管（`context_control.enable_conversation_cleanup = true`，默认开启）后，会在每次 LLM 请求前**清空 `req.contexts` 字段**，改由 L1/L2/L3 记忆系统统一注入上下文。这意味着任何依赖 `req.contexts` 的第三方插件其写入内容都会被一同清空，从而导致功能失效。

**典型冲突示例**：

- **对话历史压缩/长上下文插件**：这类插件通常把长对话压缩后写回 `req.contexts`，由本插件清空后压缩结果无法生效，等于该插件完全失效
- **跨会话上下文 / 上下文增强插件**：往 `req.contexts` 注入额外历史消息或系统预设的插件，注入内容会被清空
- **手动拼接上下文的记忆类插件**：同类记忆插件若也操作 `req.contexts`，两者会互相覆盖，出现上下文时有时无、记忆错乱

**解决方案**：

- 优先方案：避免同时启用同类功能插件，由本插件统一管理上下文
- 若必须共存：将 `context_control.enable_conversation_cleanup` 设为 `false`，但此时 AstrBot 内置对话历史与本插件注入内容会同时进入上下文，可能导致 Token 翻倍、回复重复，**不推荐**

### Q4：Token 消耗太高怎么办？

- 降低 `l1_buffer.inject_queue_length`（如 30）
- 降低 `l2_memory.top_k`（如 3～5）
- 关闭不需要的功能（如 `l1_buffer.image_parsing.enable = false`）
- 为不同任务配置不同的 Provider（使用轻量模型处理总结/提取等任务）

### Q5：切换嵌入模型后检索变差或报维度问题？

不同模型维度可能不同（512/768/1024），切换后插件会自动重建记忆库，已有记忆可能会丢失。**切换前请务必备份数据**（Web 管理界面导出）。建议在初期确定好嵌入模型后不要频繁更换。

### Q6：`iris_mem` 指令无响应？

确认你已配置为管理员。使用 `/sid` 获取用户 ID，在 AstrBot WebUI 中添加管理员 ID。

### Q7：数据会上传到云端吗？

默认存储在本地（FAISS / SQLite / 本地文件）。仅在你配置并调用外部 LLM 时，会向所选 Provider 发送必要文本。

### Q8：如何彻底清空所有插件数据？

```
/iris_mem all clear --all
```

### Q9：L2 嵌入模型应该选 Provider 还是本地？

- **Provider（推荐）**：使用 AstrBot 配置的 Embedding Provider（如 OpenAI 兼容服务、Ollama 等），无需下载模型，不占用本地内存
- **本地**：使用本地自选的轻量模型（需要手动在日志面板，安装pip包`sentence-transformers`），首次使用需下载约 96 MB 模型文件，运行时额外占用 200～500 MB 内存。适合无法访问外部 Embedding 服务的离线环境
- 当Provider和本地模型都配置时，且启用了Provider，当Provider 不可用时会尝试降级到本地模型，维度不同，将无法降级。

## 链接

- [AstrBot 主项目](https://github.com/AstrBotDevs/AstrBot)
- [插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)

## 许可证

AGPL-3.0

欢迎提交 Issue 和 Pull Request。
