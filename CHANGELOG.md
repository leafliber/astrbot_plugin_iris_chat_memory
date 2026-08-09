# 更新日志 (CHANGELOG)

本项目所有重要变更均会记录于此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.1] - 2026-08-09

### 优化

- **梦境任务零 embedding 消耗**：梦境阶段1（合并重复项）与阶段3（矛盾消解）的相似度检索改为复用 FAISS 索引中已存的向量（新增 `L2MemoryAdapter.batch_retrieve_by_ids`，经 `faiss_idx` reconstruct 取向量），不再对记忆文本重新计算 embedding，彻底消除梦境任务对 embedding 模型的周期性批量调用（此前每轮每人格可达数百次，embedding 来源为 AstrBot Provider 时表现为外部 API 风暴）。阶段3 同时补上此前缺失的扫描预算（`dream_contradiction_scan_budget`，默认 200）与批量检索（`dream_contradiction_query_batch_size`，默认 50）；阶段1 扫描预算默认值 500 下调至 200。

### 修复

- **L2 FAISS 稀疏索引覆盖**：free-list 缺失时改用 SQLite 中 `MAX(faiss_idx) + 1` 分配新向量 ID，避免删除产生空洞后以 `ntotal` 复用仍在使用的 ID，造成向量与元数据被覆盖。
- **L2 跨人格去重与模型迁移**：相似记忆去重改为扫描多个近邻后按 `persona_id` 匹配，并补齐 SQLite upsert 的人格字段；模型迁移期间临时启用受守卫的导入导出 API，同时将备份移到 collection 目录外，避免备份随旧索引一起删除。
- **梦境加工数据与主体丢失**：重复记忆合并改为先写新记录、成功后再删除旧记录；合并结果继承一致的 `user_id` / `active_users`，主体冲突或缺失时标记 `subjectless`；模式挖掘拒绝写入缺少 `PERSON` 的 L2/L3 记录，并将有效主体写入 L2 metadata。
- **L1 统计边界**：用户消息移除后的 `total_tokens` 钳制为非负值；总结日志按实际成功写入的记忆统计 high/medium 置信度，避免质量过滤或去重后统计错位。
- **私聊 L1 工作记忆跨用户污染**：QQ 私聊事件的 `group_id` 为空字符串，L1 缓冲直接以 `group_id` 作为队列键，导致所有私聊用户共用同一个空字符串队列、上下文互相污染，WebUI 中显示为无法点击加载的空白项。平台适配层新增 `get_session_id()`——群聊返回群号、私聊返回 `private:{user_id}`——L1 消息写入、上下文注入、图片队列、指令清空等所有路径统一改用会话键，每个私聊用户拥有独立队列。总结写入 L2 与画像归属保持既有行为（私聊仍归入空群 ID 桶，避免写入读不到的命名空间）。
- **WebUI 支持私聊会话查看**：L1 缓冲页队列列表为私聊会话显示用户昵称与用户 ID（图标区分私聊/群聊），私聊队列可正常选中、刷新与单独清空；"群聊列表"等相关文案调整为"会话列表"。
- **L1 空队列键防污染兜底**：`add_message` 对空队列键拒绝写入并告警，杜绝任何异常事件（user_id 获取失败导致会话键为空）再次产生共享 `""` 队列。
- **遗留空键队列可治理**：修复前产生的 `""` 队列在 WebUI 中显示为"未知会话"，支持选中查看消息与单独清空（前后端全链路不再把空字符串键当作"未选择/未传参"，清空空键队列不再误触发清空全部）。

## [0.2.0] - 2026-07-02

> 本次更新聚焦稳定性与 Token 效率：修复了人格隔离导入导出、画像并发丢失更新、L3 节点合并回滚等多处数据安全问题；全面精简 LLM Prompt 并优化总结解析逻辑以降低 Token 消耗；新增 L3 无主节点治理机制和前端构建拆分。

### 新增

