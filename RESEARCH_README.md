# twscrape Research Workbench：独立研究试验版

这是 [vladkens/twscrape](https://github.com/vladkens/twscrape) 的公开fork，保留上游MIT许可证和版权。研究附加模块由Codex辅助开发，不把上游代码声称为独立原创。

固定上游起点：`568844df82af956df5970bcf7ff84dd5ba729eb0`（2026-10-13克隆）。原`twscrape/`目录不改；附加功能在`research_workbench/`，离线测试在`pilot_tests/`。

## 不影响原工具的边界

- 新目录、新Python环境、新账号数据库、新输出；不读取旧project目录、A–E的Chrome登录资料或原始raw/config/queue。
- 不启动/停止原任务、不恢复定时心跳、不转换原断点。这个试验不是生产迁移。
- GitHub fork是公开的；tmp、outputs、.venv、Cookie、数据库、日志全部忽略。Cookie数据库是本机敏感文件，不能上传、发送或截图。
- 只使用一个明确独立且未被其他任务占用的自有测试账号。禁止轮换账号绕过限流；账号别名不等于实际账号身份，需要用户自行核对。

## 第一版新增能力

1. 北京时间完整自然日半开时间戳查询，固定Latest/httpx，每页请求20条；记录上游起点版本，不混用HTTP后端测速。
2. 逐页保存原始响应和SHA256；实际query/cursor与配置核对。错误和空响应也保留，不使用上游会丢弃部分空响应的分页生成器。
3. posts/下一游标/页码原子检查点，ID去重；`--resume`只续保存游标，配置漂移/证据变化/保护状态禁止继续。
4. 原始解析数、日期排除数、实际保存数、正文关键词子串命中分别统计。自填位置仅从帖子自带用户对象被动读取，不请求About。
5. 末端只认`response_end`或至少三次规范游标空页`validated_empty_tail`；重复cursor、预算结束、HTTP错误、解析失败不算到底。
6. 401/403/404保护停止；429/reset/retry-after（含HTTP日期）遵守等待，有限网络重试，不降低保护。**第一版未移植生产工具的自动404控制组复核**，不能说已有完整生产等价能力。
7. 一日台账CSV、分栏UTF-8 TXT、状态JSON与响应文件相互关联。该版本是单日验证入口，尚非批量年度调度器，也没有原工作台全部UI功能。

## 本机安装和账号配置

```powershell
cd twscrape-research-workbench
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m research_workbench setup
```

也可双击根目录`配置独立测试账号.cmd`。在本机正常登录独立账号后，准备该账号的Cookie header（必须含auth_token、ct0），只在本机隐藏输入提示中粘贴，不在聊天中发送。项目不自动登录、不接受密码/2FA密钥、不绕过验证。

会话只写`tmp/research_accounts.db`。配置和采集共用排他锁，拒绝第二个试验进程。进程异常退出遗留锁时，先核验原PID已退出，不能直接删除锁重复启动。

## 先做5页试采

```powershell
.venv\Scripts\python.exe -m research_workbench run --keyword gpt --date 2026-06-25 --run-id pilot-gpt-0625 --max-pages 5
```

最多5页不是完整性承诺；遇合法冷却仍等待。输出`outputs/pilot-gpt-0625/`：

|文件|用途|
|---|---|
|state.json|帖子、保存游标、所有成功页和请求尝试、保护、累积采集耗时|
|status.json|运行状态、PID、实际保存数、页码、冷却截止|
|responses/attempt_*.json|原始响应正文；不保存请求Cookie/Authorization|
|posts.txt|UTF-8 BOM、制表符分栏；正文换行转义，ID按文本导入Excel|
|daily_ledger.csv|单日台账；正文命中不是语义相关性|

预算用完且无保护时，可以显式增加下一轮5页预算，仍从保存游标续：

```powershell
.venv\Scripts\python.exe -m research_workbench run --keyword gpt --date 2026-06-25 --run-id pilot-gpt-0625 --max-pages 5 --resume
```

页面延迟默认4秒，最低2秒；重试最多3次，基础退避至少180秒，服务器reset更长时按更长等待。不能把调低延迟或账号轮换当成公平测速。

## 如何检验到底好不好

当前完成的是离线功能测试，**尚未使用真实测试账号，不知道真实吞吐、登录兼容性或结束结果**。不宣称比原工具更快、采得更多或全量。

真实比较需要两套各自隔离的运行：同一实际账号（顺序进行，不能并发占用）、同一query/日期/时区/排序、相同延迟和页面预算；保留执行时间、原始响应、成功/失败次数、保存ID集合及结束状态。账号/时间不同会影响结果，不能只比较总条数。

`python -m research_workbench.compare <left-state.json> <right-state.json>`可对同结构的两个新鲜试验状态做ID交差集和吞吐摘要；配置不一致、恢复累计、预算不同或有保护时不给速度比较值。旧工具历史raw不是该格式，不能直接混用。此比较器尚未自动运行浏览器基线；公平的浏览器/HTTP端到端对比待账号配置后另做。

`active_seconds`包含签名准备、页面请求、退避、页间等待和检查点写入，排除进入采集函数之前的账号客户端初始化及最后导出。正式测速还需计完整进程墙钟和资源占用、重复多轮，不以单轮证明普遍优势。

## 测试

```powershell
.venv\Scripts\python.exe -m pip install pytest pytest-asyncio
.venv\Scripts\python.exe -m pytest pilot_tests -q
```

上游完整测试有额外开发依赖与部分外部测试样本；试验测试默认不用真实账号、不发X请求。
