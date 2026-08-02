实施计划：AI 功能嵌入（Tier 1 + 缓存 + 修复 + Agent 诊断修复闭环）
总体架构决策
新增 backend/ai/service.py（AiService） 作为统一入口，5 个现有端点 + 新端点全部改走它：
路由层 → AiService.invoke(endpoint, user_id, prompt) 
         → ① 查 ai_cache（命中直接返回）
         → ② DeepSeekClient.chat()
         → ③ 落库 ai_usage（tokens/耗时/是否命中缓存）
         → ④ 写缓存（TTL 24h）
两张新表（放 experiment.db，与 ai_diagnosis 同库，建表逻辑集中在 main.py）：
- ai_cache：cache_key TEXT PK（sha256 of 端点+规范化输入+数据版本）、response、created_at、expires_at
- ai_usage：user_id、endpoint、model、prompt_tokens、completion_tokens、total_tokens、latency_ms、cache_hit、created_at
Phase 0 — AI 基础治理（前置，约 0.5 天）
任务	文件	说明
A1 缓存	新建 backend/ai/cache.py、main.py 建表	key 含数据版本：analyze 用实验 completed_at，market-insight 用最新 NAV 日期，避免脏缓存
A2 用量落库	backend/ai/client.py（chat() 返回 usage）、service.py	现有 client 只打日志丢弃 usage，改为返回
Phase 1 — Tier 1 五个嵌入点（约 1.5-2 天）
任务	后端	前端
B1 AI 回测分析	改走 AiService	ExperimentDetailPage 加"AI 分析"按钮+结果卡片（completed 时显示）
B2 失败诊断闭环	① main.py:876 改存 traceback.format_exc() 完整堆栈 ② 失败分支自动触发诊断（有 API key 时 fire-and-forget，写 ai_diagnosis）	详情页加"AI 诊断"按钮（未自动生成时可手动触发）；JobCenterPage 失败 job 加诊断入口
B3 AI 调参建议	修契约：prompt 改 JSON 输出，用 DeepSeek response_format={"type":"json_object"} 返回 suggestions: [{param_name, current_value, suggested_value, reason}]，与前端类型对齐	ExperimentNewPage 参数步骤加"AI 建议"按钮，逐条展示+一键应用
B4 信号解释	无需改动	services/ai.ts 补 explainSignal 封装；SignalPanelPage 每行加"解释"弹层（填充 reasoning 空列的位置）
B5 AI 市场解读	修占位符：api/ai.py:281 的 industry_exposure 改为复用行业分类数据计算真实持仓行业分布	PortfolioManagerPage overview 分区加"AI 解读"卡片
Phase 2 — 顺带修复（约 1-1.5 天）
任务	范围
C1 benchmark NULL	① akshare_source 新增指数日线方法（akshare stock_zh_index_daily）② 指数行情缓存 ③ 池→基准映射：csi300→000300 / csi500→000905 / csi800→000906 / csi1000→000852 / all_a+custom→000300（默认）④ _run_experiment 计算基准净值写 equity_curve.benchmark，使 metrics.py 已有的 alpha/beta/IR 计算自动生效
C2 假数据模型卡	ExperimentDetailPage:807-817（硬编码 v1.2.0/3h24min/sharpe×0.85）改为调已有的 GET /{id}/models，展示真实 train_metrics 关键项 + feature_importance Top 10
Phase 3 — AI Agent：失败全链路诊断 + 策略修复闭环（用户额外需求，约 1.5-2 天）
核心原则：AI 只提议，人类做决策。绝不无确认自动改代码。
任务	设计
D1 诊断升级为结构化全链路分析	ERROR_DIAGNOSIS_PROMPT 升级：上下文 = 完整堆栈 + 实验配置 + 定位报错策略文件/行并截取源码片段 + 数据覆盖情况。JSON 输出 {category, root_cause, evidence, fix_suggestion, auto_fixable}，category ∈ strategy_interface / strategy_code / data / params / environment / unknown。ai_diagnosis 列存文本摘要保持向后兼容，结构化结果存新列或随响应返回
D2 修复提议 POST /api/ai/propose-fix	仅当 category ∈ {strategy_interface, strategy_code} 时前端可调。上下文 = 策略完整源码（单文件模块，百行级，token 可控）+ 堆栈 + base.py 的 StrategyProtocol 契约 → LLM 输出修复后完整源码（比 unified diff 更可靠）+ 变更说明
D3 应用修复 POST /api/ai/apply-fix	流程：写 .bak 备份 → 覆盖策略文件 → 触发 Registry.scan() 热加载 → validate_params 冒烟 → 任一失败自动回滚 .bak。成功后可选"继承配置重跑实验"。第一版限 admin 权限
D4 前端修复确认 UI	诊断卡片显示分类标签 → "AI 修复"按钮 → diff 预览弹窗（不引重依赖，行级简单高亮）→ 确认应用 → 结果反馈 + "重跑实验"按钮
安全红线：propose/apply 需 ai:use + 实验所有者（apply 限 admin）；永远人工确认；.bak 兜底回滚；策略改动也可被 git 审计。
验证方案
- 后端：补 mock DeepSeek 的行为测试（缓存命中/未命中、用量落库、suggest-params JSON 契约、apply-fix 回滚路径）；ruff check + pytest backend/tests/ tests/integration/
- 前端：tsc -b --noEmit + AI 卡片组件 vitest + npm run build
- 端到端手动：起服务 → 故意造一个策略接口错误跑实验 → 验证 自动诊断 → propose-fix → diff 确认 → apply → 热加载 → 重跑成功 的完整闭环
- 文档：ROADMAP.md 修正 #67 虚报、勾选 #68-71、新增 Phase 3 条目；docs/API.md 补 3 个新端点
工作量与批次
批次	内容	估计
第一批	Phase 0 + Phase 1（缓存+用量+5 嵌入点）	~2-2.5 天
第二批	Phase 2（benchmark + 模型卡）	~1-1.5 天
第三批	Phase 3（Agent 诊断修复闭环）	~1.5-2 天
建议按批次顺序执行、每批独立验证提交。
两个待确认的小决策：
1. all_a / custom 池的基准默认用沪深300（000300）可以吗？还是这些池暂不写 benchmark？
2. apply-fix 第一版限 admin——考虑到开发环境首个用户即 admin，个人使用场景下等于不受限，可以吗？