- **多模态图片传递优化**：`LLMManager.generate_with_images` 改用 `image_urls` 参数直接传递图片给 AstrBot Provider，由 Provider 负责构建正确的多模态消息格式，不再手动拼接 OpenAI Vision 格式的 contexts。
- **前端构建拆分与体积优化**：移除 `vite-plugin-singlefile` 插件，将前端产物从单文件拆分为 `index.html` + `iris.css` + `iris.js` 三文件；改用 Terser 压缩（drop console/debugger、3 趟压缩、target es2020），原始体积减小约 53 kB（2.6%），gzip 体积减小约 32 kB（5.2%）。
- **L3 无主节点治理机制**：针对 Preference/Trait/Belief/Goal/Skill 类型节点缺少 Person 关联边成为"孤儿"的问题，建立全链路治理：
  - **知识提取阶段**：对缺少 Person 边的主体绑定类型节点降级置信度至 0.4 并标记 `orphaned_subject`，交由遗忘清洗按综合评分处理，不做硬删除以避免误伤。
  - **梦境遗忘清洗**：新增 `_cleanup_orphaned_subject_nodes` 阶段，在遗忘淘汰前定向清除无 Person 关联且置信度低于 0.5 的节点。
  - **模式挖掘阶段**：`person_id` 为空时跳过节点创建，从源头杜绝无主节点产生。
  - **L1 总结标记**：总结未能关联到具体用户时标记 `subjectless`，遗忘评分对该类记忆阈值提高 20% 加速淘汰。
  - **save_knowledge 工具**：手动保存知识时同样对无主体节点降级置信度并提示用户。
- **@提及用户提取**：平台适配层新增 `get_mentioned_users` 抽象方法，OneBot11 适配器实现从消息段（段列表格式与 CQ 码字符串格式）提取 `[CQ:at,qq=xxx]` 提及用户列表，支持 `@用户` 定向查询功能。

### 变更

- **L1 总结解析逻辑重构**：
  - 新增 `_strip_code_fences` 函数剥离 Markdown 代码块围栏（` ```json ... ``` `），支持小模型常用围栏包裹的 JSON 输出。
  - 总结 Prompt 重写为紧凑格式，强调"第一个字符必须是 `{`"、禁止使用代码块，减少 Token 消耗。
  - JSON 解析成功但 `memories` 为空时不再触发行式回退——此前会把 JSON 骨架行（`"memories": []`）当作记忆导入 L2。
  - 行式回退增加对 Markdown 代码块标记和 JSON 结构行（花括号、方括号、键名行）的跳过逻辑。
- **多 LLM Prompt 全面精简**：画像分析（群聊短期/长期、用户短期/长期）、知识提取、查询改写、记忆合并、模式挖掘等 Prompt 统一精简为紧凑格式，去除冗余说明文字，降低 Token 消耗。
- **画像消息截取修正**：群聊/用户画像分析的消息截取从 `messages[:max]`（取最早 N 条）改为 `messages[-max:]`（取最近 N 条），确保分析基于最新对话内容。
- **画像自定义字段合并阈值调整**：`merge_list_field` 的 `replace_threshold` 从 5 降为 1；`merge_custom_fields` 的 `existing_confidence` 从 0.5 降为 0.4，使中期更新（置信度 0.7）能覆盖已有字段（0.7 > 0.6 = True），此前 0.7 > 0.7 = False 无法覆盖。
- **用户画像 occupation 字段更新层级放宽**：从仅长期更新（`UpdateTier.LONG`）改为所有层级均可更新，使中期更新也能修正职业信息。
- **全局 CSS 样式覆盖修复**：多处 Vuetify 3 默认样式覆盖失效问题修复：
  - `.v-card` border-radius 添加 `!important`，覆盖 Vuetify 默认的 4px。
  - `.iris-card` transition 添加 `!important`，确保 `transform` 参与过渡。
  - `.iris-list-card` 选择器提升为 `.v-card.iris-list-card`（特异性 0,2,0），修复 `display: flex` 被 Vuetify 的 `display: block` 覆盖导致 flex 布局失效、列表无法滚动的问题。
  - `.iris-section-title` 的 `display` / `letter-spacing` 添加 `!important`。
  - `.iris-table` 的 `border` / `border-radius` 添加 `!important`。
  - G6 Tooltip 样式从 `L3GraphCanvas.vue` 的 scoped `:deep()` 迁移到全局 `iris-common.css`——G6 将 tooltip 渲染到 `document.body`，scoped 选择器（编译为 `[data-v-xxx] .l3-tip`）无法命中。
  - L3 侧栏卡片统一添加 `iris-card iris-card-hover` 类，移除冗余的 `:deep(.v-card)` 样式。
  - `L3NodeDrawer` 移除冗余的 `:deep` 样式，改用全局 `iris-list` / `iris-table` 类。
