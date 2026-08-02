# 模型产物安全边界

模型文件不是普通数据文件：Joblib/Pickle 与旧式 `torch.load` 可以在加载时执行
Python 代码。本项目把它们视为受信执行边界，而非可上传格式。

## 当前规则

1. 模型必须位于 `MODEL_STORE_DIR`，是普通文件，且有长度与 SHA-256；加载时先
   复制并二次验证快照，避免校验与读取之间被替换。
2. 实验模型必须绑定 RunManifest、训练/验证补充证据和 `model-serialization/v1`
   合同。合同指定唯一格式和 loader，未知、篡改或与策略不匹配的格式在反序列化前
   拒绝。
3. Joblib 仅允许平台生成且已通过上述版本、哈希和不可变证据验证的
   `joblib-platform-v1`。外部上传的 `.joblib`、`.pkl`、`.pickle` 没有受支持入口。
4. LSTM/Transformer 的新 PyTorch checkpoint 是 tensor-only state-dict，并通过
   `torch.load(weights_only=True)` 加载；不会回退为不受限 pickle 加载。
5. 已有、带完整版本历史/哈希/RunManifest 证据的 `.joblib` 以
   `legacy-platform-joblib-v0` 兼容。它只为平滑既有模型，不能把任意旧文件变成可信
  文件，也不能用于新的外部导入。

## 反序列化入口审计（2026-08-02）

| 入口 | 处置 |
| --- | --- |
| 模拟部署 `load_verified_deployment_model` | 唯一生产加载路径；先证据、格式合同、快照，后加载。 |
| `StrategyProtocol.load_model` / Joblib | 平台生产路径只可经上述已验证 loader 调用；调优脚本已改为只验 hash，不再为自检反序列化。该策略方法是内部兼容 API，不是上传或数据库文件入口。 |
| LSTM / Transformer | 新产物写 tensor-only state-dict，受信路径强制 `weights_only=True`；格式不符不回退到 Joblib。 |
| 远程训练 worker | 只在受控 worker 中**写出**模型；上传产物未注册为可加载模型，仍须通过本地研究证据/promotion 门禁。 |
| `isolated_cpu` 父子进程 | 这是私有临时目录内、父进程创建的 IPC pickle，不是模型入口；受 0700 目录、0600 文件、hash、代码身份和任务白名单约束。后续可迁移 JSON/Arrow 以进一步缩小边界。 |

## 运行操作

- 只让本平台训练流程写入模型目录；不要从聊天、邮件、共享盘或第三方下载模型后放入
  该目录。
- 发现序列化合同、哈希或版本历史失败时，保持当前 champion，不要手工修改数据库或
  覆盖模型文件；重新训练产生新版本。
- 以后引入新模型时，优先 ONNX/SafeTensors 等非可执行格式。若暂时只能用 Joblib，
  必须在训练代码中生成并绑定 `model-serialization/v1` 合同、恶意 fixture 回归测试，
  并更新格式白名单；不要放宽通用 loader。

## 未完成事项

这不是对历史模型的“安全格式迁移证明”。现有历史 `.joblib` 仍是受信边界；迁移前需
逐模型数值等价测试和正式模型清单。ONNX/SafeTensors 转换器及生产历史模型迁移仍在
ROADMAP NEXT-14 的后续验收范围内。
