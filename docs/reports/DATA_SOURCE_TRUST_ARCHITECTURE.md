# 股票数据来源可信度与交叉验证方案

> 审查日期：2026-07-29
> 结论：当前 AKShare/东方财富/新浪链路仅能作为公开聚合研究数据，
> 即使交叉验证通过，也不能升级为交易所权威或实盘认证数据。

## 1. 本轮确认的风险

1. 原 `AKShareSource` 在东方财富请求失败后，对单只证券静默回退新浪。
   合并后的 DataFrame 不记录逐代码实际端点，缓存却统一声明为 qfq，
   无法证明同一股票池使用了相同来源和复权口径。
2. `689*` CDR 使用 `stock_zh_a_cdr_daily`，该端点没有 `adjust`
   参数，但结果此前仍会进入声明为 qfq 的股票池缓存。
3. 缓存 schema v3 证明了增量重叠区间没有变化，却没有记录网络请求、
   实际 provider、endpoint、失败代码或交叉验证证据。
4. 行业列表的模糊列匹配会让“板块代码”覆盖“板块名称”，造成
   `code/name` 同为 `BK1298` 一类值。网络失败时又把申万 801xxx
   静态列表交给东方财富行业成分端点，分类身份不一致，可能得到空映射。
5. 当前指数成分仍不是 point-in-time；公开行情交叉验证不能消除
   幸存者偏差、证券生命周期缺失或历史可交易状态缺失。

## 2. 已落地的安全协议

### 请求级证据

`daily-fetch-evidence/v1` 对以下内容生成 SHA-256：

- provider 和具体 endpoint；
- 复权语义与证据等级；
- 请求日期、请求代码；
- 实际返回代码、失败代码、frame 内容摘要；
- 可选的独立来源交叉验证结果。

缺失证券不会被静默删除，而会进入 `failed_codes`。
这里的 SHA-256 只是内容完整性校验，不是供应商签名，不能证明网络响应
的法律身份或阻止有写权限者重写证据后重新计算哈希。运行时证据等级还
受代码内适配器注册表上限约束：未注册适配器最多只能声明 `declared`，
AKShare 东财/新浪最多只能声明 `public_aggregator`。

### 独立来源验证

`CrossValidatedDailySource` 顺序读取两个具有不同 provider 身份的来源。顺序读取是
必要的进程安全边界：AKShare 的部分端点依赖 `mini_racer`，并发原生调用可能直接
终止解释器。两个响应仍分别保留独立来源身份，并经过相同的交叉验证门禁。
它不会自动拼接、投票或修正价格，只在验证通过后返回主源结果。

前复权绝对价格可能因为锚定日期不同而存在常数比例，因此比较每日收盘
收益，而不是直接比较 qfq 绝对价格。以下情况整体失败：

- 两个来源身份相同；
- 复权语义不同；
- 任一代码重叠收益少于策略阈值；
- 收益差异超过容差或冲突率阈值；
- 验证证据哈希或汇总统计被篡改。

生产研究更新路径默认使用 AKShare 的东方财富和新浪两个公开上游；
相同上游即使包成不同 Python wrapper 也会被拒绝。这两个端点仍可能
共享上游错误，因此只是公开数据异常互检，不是权威供应商独立性证明。

这个检查可发现端点异常、复权断点和部分公司行动分歧，但不能证明两个
聚合源没有共享相同的上游错误。

### 缓存血缘

`cache-source-provenance/v1` 把所有网络批次和最终合并 frame 绑定。
日线缓存 schema 升级为 v4。schema v3 及更早缓存会 fail-closed，
必须受控 `force refresh`，不会自动继承为可信缓存。

不同 provider 或不同 adjustment 的批次不能进入同一个缓存身份。
缓存状态明确区分：

- `exchange_authoritative`
- `licensed`
- `public_cross_validated_research_only`
- `public_single_source_research_only`
- `declared`
- `unverified`

等级映射采用最低证据原则；只要任一批次是 `declared`，整个缓存只能是
`declared`，不能通过默认分支抬升为公开研究数据。

### 实验级网络访问策略

实验 `run_spec` 和研究 manifest 持久化
`data_access_policy=allow_fetch|cache_only`。默认 `allow_fetch` 保持历史行为；
`cache_only` 在股票池、行业筛选和基准指数链路上都禁止隐式网络访问：

- 日线输入只能通过 schema-v4 严格读取，逐代码验证
  `open/high/low/close/volume` 和训练/测试日期覆盖；