- **L3 列表面板高度约束**：`L3GraphView` 节点/边列表容器添加 `.l3-panel-container` 类（`height: calc(100vh - 200px)` / `display: flex` / `flex-direction: column`），建立高度约束使内部表格可独立滚动；小屏（≤1280px）降级为 `height: auto`。
- **L3 图谱检索种子节点群隔离**：图谱扩展检索的种子节点查询添加 `group_id` 过滤，防止跨群节点作为种子泄漏。
- **L3 图谱检索边跨层去重**：多深度扩展检索时使用 `seen_edge_keys` 集合去重，避免同一条边在不同 depth 的 frontier 中被重复查出。
- **L3 图谱格式化返回纳入节点 ID**：`GraphRetriever.format_graph_context` 返回值从 `str` 改为 `(str, set[str])` 元组，附带实际纳入文本的节点 ID 集合，供上游访问统计使用。
- **L3 图谱节点搜索 limit 透传**：`GraphRetriever._search_by_keywords` 使用传入的 `limit` 参数而非硬编码 5。

### 修复

- **persona_id 记忆导入导出人格隔离**：`MemoryEntry` 新增 `persona_id` 字段并透传至 L2 adapter 的所有查询/导出/导入路径。导入时逐条 `persona_id` 优先、文件级 `persona_id` 回退、再回退 `"default"`，避免模型迁移的"导出→删库→重导入"回合将所有人格记忆塌缩为 `"default"` 命名空间，永久破坏人格隔离。同时修复导入时 `skip_duplicates` 未正确映射为 `skip_dedup` 的问题——`skip_duplicates=False` 时相似记忆被 `add_memory` 静默丢弃。
- **L1 空总结段位误推进**：空总结（`summary_items` 为空）时不再推进段位。此前无论是否达到重试阈值都会执行 `rotate_after_summary`，使未写入 L2 的内容被丢弃、重试阈值形同虚设。
- **L1 总结后段位转移精确化**：`rotate_after_summary` 新增 `summarized_messages` 快照参数，基于快照按对象标识精确移除已总结消息，保留 `await` 期间新添加到 segment_2 的消息，避免误移除或误保留导致下轮重复总结。
- **L2 模型迁移空库跳过**：`_migrate_on_model_change` 在数据库未打开时先打开数据库再计数。此前 `_db` 为 `None` 时 `_count_db()` 返回 0，导致迁移被静默跳过、索引与元数据不一致。
- **L2 导出函数异步标注修正**：`MemoryExporter.export_to_json` 实为同步方法，`data_routes.py` 的 `await` 调用修正为同步调用。
- **画像 Web 更新并发丢失**：`ProfileStorage.update_group_profile` / `update_user_profile` 加入命名空间锁（`lock_group` / `lock_user`），避免 Web 路由直接调用时与消息驱动的 `update_from_analysis` 并发互相覆盖（lost update）。
- **图片配额重置死锁**：`ImageQuotaManager` 拆分 `_reset_quota_locked` 不加锁版，`check_quota` 持锁后调 `_reset_quota_locked` 而非 `reset_quota`，避免重入 `asyncio.Lock` 死锁。
- **图片解析 batch 异常配额泄漏**：`_parse_images_if_enabled` 中 `parse_batch` 抛异常时退还全部预扣配额、标记图片为 FAILED 并清占位符，避免配额泄漏和占位符残留。
- **L3 节点合并异常未回滚**：合并重复节点的异常路径添加 `self._db.rollback()`，避免半合并的 DELETE/INSERT/UPDATE 被下一个无关写入的 `commit` 一并刷盘造成节点/边不一致的半提交损坏。同时将 `try/except` 移入 `with self._db_lock` 内确保锁正确释放。
- **L3 properties JSON 损坏崩溃**：节点/边的 `properties` JSON 解析添加 try/except，损坏时回退为空字典而非崩溃，影响范围包括节点查询、边合并、已有边属性合并三处。
- **L3 图谱导入边跳过计数错误**：边导入失败时计入 `skipped_edges` 而非 `skipped_nodes`，此前计数归类错误。
- **L3 节点内容修正 source_memory_id 回退查询**：`update_node_content_by_source_memory` 在 `source_memory_id` 列未命中时，回退查 `properties.source_memory_ids`（批量提取的多来源节点），避免漏更新。同时支持传入 `new_source_memory_id` 重指向新记忆，避免 L2 记忆修正后 L3 引用断裂。
- **Token 统计重启丢失历史**：`record_usage` 在累计前先 `_load_from_kv` 回读未加载模块的历史数据；新增 `_known_modules` 集合登记模块名，`get_all_stats` 遍历已知模块从 KV 加载，避免重启后未触达模块不出现在统计中。
- **TaskScheduler 忙循环**：`interval_hours` 非正数时钳制为 1 小时，防止 `sleep(0)` 忙循环。任务覆盖时异步 `await` 旧任务清理（5 秒超时），避免 `cancel` 后不 `await` 导致旧任务清理未完成。
- **should_evict threshold 参数被忽略**：形参 `threshold` 优先于配置默认值，与 `should_evict_kg_node` 行为一致。此前形参被静默忽略，调用方传入的 `threshold` 被丢弃。
- **SaveKnowledgeTool confidence 越界**：节点和边的 `confidence` 钳制到 `[0.0, 1.0]`，防止 LLM 返回越界值经 `max()` 合并后被永久固化，破坏遗忘评分语义。
- **SearchMemoryTool 图节点反查错误**：此前从 L2 metadata 取 `source_memory_id`（L2 无此字段，该字段是 L3 图节点属性），改为用 L2 条目自身 `id` 通过 `get_node_ids_by_source_memory_ids` 反查 L3 图节点。
- **CorrectMemoryTool 节点关联断裂**：更新记忆时透传 `new_source_memory_id`，确保 L3 图节点的 `source_memory_id` 重指向新记忆 ID，避免旧记忆已删除但图节点仍指向已删 ID。
- **助手响应 persona_id 丢失**：`handle_llm_response` 添加助手消息到 L1 Buffer 时解析并传入正确的 `persona_id`，此前使用 `"default"` 占位污染人格命名空间。
- **指令解析 current_user 误写 target_user_id**：`CommandParser` 中 `current_user` scope 不再返回 `user_id`，否则 executor 会把它写入 `target_user_id`，使 scope 实际变为 `specified_user`，各 handler 的 current-user 分支成为死代码。
- **指令 profile 子命令 em-dash 变体**：`ProfileCommandHandler` 支持 em-dash 变体（`—group` / `—all`），与 `parser.SCOPE_FLAGS` 对齐。
- **隐藏配置 Web 更新无校验**：新增键名和值类型校验（int/float/str/bool），拒绝空请求体和类型不匹配的值，防止字符串污染下游算术与 `wait_for`。
- **Web API 空请求体崩溃**：`search_l2_memory`、`delete_l2_entries`、`update_l2_entry`、`delete_l3_nodes`、`delete_l3_edge` 等路由添加空请求体检查，返回 400 而非崩溃。
- **L3 列表 limit 无上限**：`list_l3_nodes` / `list_l3_edges` 的 `limit` 钳制到 `[1, 500]`，防止恶意大值导致内存/DB 过载。
- **未实现平台适配器崩溃**：平台已注册但适配器未实现时（如 `qqofficial` / `gewechat`）降级到 `GenericAdapter` 而非抛 `UnsupportedPlatformError`，避免钩子链无 try/except 兜底时每条消息崩溃。
- **梦境矛盾消解 timestamp 覆盖**：`update_content` 内部已把 `metadata["timestamp"]` 刷新为 `now`，随后 `update_metadata` 用 `keep_entry.metadata`（陈旧副本）整 blob 覆盖写回会把新 timestamp 覆盖回旧值。修复为先同步 `timestamp` 再写 `confidence`。
- **梦境时间锚定未纳入变更阶段**：`_PHASES_THAT_MUTATE_ENTRIES` 集合添加 `"temporal_anchor"`，确保时间锚定阶段正确参与变更标记。
- **知识提取全失败仍标记已处理**：提取结果非空但全部写入失败时不再标记为已处理，允许下轮梦境任务重试，避免永久跳过。
- **DashboardView flex 子项溢出**：`.memory-card :deep(.v-card-text)` 添加 `min-height: 0`，允许 flex 子项收缩，防止内容过长时溢出。

