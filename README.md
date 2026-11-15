# 时段开关试验台

本项目用于时段开关试验台的业务资料核对与分析留存。目前提供 `exposure_coverage` 覆盖核对和 `schedule_effect` 排班分配效果分析，业务定义见 `docs/data-contract.md`。示例数据在本地生成，分析不访问远程业务系统。

## 运行环境
Python 3.11；数值库固定在 `requirements.txt`。Dockerfile 通过不可变镜像摘要锁定已验证的 Python 3.11.14，代码兼容控制台 Python 3.11.2。依赖安装属于环境准备，不属于服务启动；已经准备过的环境不再联网下载。

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m app demo
python3 -m app effect
python3 -m app serve
```

服务默认监听本机 8080，`GET /health` 检查存活，`GET /v1/catalog` 查看当前分析类型。`POST /v1/analyses` 接收 `request_id`、`operation`、`as_of` 和可选 `options` 对象，返回保存后的分析；`GET /v1/analyses/{request_id}` 打开历史记录。`schedule_effect` 通过 `options.carryover_periods` 指定残留影响窗口（默认 `[0,1,2,3]`，单位为半小时时段）。数据文件由 `--source` 指定，SQLite 由 `--database` 指定，参数放在子命令前。`POST /v1/actions` 调用领域操作，已支持的动作见领域模块；没有配置动作的项目会明确拒绝。

## 分配效果分析
`python3 -m app effect` 从本地数据保存一次 `schedule_effect` 并打印完整结果，可用 `--as-of`、`--request-id` 和 `--carryover 0,1,2` 控制观察截止、记录编号和窗口。分析单位是 `randomization_set` 抽取出的完整排班，而不是订单或单个时段：

- `windows[]` 每个残留影响窗口列出可用时段、每种排除理由的计数和具体片区时段、开/关两臂的样本量、成交数和描述性成交率，以及 `assignment_effect`（按 block_offset 分层、按 randomization_set 聚类的分配效果和 95% 区间）。
- `evidence` 说明停投、覆盖漏报、观察截止、结果未报齐、确认零曝光和跨界订单如何改变可用证据；这些时段不会补零，也不会按成交结果挑选。
- `descriptive_claim_contrast` 只是实际领券人群的描述性对比，不是分配效果。
- `robustness` 汇总各窗口方向和区间，提示结论是否随窗口选择变化。
- `request`、`design.schedule_used`、`design.schedule_digest` 和 `analysis_options` 一并留存，`GET /v1/analyses/{request_id}` 可重新打开原判断依据。

## 目录职责
`app/domain.py` 保存业务核对与动作，`app/effect.py` 实现排班分配效果与窗口敏感性，`app/contracts.py` 处理可见版本，`app/repository.py` 维护输入与分析记录，`app/service.py` 协调操作和留存，`app/httpd.py` 提供本地接口。`tests` 覆盖业务口径、重复请求、并发回放及真实 HTTP 往返。

## 快速查看
`python3 -m app demo` 从本地数据执行当前覆盖核对并完整打印结果，一般数秒内完成。`python3 -m app effect` 输出分配效果分析。清理 `var` 可从干净状态重新运行，历史正式记录应另外留存。

## 容器与离线复现
首次在运行前执行 `docker build -t local-analysis .` 准备锁定依赖。
之后 `docker run --rm --network none local-analysis python3 -m unittest discover -s tests -v` 检查领域契约和真实 HTTP 工作流，`docker run --rm --network none local-analysis` 从全新容器输出当前业务核对。
容器不访问业务网络，不需要外部数据库或第二个服务；`requirements.lock` 与正式依赖入口 `requirements.txt` 保留相同精确版本。
当前测试和演示的执行上限各为80秒；新增分析应提供90秒以内可实际展示完整结果的入口，依赖准备在演示前完成。
