# 模型目录、版本选择与真实 RVC 参数

## 真实参数与执行链

参数的唯一机器可读定义为 `packages/contracts/rvc-parameters.schema.json`；由 `scripts/generate_contracts.py` 生成 TypeScript/Python 类型，前端通过 `/api/v1/rvc/parameters` 获取当前模型和参数下的合法范围、默认值、来源、推荐依据和禁用原因。界面保留原有外观与六阶段导航，基础/高级/诊断展开状态保存在工程视图偏好中。

当前开放的实际路径包括移调、已验证索引/检索比例、清辅音保护、音量包络、已训练 Speaker ID、现有 F0 算法/音域/原生阈值/静音门控、目标曲线平滑和自动靠近半音、预览上下文、CPU/CUDA、固定 float32、种子以及已有 noisereduce 后处理。固定帧长、训练编码器和未验证的共振峰/Proposed Pitch/按静音重拼接/其他精度不会伪装成任意可调的通用能力；面板明确给出限制。

- `protect=0.5` 关闭保护，越小保护越强。检索为0时该控制禁用；执行时也把原生保护分支关闭，避免无意义浮点混合产生细微差异。
- `volume_envelope=0` 更接近源动态，`1` 保留转换动态。建议只是可解释起点，不是音质分。
- F0 顺序固定为原始检测 → 连续有声段中值平滑 → 自动靠近半音 → 全局移调 → 手工编辑 → 绝对锁定快照。实际生成器仍消费外部 F0，不内部重新检测覆盖。
- 原始 F0 缓存只依赖源和实际分析配置；换 Epoch、检索或包络不重算检测。改变目标控制时轻量重建目标层。缓存跨引用/声部使用时重新绑定元数据，保留真实原生产者。
- 每个候选保留请求参数、实际生效参数、未生效原因、模型/run/Epoch/索引哈希、源引用及 requested/consumed F0 哈希。`auto` 设备与实际执行设备分别记录，采纳试听设置恢复请求意图而非把运行时结果冒充用户选择。

## 同源版本对比与采纳

`compare_versions` 接受同一训练实验的2–8个兼容且内容不同的导出，固定同一输入、选区、分析产物、目标 F0 与种子，串行生成候选并检查实际请求/消费 F0 哈希一致。模型复制品不冒充多个 Epoch。预览不会自动替换整段成品。

试听支持原声或另一同源候选作为A通道，保持统一播放头；等响只使用相同范围的实际RMS。短原声参考由实际裁剪得到。盲听隐藏模型/版本并打乱展示顺序。备注、收藏和视图偏好保存到工程。

“采用此试听设置”恢复版本和合法请求参数，随后可完整生成；“局部采用到完整版”必须选择同源完整转换基底，按真实时间映射与线性交叉淡化插入短候选，区间外保持基底音频，不把短片段直接当全长成品。

工程/轨道/区间的覆盖通过明确接口保存不可变版本哈希。轨道与区间只覆盖其提供的参数，支持恢复继承。完整生成遇到区间覆盖时需要明确忽略覆盖或逐段渲染后采用，不静默丢弃覆盖。

## 模型目录、训练实验与版本注册

本节对应模型注册模块，验证日期为 2026-10-31。模型目录与工程数据共享同一 SQLite 文件，使用独立 `catalog_meta.schema_version` 和 `catalog_*` 表，不修改主工程的 `PRAGMA user_version=1`。首次迁移通过 SQLite backup API 保存一致性快照到数据库旁的 `backups/catalog-before-v1-*.sqlite`；原有工程、候选、模型 ID 与模型哈希不被重写。

**发现、验证、推理是不同状态。** 默认只读扫描 `E:/voice/Applio/logs` 及其直接子目录，读取文件属性、受限 JSON 元数据和 PyTorch ZIP 中的 `data.pkl` 描述，不读取 tensor storage，不导入 checkpoint 中的 Python 对象，不加载所有模型。`workers/catalog_probe.py` 以有限 pickle opcode 解释器提取纯数据和符号化张量形状；`GLOBAL` 与 `REDUCE` 仅检查，不调用。未知全局、非 ZIP 旧权重、越界 ZIP 成员、超预算元数据和不支持的结构会隔离，绝不退回不受限 pickle。

扫描默认预算为 5,000 个候选文件、30 秒；最大允许 10,000 个文件、120 秒、512 个直接子目录。单文件不超过 2 GiB，ZIP 元数据与目录预算各 4 MiB，opcode 预算 300,000。符号链接越界不跟随。达到预算或取消只保留已发现记录，不能把未访问的文件标成缺失。启动/首次目录打开可调用 `ensure_initial_scan()`；同数据库的并发扫描请求合并为当前作业。之后的显式扫描会增量检查属性，未变化的文件不会重新解析或计算完整哈希。

选中模型后调用 `validate()` 或 `resolve_model()`，才计算 SHA-256，并在独立 CPU 子进程中执行静态检查与 `weights_only=True` 受限加载；验证 RVC 必要 tensor、内容维度、采样率和训练元数据。索引也在选中时完成 CPU FAISS 结构检查与哈希计算。环境固定复用已有解释器，没有下载、安装、升级或训练。该子进程是稳定性隔离，不被称为完整操作系统安全沙箱。