## [0.1.2] - 2026-06-28

> 本次更新主要尝试支持合并转发消息的解析与入队，同时附带前端管理界面（pages）的若干显示修复。

### 新增

- **合并转发消息支持（实验性）**：识别 OneBot11 协议的 `forward` 消息段，通过 `get_forward_msg` API 拉取合并转发内的所有子消息，并将其作为一条结构化消息入队 L1 Buffer。
  - 平台适配层新增抽象：`iris_memory/platform/base.py` 提供 `ForwardMessage` 数据类与 `get_forward_messages` 默认空实现；`iris_memory/platform/qq.py` 实现 OneBot11 兼容拉取（单个 resId 超时 10 秒，失败返回空列表）。
  - 子消息按 `[{用户名}]: {内容}` 格式拼接，统一包裹在「【合并转发内容】」标题下；按 `l1_max_single_message_tokens` 预算累加，超预算时停止累加并追加截断提示（预留 30 tokens 给前后缀与结构开销，下限 64 tokens）。
  - 入队消息携带 `forward` / `forward_total` / `forward_included` / `forward_truncated` 元数据，便于后续统计与回溯。
  - 合并转发中的图片同样会被提取，`source` 标记为 `forward`，纳入每日配额与去重逻辑。
  - 不同 OneBot11 实现对 `get_forward_msg` 支持程度不同，不支持或拉取失败时静默跳过，不影响主消息入队。

