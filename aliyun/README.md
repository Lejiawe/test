# 阿里云 FC 云沙箱并发启动测试

本目录是本地压测客户端，不是在本机部署阿里云云沙箱服务端。客户端通过
E2B Python SDK 调用阿里云 Function Compute 云沙箱，测试以下两项：

1. 并发启动速度：按 `1/s、10/s、50/s、100/s、200/s` 发起创建。
2. 存储挂载启动速度：分别测试无挂载、对象存储 OSS、文件存储（通常是 NAS）。

每次试验同时记录：

- `api_latency_ms`：调用 `Sandbox.create()` 到返回 Sandbox ID 的时间（L1）。
- `first_command_latency_ms`：create 返回后，第一条真实命令执行完成的时间（L2）。
- `second_command_latency_ms`：第一条命令完成后，到第二条命令完成的时间。
- API 成功率、功能就绪成功率、清理成功率。

## 1. 环境

环境已经创建：

```powershell
conda activate aliyun
python --version
python -m pip show e2b e2b-code-interpreter
```

以后如需重建：

```powershell
conda env create -f environment.yml
```

当前固定为阿里云快速入门验证过的 SDK 组合：
`e2b==2.31.0`、`e2b-code-interpreter==2.8.1`。

## 2. 阿里云侧准备

1. 开通函数计算和云沙箱功能。
2. 在函数计算控制台选择目标地域。
3. 在“云沙箱 > API Keys”创建并启用 API Key。
4. 确认官方模板 `code-interpreter-v1` 可用，建议先配置模板日志。
5. 确认账号在该地域的 Sandbox 并发/实例配额足够。`200/s` 不是“单实例并发度”；
   它表示客户端每秒发起 200 次独立的 `Sandbox.create()`。
6. OSS/NAS 测试需先准备同地域存储、RAM 角色和（NAS 所需的）VPC。

## 3. 配置

复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

至少填写：

```dotenv
E2B_API_KEY=e2b_xxx
E2B_API_URL=https://api.cn-beijing.e2b.fc.aliyuncs.com
E2B_DOMAIN=cn-beijing.e2b.fc.aliyuncs.com
```

三项必须属于同一地域。API Key 不要发到聊天、日志或 Git 仓库；`.gitignore`
已经忽略 `.env`。

参考图片中的 VPC 配置可写为一行 JSON：

```dotenv
E2B_VPC_CONFIG_JSON={"vpcId":"vpc-xxx","securityGroupId":"sg-xxx","vSwitchIds":["vsw-xxx"]}
```

OSS 的配置例子已写在 `.env.example`。文件存储/NAS 的 metadata key 和 JSON
在不同灰度版本中可能由项目侧约定，因此脚本不写死格式；将交付给你的 key
填到 `E2B_FILE_METADATA_KEY`，JSON 填到 `E2B_FILE_CONFIG_JSON`。

## 4. 先做一次连通测试

无挂载：

```powershell
python benchmark.py smoke --storage none
```

再分别验证挂载，成功后才做高并发：

```powershell
python benchmark.py smoke --storage oss
python benchmark.py smoke --storage file
```

## 5. 正式测试

先看计划，不产生云资源：

```powershell
python benchmark.py plan --rates "1,10,50,100,200" --duration-seconds 1 --storages none
```

并发启动速度（第一行）：

```powershell
python benchmark.py run --rates "1,10,50,100,200" --duration-seconds 1 --storages none --confirm
```

存储挂载对比（第二行）：

```powershell
python benchmark.py run --rates "1,10,50,100,200" --duration-seconds 1 --storages "none,oss,file" --confirm
```

后一条会创建 `(1 + 10 + 50 + 100 + 200) × 3 = 1083` 个沙箱，可能触发
配额、限流和费用。建议先用：

```powershell
python benchmark.py run --rates "1,10" --duration-seconds 1 --storages "none,oss,file" --confirm
```

验证通过，再逐级升到 `50/s、100/s、200/s`。需要更稳定的统计时，可以把
`--duration-seconds` 提高到 5 或 10，但调用量和费用会同比增加。

## 6. 结果

每次运行会按“测试内容 + 存储类型 + 档位 + 时间”生成目录和文件。例如：

```text
results/
└── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123/
    ├── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123_原始明细.csv
    ├── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123_汇总.csv
    ├── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123_失败日志.csv
    ├── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123_失败日志.txt
    └── 启动并发速度_无挂载_50rps_持续60s_20260730_143000_123_汇总.json
```

- `原始明细.csv`：每一个 Sandbox 的原始测量。
- `汇总.csv`：按存储类型和并发档位聚合后的成功率、均值以及
  P50/P90/P95/P99。
- `失败日志.csv`：失败请求的阶段、Sandbox ID、错误类型、错误信息和堆栈，
  便于 Excel 筛选分析。
- `失败日志.txt`：失败请求的完整可读日志；没有失败时也会生成并写明
  “本轮无失败”。
- `汇总.json`：同一份汇总的 JSON 格式。

提交测试结论时，应同时报告 `api_latency_*`、`first_command_latency_*`、
`second_command_latency_*` 和 `ready_success_rate_pct`。如果
`schedule_delay_p95_ms` 很大，说明本地压测机线程或网络已经无法按目标速率
发压，该档结果不应直接归因于云沙箱。全部字段解释见 `指标说明.md`。
可直接复制到报告中的纯文字说明见 `指标说明.txt`。
