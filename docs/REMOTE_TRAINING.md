# Windows 远程训练客户端

远程训练客户端让受信任的 Windows 工作站领取一个不可变训练任务，在本地
校验策略源码和 Parquet 数据、训练模型，并把模型产物上传回平台。客户端不会
执行远端脚本、解压远端归档或加载远端模型。

## 安装

建议为工作端建立独立虚拟环境，并从项目根目录运行：

```powershell
py -3.11 -m venv .venv-remote-worker
.\.venv-remote-worker\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-remote-worker.txt
```

如果需要 NVIDIA CUDA，请先按照 PyTorch 官方安装器为显卡驱动选择匹配的
PyTorch wheel，再安装其余依赖。LightGBM、XGBoost 和 PyTorch 均包含原生
组件，不要从不可信目录加载 DLL，也不要把下载目录加入 `PATH`。

检查 Python、依赖、PyTorch、CUDA 和显卡：

```powershell
python scripts\remote_train_client.py --doctor
```

## 运行任务

推荐直接运行不含令牌的命令，随后在安全提示中粘贴一次性令牌。输入内容不会
回显，也不会写入 PowerShell 历史：

```powershell
python scripts\remote_train_client.py `
  --server https://quant.example.com `
  --task-id 0123456789abcdef8123456789abcdef `
  --output-dir D:\quant-models `
  --device auto
```

无人值守环境使用进程级环境变量。环境变量优先于 `--token`；`--token` 仅为
已有自动化兼容保留，因为命令行参数可能被进程列表和终端历史记录：

```powershell
$secureToken = Read-Host "一次性令牌" -AsSecureString
$env:QUANT_REMOTE_TRAINING_TOKEN = [System.Net.NetworkCredential]::new("", $secureToken).Password
python scripts\remote_train_client.py `
  --server https://quant.example.com `
  --task-id 0123456789abcdef8123456789abcdef `
  --output-dir D:\quant-models
Remove-Item Env:\QUANT_REMOTE_TRAINING_TOKEN
Remove-Variable secureToken
```

先验证而不训练、不上传：

```powershell
python scripts\remote_train_client.py `
  --server https://quant.example.com `
  --task-id 0123456789abcdef8123456789abcdef `
  --output-dir D:\quant-models `
  --dry-run
```

## 协议与校验

客户端通过 `X-Training-Token` 请求头认证。任务 ID 是服务端生成的 32 位
小写十六进制字符串：

- `GET /api/remote-training/tasks/{id}/bundle` 获取
  `{data: manifest}` 响应，其中 manifest 使用
  `remote-training-bundle/v1`。
- `GET /api/remote-training/tasks/{id}/data` 下载 Parquet。
- `POST .../start` 使用空请求体；`POST .../progress` 只发送
  `progress`、`message`。
- `POST .../complete` 以 `report_json`、`artifact` multipart 字段上传结果。
- `report_json` 使用 `remote-training-result/v1`，并绑定任务、实验、策略、
  参数 SHA-256 和数据 SHA-256。
- 任一步失败时尽力调用 `POST .../fail`，请求体仅包含 `error`。

客户端在任何 Parquet 读取前扫描本地 Strategy Registry，从而保证 Windows
先加载 LightGBM DLL，再由 PyArrow 加载 Parquet 原生库。训练前还会校验：

- task ID、bundle 协议版本、参数规范化 SHA-256；
- 本地策略源码 SHA-256，且策略必须继承 `TrainableStrategy`；
- 数据下载 URL 必须是当前任务的 `/data`，避免跨站下载；
- Parquet 文件 SHA-256、行列数、日期范围；
- `DatetimeIndex` 必须无时区、排序且唯一；
- 列必须是唯一的 `(code, field)` 两级 `MultiIndex`；
- 必须包含 `open/high/low/close/volume/amount`；
- 训练窗口、标签周期必须和 manifest、本地策略一致。

所有现有训练策略统一调用：

```text
strategy.prepare(pivot, params)
strategy.fit(pivot, params, train_start, train_end)
strategy.last_train_metrics
strategy.save_model(model, artifact_path)
```

远端任务定义的是单一训练窗口。周期 Walk-Forward 的窗口拆分和任务编排仍由
服务端负责，客户端不会自行扩大训练范围或重复训练多个周期。

## 本地产物

成功任务以 `<output-dir>\<task_uuid>\` 为目录原子发布。服务端不提供模型
文件名时，客户端使用安全固定名称 `model.joblib`：

```text
<task_uuid>\
  <manifest 中的 suggested_name>
  report.json
```

模型文件可能是 Joblib 模型，也可能是 PyTorch state-dict checkpoint，不能仅
根据扩展名判断格式。`report.json` 包括：

- 任务、实验、策略、参数和数据指纹；
- Windows、Python、依赖和 CUDA/GPU 信息；
- 策略实际使用的设备；
- 训练窗口、训练指标；
- 模型文件大小和 SHA-256。

## 安全边界

- 只连接管理员配置的 HTTPS 平台；开发环境使用 HTTP 时不要传输真实令牌。
- 令牌不会写入日志或报告；优先使用安全交互输入或临时环境变量。
- 客户端只加载本地仓库策略。Registry 扫描会执行本地策略模块顶层代码，因此
  运行前应确认仓库 commit 可信且工作树没有未知修改。
- Joblib/Pickle 和 `torch.load` 能执行代码。不要用本客户端之外的工具加载
  未验证来源的模型，也不要把远端文件冒充成本地模型。
- 输出文件名只允许普通文件名；已有 task 目录不会被覆盖。
- 树模型可能使用全部 CPU，深度模型会自动选择 CUDA。并行运行多个客户端前
  应限制任务数，避免 CPU 过度订阅或 GPU 显存耗尽。