### 变更

- **前端图标体系调整**：应用主图标由 `mdi-brain` 更换为 `mdi-flower-tulip`（贴合 Iris 鸢尾花主题）；画像管理「性格特征」卡片图标更换为 `mdi-emoticon-outline`。
- **仪表盘顶部条样式简化**：移除 `iris-hero-card` 的主色渐变背景，改用普通 `iris-card`，避免与导航栏活动项渐变叠加造成视觉冗余。

### 修复

- **L3 图谱小屏幕布局重合**：重设三档响应式断点（1280px / 768px），小屏下侧栏改为固定高度并允许内部滚动，画布高度逐档递减；侧栏自身 `max-height` 由 `calc(100vh - 140px)` 改为 `100%`，交由父容器统一控制，避免与父容器冲突导致溢出重合。顶部统计条改用 flex 自动换行布局，小屏隐藏次要的「当前/总数」统计。
- **画像管理列表无法滚动**：ProfileView 比 L1BufferView 多一层 `v-window` 包裹，导致 flex 高度链条断裂、`overflow-y: auto` 失效；通过 `:deep()` 显式为 `.iris-list-card .v-card-text` 设置 `max-height` 触发滚动。同时在全局 `iris-common.css` 中为 `.iris-list-card > .v-card-text` 补充 `min-height: 0`，修复 flex 子项默认 `min-height: auto` 阻止收缩的通病，惠及所有使用该类名的列表。
- **导航栏活动项残留渐变底**：移除品牌头部 `linear-gradient` 背景；为 `.v-list-item--active` 强制纯色高亮并禁用 Vuetify 默认的 `::before` / `__overlay` 渐变层。
- **L3 控件调整误触发主节点重置**：调整深度 / 最大节点数时改用新增的 `refreshL3Graph()`（保留当前主节点），仅「随机主节点」按钮调用 `fetchL3Graph()` 重新随机；侧栏原「重新加载」按钮更名为「随机主节点」并更换为 `mdi-shuffle` 图标。
- **ProfileView 用户列表 active 状态错乱**：同一 `user_id` 出现在多个群组时所有匹配行同时高亮，修复为同时校验 `user_id` 与 `group_id`。
- **L3 列表面板样式不一致**：`L3NodeListPanel` / `L3EdgeListPanel` 补齐 `iris-card` / `iris-section-title` / `iris-table` 类与统一空状态；`L3Sidebar` 分区标题补齐 `iris-section-title`，`border-radius` 改用全局 `--iris-card-radius` 变量。
- **L3NodeDrawer 脆弱 CSS 选择器**：`.pa-3.d-flex.gap-2` 依赖模板类名顺序，改用语义化类名 `.drawer-actions`。