| 实体 | 身份与证据 |
| --- | --- |
| `VoiceProfile` | 用户别名、说明、标签、收藏、最近使用和默认版本。初始声音分组明确标记 `filename_inferred`，不是对声音身份的自动确认 |
| `TrainingRun` | 原始实验路径、配置与 `model_info.json`。允许同一个声音关联多次实验；既有实验身份保持稳定 |
| `ModelArtifact` | 每个推理导出独立登记，包含 `epoch/global_step`、来源、哈希、架构、编码器、训练说话人及状态。不能识别 Epoch 时保留 `null`，不会丢弃有效导出 |
| `TrainingCheckpoint` | G/D、优化器等训练状态单独列出，不能提交推理。`iteration` 和文件名计数分别保存为 `training_iteration`、`filename_counter_hint`，没有可信适配器时不猜测它们等于 Epoch 或 Step |
| `IndexArtifact` | 独立索引 ID、维度、哈希、训练来源与关联证据。文件名相同或位于同目录本身不足以成为已验证关联 |
| `ModelSelection` | 工程/轨道/片段的具体模型 ID、哈希、索引 ID/哈希、speaker ID 与预设引用。修改声音默认版本不会修改已有工程绑定 |

推理导出的 Epoch/Step 优先读取 checkpoint 的标量，来源标为 `checkpoint`；仅有文件名线索时标为 `filename_inferred`。模型 `creation_date` 与文件时间保持各自含义，不把目录排序解释成音质排名。

说话人数量与 embedding 容量分开。当前 Twilight 导出及实验元数据声明一个训练说话人，因此 `speaker_ids` 和 `speaker_ids_trained` 为 `[0]`，虽然 `embedding_speaker_capacity` 为 109。后者不代表存在 109 个已训练目标声音。内容编码器也从 checkpoint/训练元数据读取；未知或当前执行器不支持时给出阻塞原因，不静默按 ContentVec 处理。

自动索引复用需要现有登记/用户明确绑定作为来源基点，再核对新版本内嵌的实验标识、同实验关联、编码器与特征维度。原有 29 份注册模型的索引关联及哈希作为兼容基线保存。全新目录中仅凭 `run_name.index` 不会自动标为匹配，用户可显式绑定；显式绑定仍不能绕过维度/编码器不兼容或文件哈希改变。`index_match` 保存来源基点与验证证据，`fully_validated` 区分目录元数据检查与选中后的实际 FAISS 读取。

文件发生变化时旧记录进入 `quarantined`，旧哈希和历史引用保留；再次扫描创建新版本身份。重新链接只接受与原哈希一致的文件。按哈希去重时保留每个旧 ID 和原路径，通过 `alias_of/canonical_id` 表示引用，同实验复制品默认折叠，不删除历史行。不同实验中的相同内容仍可作为各实验的引用显示。

### API 与接入

`make_catalog_router(store)` 复用现有本机会话认证与 Origin 校验，路由前缀为 `/api/v1/catalog`：

- `GET /voices`、`GET /runs`、`GET /models`：分页、声音/实验/状态/类型筛选、名称/别名/标签/Epoch/Step/哈希搜索；`models` 支持 `sort=version|recent|name`。
- `GET /models/{id}`、`GET /models/{id}/indices`：只读元数据与索引兼容诊断，不触发大模型加载。
- `POST /scan`、`GET /scans/latest`、`GET /scans/{id}`、`POST /scans/{id}/cancel`：有界增量发现及取消。
- `POST /models/{id}/validate`：选中权重的惰性哈希与受限 CPU 验证。
- `POST /models/{id}/bind-index`、`POST /models/{id}/relink`、`POST /import-batch`：明确绑定、精确哈希重连、最多 100 个显式选中文件的批量导入。
- `PATCH /voices/{id}`、`POST /voices/{id}/default`：用户元数据与默认版本。`last_used_model_id` 仅供新选择时建议，不能覆盖已保存的具体版本。

Python 集成使用 `ModelCatalog(store.path).get_model(id)` 读取轻量信息，使用 `resolve_model(id, index_id=None)` 获取运行清单。`index_id=None` 使用已登记/来源已验证的默认关联；`index_id=''` 明确不使用检索，适用于用户选择 `index_rate=0` 的路线。执行器接收到的清单仍含原 `id/path/model_hash`，同时增加声音、实验、Epoch/Step、版本 ID、训练说话人与索引证据。

共享定义在 `packages/contracts/model-catalog.schema.json`；运行 `.venv/Scripts/python.exe packages/contracts/generate_model_catalog.py` 生成 TypeScript 与 Python 类型，文件首行记录源 schema SHA-256。

### 本机验证

目录静态扫描实得 **180 份推理导出、29 份 Index、10 份训练 checkpoint，共 219 个文件**。Twilight Sparkle 的 `twilight_sparkle_pony_singing_r18m_20260914` 实验真实包含 Epoch **200、225、250、275、300**，对应 Step **11600、13050、14500、15950、17400**，均从 checkpoint 标量读取。没有把最新模型复制成多个版本。

`tests/test_model_catalog.py` 与 `tests/test_model_catalog_api.py` 覆盖恶意 pickle 不执行、ZIP 路径/大小限制、全 Epoch 发现、训练资源分离、有界/取消/合并扫描、变化文件隔离、哈希去重、原身份保持、索引来源与维度、默认版本不改工程绑定、迁移备份和 HTTP 形状。

真实 CPU 检查入口为 `.venv/Scripts/python.exe -m tests.test_model_catalog_real --run-real-metadata`。它在独立验证数据库中检查真实 Epoch 200/225 的不同模型哈希和同一匹配索引，核对原文件前后 SHA-256、29 个旧 ID/哈希及主数据库版本保留。证据保存在 `tests/results/catalog/verification.json`。这项检查不运行神经音频，不替代真实多 Epoch 音频对比和听感判断。