- frame 与 source provenance 在同一个 pool 锁内读取和验证，执行器直接绑定该
  原子快照，避免并发原子替换时把旧 provenance 与新 frame 拼接；
- 基准指数只读取独立本地缓存，并覆盖测试前十天至测试结束的 runner 窗口；
- 参数扫描成员、人工晋升的锁定测试和精确重跑继承同一策略；
- `/api/data/experiment-readiness` 只返回摘要，不触网、不写入且不暴露路径。

任何缓存缺失、旧 schema、provenance 不一致、字段/代码/日期缺口均 fail closed。

### 行业筛选

- 东方财富行业列采用精确别名匹配，名称为 BK 代码时拒绝。
- 现时行业列表和证券映射统一为巨潮 `008001` /
  `akshare:cninfo`，按证券行业变更记录构建并执行池级覆盖门槛；历史研究仍需
  独立的 point-in-time 行业时间线，不能把现时分类追认为历史分类。
- 行业缓存要求 schema、来源、分类、新鲜度、非空映射及 SHA-256 内容完整性；
  临时文件完成后原子替换。
- 映射为空或目标股票池覆盖率低于 95% 时，行业筛选失败；
  API 返回 `filterable/reason/source/classification/map_coverage`。
- `/api/data/industries` 可选传入 `pool_id` 计算该池覆盖率；未传股票池时
  只展示分类目录，明确返回 `coverage_not_evaluated`，不宣称可筛选。
- GET 读取路径只访问缓存；外部目录及映射刷新由
  `POST /api/data/industries/refresh` 显式触发并要求 `data:update` 权限。

## 3. 证据等级

| 等级 | 用途 | 当前实现 |
|---|---|---|
| `declared` | 合成测试、内部适配器开发 | 支持，不可晋级 |
| `public_aggregator` | 研究、异常发现、模拟盘观察 | AKShare 东财/新浪 |
| `licensed_vendor` | 有合同、字段字典、修订和 SLA 的研究输入 | 尚未接入 |
| `exchange_authoritative` | 交易所/指数公司授权历史产品及其可审计交付 | 尚未接入 |

两个公开聚合源一致，仍然是 `public_aggregator`，不能提升等级。

## 4. 推荐的生产数据组合

### 权威主证据

应采购并保存交易所或其授权信息公司的历史行情产品。上证所信息网络
有限公司的《历史数据产品说明书》明确描述历史行情数据产品及申请审核
机制：

- https://www.sseinfo.com/services/assortment/market/hqywwd/wdcpsms/c/10782125/files/f2ba70dea74a4323bf13b76fffce0e40.pdf

历史指数成分必须来自中证指数的带生效时间调整文件或授权数据产品，
而不是用当前成分反推历史。中证指数官方规则明确存在定期和临时调整：

- https://oss-ch.csindex.com.cn/notice/20251114142128-%E3%80%8A%E4%B8%AD%E8%AF%81%E6%8C%87%E6%95%B0%E6%9C%89%E9%99%90%E5%85%AC%E5%8F%B8%E8%82%A1%E7%A5%A8%E6%8C%87%E6%95%B0%E6%A0%B7%E6%9C%AC%E8%B0%83%E6%95%B4%E8%A7%84%E5%88%99%E3%80%8B.pdf

### 独立校验源

可用第二家有合同的数据供应商做日常交叉验证。Tushare 官方文档说明
其 qfq 根据请求 `end_date` 动态锚定，且复权因子由其自行生产；这正是
不能直接比较不同供应商 qfq 绝对价格、也不能把它视为交易所原始证据的
原因：

- https://tushare.pro/document/2?doc_id=146
- https://tushare.pro/document/2?doc_id=28

AKShare 官方风险说明本身记录过复权 OHLC 出现负值的案例，因此适合
研究交叉检查，不应被描述为实盘权威源：

- https://akshare.akfamily.xyz/data_tips.html

## 5. 仍未关闭的阻断项

1. point-in-time 指数成分、临时调整和公告原件尚未接入。
2. 上市、退市、ST、停复牌、涨跌停价格、公司行动和清算价格尚未形成
   同一时间轴。
3. 未采购交易所/授权供应商数据，证据等级仍停留在公开聚合研究级。
4. 旧 Parquet 缓存没有被修复；schema v4 只负责阻断并要求重建。
5. 交叉验证策略尚需根据两家合同数据的单位、精度和修订政策校准容差，
   不能沿用测试默认值直接认证实盘。