## [0.1.1] - 2026-06-24

> **⚠️ 重要：本次更新需要重启 AstrBot，否则可能会导致插件加载失败。**
>
> 原因：本版本新增了 `on_llm_request` 钩子中的对话上下文清理逻辑与 UI 偏好设置 Web 路由，二者均在插件加载阶段注册；若不重启 AstrBot 使插件重新加载，新钩子与路由不会生效，且可能与已运行的旧实例产生状态不一致。

### 新增

- **L2 FAISS 索引定期 checkpoint**：新增隐藏配置 `l2_checkpoint_writes`（默认 50），L2 索引每 N 次写入异步落盘一次，把崩溃丢失窗口从「自启动以来全部增量」收敛到「最近 N 次写入」（0=禁用，仅关闭时保存）。此前 FAISS 索引仅在 shutdown 时整体持久化，运行期进程崩溃会丢失全部向量增量，而 SQLite 走 WAL 即时耐久，导致重启后 SQLite 有记录但 FAISS 缺向量、索引与元数据不一致。
- **UI 偏好设置 API**：新增 `iris_memory/web/routes/ui_preferences_routes.py`，将前端 UI 偏好（如深色模式）持久化到后端 JSON 文件。AstrBot 插件页面以 iframe 嵌入 Dashboard，受浏览器安全策略限制 `localStorage` 不可用（sandbox / 第三方存储分区），故改由后端存储，前端通过 bridge API 读写。
- **L2 记忆分页**：L2 记忆列表接口新增 `offset` 参数与 `total_count` 返回，前端 L2MemoryView 实现完整分页功能。

### 变更

- **对话清理策略重构（重大）**：新增隐藏配置 `enable_legacy_cleanup`（默认 `False`）。
  - **默认策略（推荐）**：在 `on_llm_request` 钩子中清空 `req.contexts`，保留对话 ID，使 AstrBot 主动回复（active_reply）能通过 `get_curr_conversation_id` 正常检测到对话存在；Agent 完成后对话历史由 AstrBot 正常保存，下次请求时再次清空。
  - **旧版策略**：在 `on_agent_done` 钩子中调用 `conversationManager.delete_conversation` 删除整个对话，会导致主动回复因 `get_curr_conversation_id` 返回 `None` 而失败，仅作为隐藏参数保留。
  - 两种策略均受 `context_control.enable_conversation_cleanup` 配置控制。
- **图片解析 SSRF 防护增强**：新增 `_GlobalOnlyTransport`（包装 httpx transport，在连接前再次强制校验所有 DNS 解析结果为全局地址）与 `_fetch_image_data_url`（下载图片转为 base64 data URL）。可达性检查与实际下载各自独立解析 DNS，存在 DNS rebinding 窗口（检查时解析到公网、下载时被切到内网），新方案在下载连接前再次强制校验，堵死向内网的 rebinding；同时图片字节转为 data URL，使 LLM provider 不再直连外网 URL。新增 10MB 大小限制，避免大图撑爆 LLM 上下文。
- **L2 检索超时范围扩大**：embedding 计算纳入 `l2_timeout_ms` 超时控制，避免 embedding provider 卡死时在 `on_llm_request` 会话锁内无限挂起。
- **L2 统计查询异步化**：`get_stats` 改走线程池（`asyncio.to_thread` + 新增 `_db_fetchone` / `_db_fetchall` 持锁取数），避免在事件循环线程持同步锁、与 executor 中的长 FAISS 检索争用而阻塞整个插件。
- **遗忘算法简化与修正**：
  - L2 遗忘评分移除恒不触发的 `D > 0` 分支（L2 记忆无 `connected_count`，`calculate_isolation_degree` 恒返回 0），权重统一从隐藏配置读取；
  - KG 验证度 V 由 `min(1.0, source_memory_count / 5)` 改为对数曲线 `log(source_memory_count + 1) / log(6)`（`source_memory_count` 为 0 时取 0），使少量来源即可获得较高验证度，与「`source_memory_count >= 3` 永不淘汰」保护阈值相配合。
