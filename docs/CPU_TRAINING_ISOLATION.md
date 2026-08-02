# CPU/训练隔离边界（NEXT-09 代码验收）

## 已审计入口

| 入口 | 当前处置 | 边界 |
|---|---|---|
| 因子研究计算 | 既有 `isolated_cpu` spawn 子进程 | 保持原实现，不在本次重写。 |
| 模拟部署的周期性 `retrain` | 本次迁入 `model_retrain_fit` 注册 isolated task | 父进程只做 PIT 输入、证据、模型序列化和 CAS 晋级；`prepare/fit_with_validation` 在子进程。 |
| 常规实验的 TrainableStrategy walk-forward | PIT sample/label eligibility 门禁及显式“未接入隔离执行契约”双重拒绝，均在 fit 前 | 在可证明的 PIT mask 与完整隔离 result contract 完成前，禁止绕过门禁。 |
| 远程训练 | 服务只冻结 Parquet、接收不反序列化的 opaque artifact | 不在本机执行供应商/远端训练代码。 |
| 非 ML 策略回测 | 不调用 `fit` | 仍受 broker 的资源准入，不应被描述为训练隔离。 |

## 子进程契约

`isolated_cpu` 为因子和 retrain 共用一个全局单槽。子进程具有：

- 明确环境白名单；不继承 `.env`、JWT、数据库连接或打开文件描述符；
- 原生库线程数固定为 `JOB_CPU_THREAD_BUDGET=1`；
- `JOB_ISOLATED_CPU_TIMEOUT_SECONDS`（默认 1800 秒）超时终止；取消时等待回收；
- POSIX 新会话/进程组；超时或取消先 `SIGTERM` 整组、再 `SIGKILL`，防止训练子孙进程遗留；
- Linux worker 在导入重任务前应用
  `JOB_ISOLATED_CPU_MEMORY_LIMIT_MB=4096` 的地址空间 soft limit。macOS 不能由无特权
  进程可靠降低 `RLIMIT_AS`，Windows 仍缺 Job Object；两者只依赖单槽、单线程和自适应
  准入，不能称为 OS 级硬内存上限；
- 一个受限私有临时目录、请求/结果字节上限、运行时代码身份匹配和退出结果校验。

本机 8 GB 的调度前提是：最多一个隔离重任务、最多一个原生线程、并由现有容量控制器在
可用内存低于 768 MB、内存使用高于 92%、过度 swap/I/O 等情形下拒绝新重任务。

## 验收与未声明事项

```bash
.venv/bin/python -m pytest \
  backend/tests/test_isolated_cpu.py \
  backend/tests/test_retrain_isolation.py \
  backend/tests/test_model_artifact_integrity.py -q --timeout=120
```

测试覆盖 timeout、取消、崩溃、进程组回收、无效内存上限、隔离任务路由和重训练隔离失败
时的 fail-closed 行为。它们不分配真实 4 GB 内存，**不构成**真实 OOM 压测、macOS
memory-pressure SLO、macOS/Windows OS 级硬 RSS 限制或所有第三方模型生命周期已完成的
证明。Windows Job Object、macOS memory controller、训练输入大小/模型种类的实机压力
观测及连续 API/watchdog SLO 仍是 NEXT-09 的未闭环部分。
