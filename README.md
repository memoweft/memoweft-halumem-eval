# MemoWeft × HaluMem 评测与诊断

使用 MiMo-V2.6-Flash 对 MemoWeft 最新内核进行隔离评测的完整实验项目。当前保存的是 **2026-10-16 主动停止的一次试考**，并非完整用户或排行榜成绩。

**先看 [问题定位报告](reports/DIAGNOSIS.md)。可信统计范围为前 12 个会话、32 道问答：8 对、12 遗漏、12 幻觉。** 第 13 会话判卷受到停止影响，第 14 会话仅开始摄取；二者均排除在诊断统计外。原始结果保留，未人为改分。

## 保存了什么

| 路径 | 内容 |
|---|---|
| `runner.py` | 逐轮摄取、形成等待、召回、答题、官方判卷接线、断点及用量记录 |
| `diagnose.py` | 无付费调用的离线分析，读取原始结果和测试数据库 |
| `test_runner.py` | 适配器的 3 项基础检查 |
| `test_diagnosis.py` | 5 项诊断测试：1 项通过、4 项预期失败，保存尚未修复的缺陷 |
| `data/HaluMem-Medium.jsonl` | 未修改的官方 Medium 数据文件；来源及许可见 `data/README.md` |
| `runs/medium-user-01/` | 原始逐会话快照、模型输出缓存、判卷、用量和 SQLite 测试数据库 |
| `reports/valid-summary.json` | 排除中断会话后的统计；优先使用这份，不要使用原始汇总比较成绩 |
| `reports/DIAGNOSIS.md` | 根因、证据、限制和建议修复顺序 |
| `memoweft/`、`halumem/` | 固定提交的 Git submodule（子模块），保留上游源码与许可 |
| `requirements-lock.txt` | 当次环境的依赖版本，已移除原机器路径 |
| `PUBLISH-MANIFEST.json` | 发布副本与原始实验的版本、文件摘要及移植说明 |

不提交可重建的虚拟环境、模型权重缓存、密钥及进程锁。测试数据库只包含公开合成考卷数据，没有日用记忆。

## 固定版本与模型

- MemoWeft：`e86b73ba9570a9bd490ef90bfb7059574de5e449`。
- HaluMem：`718f16ff0c83413b1c86fa83fc13cc1a639871f9`。
- 形成、答题、判卷：`mimo-v2.6-flash`；温度 0；关闭思考。
- 使用生产召回接口和本地语义模型，不用标准答案或考点写入记忆。
- 全部试考接口用量 7,546,435 词元；按当日公开按量价估算约 4.61 元。另有 31 词元接口预检；实际金额以账单为准。
- MiMo 自己答题、自己判卷可能带来偏差；原有 3 条非法 JSON 判卷保留为无效记录。

## 获取与离线复现

当前执行入口针对 Windows + PowerShell；Python 3.12，依赖 `uv` 和 Git。

```powershell
git clone --recurse-submodules https://github.com/memoweft/memoweft-halumem-eval.git
cd memoweft-halumem-eval
.\setup.ps1
.\.venv\Scripts\python.exe -m unittest test_runner -v
.\.venv\Scripts\python.exe diagnose.py
.\.venv\Scripts\python.exe -m unittest test_diagnosis -v
```

离线诊断不需要模型密钥，不会向模型发送请求。`expected failures=4` 表示成功复现了已知未修复缺陷，**不能称为质量测试全绿**。`diagnose.py` 重新生成报告证据文件，但只读原 SQLite，校验其前后摘要一致。

如果普通克隆没有取回子模块：

```powershell
git submodule update --init --recursive
```

## 再次付费运行

仓库保留 `STOP` 标记，默认不会开考。开始前阅读诊断报告；当前脚本的判卷格式与停止处理缺陷尚未修复，尤其不要把中断会话误当完整结果。

运行需提供环境变量 `MIMO_API_KEY`，不要将密钥写入仓库。启动脚本依次读取当前进程、用户和机器环境变量。确认需要运行后移除 `STOP`，用新的运行名保存结果：

```powershell
.\start-exam.ps1 -RunName medium-user-01-new -SessionLimit 1
```

只验证首个会话后可继续同一运行名；要考首位完整用户，将 `-SessionLimit` 设为 65。默认累计 2500 万词元停止，不自动扩大到其它用户或 Long。旧的 `medium-user-01` 是本次原始实验，应保留用于诊断。

运行中在项目根目录创建名为 `STOP` 的空文件即可请求停止；已知限制是当前评分器会将该异常吞为无效判卷，完成范围需要按停止时间核对。不会创建系统计划任务。

## 已知问题

已定位本人姓名过滤阻断本人事实、英文常见词影响排序、形成时丢失状态变化、长期目标被规则排除、答题模型猜测未知信息，以及判卷 JSON/中断接线问题。源码保持原基线，尚未修复这些问题。

## 来源与许可

本仓库用于私人研究复核。MemoWeft 与 HaluMem 子模块保留各自许可；HaluMem 数据与提示词等材料受上游 CC BY-NC-ND 4.0 约束，**不是本仓库自有的宽松许可素材**。见 [第三方来源](THIRD_PARTY_NOTICES.md)。未为整个混合实验仓库另行声明统一开源许可。