- **bot 人格隔离配置说明修正**：`enable_persona_isolation` 的 hint 由「切换会导致记忆、用户画像重建」改为「不同 bot 人格的记忆与画像按人格 ID 逻辑隔离（共享底层存储、命名空间互不可见）；切换人格仅改变可见数据，不删除或重建」。
- **L1 总结解析日志增强**：`parse_summary_response` 新增 `json_parsed` 标记区分 JSON 解析与文本回退；JSON 解析失败时的警告补充模型未按 JSON 输出的提示与更换模型建议；文本回退仅提取到少量记忆时额外输出警告。

### 修复

- **L2 记忆更新并发错乱**：`update_memory` 在嵌入计算（锁外）完成后、写回 FAISS 前，重新校验 `faiss_idx` 归属。此前该记忆可能被并发 `delete_entries` 删除且槽位被 `add_memory` 经 free-list 复用，直接写回旧 `faiss_idx` 会误删并覆盖复用方的新向量，导致 FAISS 与 SQLite 错乱；校验不一致则放弃本次更新。
- **L2 checkpoint 配置未就绪崩溃**：`_mark_dirty` 读取 `l2_checkpoint_writes` 增加异常兜底，配置系统未就绪（初始化早期或测试环境）时仅标记脏、跳过 checkpoint，不再抛异常。
- **前端主题偏好丢失**：修复 Vue 响应式对象跨 bridge 序列化失败问题；主题存储改用后端 UI 偏好 API，适配 AstrBot iframe 嵌入环境下 `localStorage` 不可用的问题。

## [0.1.0] - 2026-06-19

### 安全

- **修复图片 URL 的 SSRF 漏洞**：图片可达性检查新增主机安全校验（仅允许 http/https，通过 DNS 解析拒绝私网 / 环回 / 链路本地 / 云元数据等非全局地址），并禁用自动重定向，防止恶意用户构造图片 URL 探测内网。
- **修复跨群隔离越权**：
  - `save_knowledge` 工具现在按群记忆隔离绑定 `group_id`，与 `search_knowledge_graph` 对齐，避免跨群写入污染其他群的知识图谱；
  - `correct_memory` 的群隔离校验改用正确的记忆隔离配置键 `enable_group_memory_isolation`。
- **修复异常信息泄露**：Web API 与管理指令不再把 `str(e)`（可能含文件路径、SQL/Cypher 片段）回显给客户端，统一返回通用错误消息，详情仅写入服务日志。

### 修复

- **`correct_memory` 误删与数据丢失**：原先用语义检索定位 `memory_id`（几乎必然误命中不相关记忆）、且「先删后写」无回滚。现改为按 ID 精确查询（L2 adapter 新增 `get_entry_by_id`）+「先写后删」，确保写入失败时原记忆不丢失。
- **画像并发丢失更新**：引入 `@profile_lock` 装饰器，按 (persona, group, user) 命名空间串行化所有画像「读-改-写」操作；画像索引更新加独立 `_index_lock`。
- **L2 记忆去重竞态（TOCTOU）**：去重检查与 FAISS / SQLite 写入并入同一锁临界区（新增 `_find_similar_unlocked`），消除「检查通过→释放锁→另一并发写入→重新取锁」的竞态，同时避免原先的重复嵌入计算。
- **图片解析任务泄漏**：超时取消的 pending 任务现被正确 `await`，避免任务泄漏与「Task was destroyed but it is pending」警告，确保 httpx 连接及时回收。
- **图片解析配额静默耗尽**：新增配额退还机制（`QuotaStatus.release` / `ImageQuotaManager.release_quota`），解析失败 / 跳过 / 超时后按实际失败数回补预扣配额。
- **组件系统状态重建竞态**：`SystemStatus` 改为先在局部构建完整对象再原子替换，避免后台组件并发完成初始化时读到半重建状态（如某组件已就绪却被报不可用）。
- **初始化异常静默降级**：`initialize_components` 在异常路径下仍尝试启动不依赖后台组件的定时任务，避免调度任务完全不注册。
- **`ImageCacheManager` 索引竞态**：缓存元数据索引（`hashes` 列表）的「读-改-写」加 `_lock` 保护，避免并发丢失更新。
- **L2 `delete_collection` 未持锁**：数据库关闭与目录清理纳入 `_lock` 保护，避免与并发检索 / 写入交错触发 SQLite 错误。
- **Dream 模块逻辑错误**：
  - 矛盾消解的 `NO_CONFLICT` 判定改为前缀匹配，避免正文包含该子串时误判为无矛盾；
  - 模式挖掘启用此前被忽略的 `dream_pattern_min_confidence` 配置阈值过滤低置信度模式。
