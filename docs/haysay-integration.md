# Hay Say M6 接入与清理验收

SONORA 角色 RVC 声库仅保留 Hay Say 六角色：Twilight Sparkle、Rainbow Dash、Fluttershy、Rarity、Pinkie Pie、Applejack。每个角色登记一份推理权重及原仓库配套索引。

权重目录：`resources/models/haysay/`。注册清单：`engine-envs/haysay-models.json`。默认扫描目录、资源 bootstrap、真实模型检查及旧工作台均使用此清单，不再从旧训练目录补回角色模型。

每套资源固定仓库 revision、权重/索引 SHA-256、来源链接和本地验证的 speaker 0。模型保留原始权重字节；使用 hash-pinned inference profile 提供运行所需元数据，不修改 checkpoint。embedding 容量 109 不作为已训练说话人数。Fluttershy 输出 40kHz，其余角色 48kHz。

六角色通过正式 API/worker 的真实人声转换，实际使用各自 FAISS 索引，effective index_rate=0.5，并核对 worker 记录的索引 SHA-256。

回归检查：54 passed、2 skipped；前端 production build、浏览器真实模型列表和页面无异常检查通过。模型目录为 6 份权重、6 份索引；项目工作库中不存在其他 RVC 权重/索引，待清理项为 0。

保留 16 个工程、199 个音频产物、190 条任务、715 条历史。旧候选的 Epoch 标题已改为“历史音频”；历史音频没有被标成 Hay Say 生成。169 个已存在音频文件重新计算 SHA-256，与原记录一致。

旧测试权重、独立 QA 数据库、旧目录快照和过时验收报告移至工作库之外的 `E:/voice/maintenance/sonora_haysay_20261104/retired-evidence/`。迁移前一致性数据库为同目录上级的 `sonora-before.sqlite`。这些审计/回退资料不在 SONORA 模型扫描范围。

清理范围为 SONORA 工作库及相关实时配置。独立训练研究与已发布对比站点属于独立任务，未作为 SONORA 声库保留或接入。通用分离、音高、ContentVec 和 SVS 运行依赖继续使用。

服务： http://127.0.0.1:8765 。模型列表可直接选择 Hay Say 六角色。完整机器验收记录：`engine-envs/haysay/verification.json`；真实推理记录：`runtime/haysay-acceptance/verification.json`。

本次没有自动审批拒绝；无需额外手工清理命令。