- **Token 预算估算偏小**：L2 检索的 Token 估算改用 `count_tokens`，修正原先 `len//2+1` 对中文严重偏小导致预算超用。
- **指令误触发**：`/iris_mem` 指令前缀改用单词边界匹配，避免正文出现「iris_memory」等子串被误判为指令。
- **隐藏配置默认值不一致与热修改盲区**：
  - `forgetting_threshold_kg` 代码 fallback（0.3）与 dataclass 默认（0.2）矛盾，统一为 0.2；
  - `profile_max_messages_for_analysis` 此前群聊 / 用户画像共用同一键、fallback 不一致（50/30），拆分为 `profile_max_messages_for_analysis`（群聊，50）与 `profile_max_messages_for_user_analysis`（用户，30）；
  - `l2_similarity_threshold` 此前仅在 L2 adapter 初始化时缓存，运行时热修改不生效；改为去重时实时读取，恢复「支持热修改」承诺。

### 变更

- **L2 / L3 架构轻量化（重大）**：
  - **L2 记忆库**：由 ChromaDB 迁移到 **FAISS（向量索引）+ SQLite（向量元数据与文本）**，去除 ChromaDB 自带的重型传递依赖，向量维度与召回行为不变。
  - **L3 知识图谱**：由 KuzuDB 迁移到 **SQLite**（关系模型表达节点和关系），去除 KuzuDB 的 C++ 原生扩展依赖，安装包体积与启动时间显著下降。
  - 配套 `2911125`：FAISS + SQLite 的复合操作改用 `RLock` 替代 `Lock`，确保跨组件线程安全。
  - 配套：精简 `CorrectMemoryTool` 节点更新逻辑；`SearchKnowledgeGraphTool` 改用新的 `search` 方法；L2 adapter 新增 `get_entry_by_id` 精确查询能力。
  - 测试与依赖同步更新：`tests/l2_memory/test_adapter.py` 调整用例，`requirements.txt` 移除 `chromadb`、`kuzu`，新增 `faiss-cpu`。
- **统一原子持久化**：新增 `iris_memory/utils/persistence.py`（写入临时文件 → `fsync` → `os.replace`），替换 L2 导出 / 导入、隐藏配置、L2 索引元数据等处此前的非原子覆盖写，避免写入中途崩溃导致文件截断与数据丢失（部分加载逻辑此前会静默丢弃全部数据回到默认）。
- **隐藏配置系统优化**：
  - 默认值调整：`l1_inject_max_content_chars` 200→300（减少注入截断丢信息）、`l2_similarity_threshold` 0.90→0.87（减少重复记忆堆积）、`image_max_parse_per_request` 5→3（与 `image_max_concurrent_parse` 对齐，形成「每批 3 张」统一批次语义）；
  - 硬编码可配化：将遗忘评分权重（L2 四项 + KG 四项）、L2 保留天数 `l2_retention_days`、KG 衰减系数 `forgetting_lambda_kg`、合并检索 `dream_consolidation_query_top_k` 共 11 项原硬编码魔法数字提升为隐藏配置项；
  - 死代码清理：移除从未被读取的预留配置 `l2_max_entries`、`l3_max_nodes`、`l3_max_edges`。

### 开发与质量

- `requirements.txt` 修正：移除代码未使用的 `PyJWT`，补充实际依赖但未声明的 `httpx`。
- 全量通过 `ruff check` 与 `ruff format`（共修复 62 处 lint 问题）。
- 新增与本次改动的代码通过 `pyright` 类型检查（0 错误）。
- 单元测试与组件测试 **617 项全部通过**。

### 已知技术债

- 全项目 `pyright` 仍有约 195 个既有类型标注告警（Optional 未判空、参数类型、Dict 当对象访问等），为长期累积、不影响运行时，将作为独立技术债迭代。
