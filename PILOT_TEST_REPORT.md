# 隔离版验证报告

日期：2026-10-13。上游起点：568844df82af956df5970bcf7ff84dd5ba729eb0。

## 已验证

- 42项新增离线测试：真实query/cursor传递、空/错误响应不丢弃、北京自然日范围、无游标末端、三次规范空尾页、证据SHA256/游标链、预算不当完成、ID去重、TXT转义、严格解析错误、日期排除、已有保护与配置漂移拒绝、HTTP429与HTTP日期retry-after、跨恢复reset等待、磁盘失败不当网络重试、单账号限制、比较条件不一致拒绝速度结论。
- 102项上游回归：test_pagination.py、test_queue_client.py、test_http.py。
- 共144项通过；附加模块ruff检查通过。
- 上游twscrape/代码未修改；只新增研究模块、测试和说明，并补充打包/忽略规则。

复现命令：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[curl]" pytest pytest-asyncio pytest-httpx ruff
.venv\Scripts\python.exe -m pytest pilot_tests tests/test_pagination.py tests/test_queue_client.py tests/test_http.py -q
.venv\Scripts\python.exe -m ruff check research_workbench pilot_tests
```

curl相关上游测试在Windows报告6条selector兼容警告，但测试通过。研究CLI固定httpx，此警告不是试采业务失败。

## 尚未验证

- 没有配置真实测试账号，没有向X发送研究采集请求。
- 未测真实速度、登录/签名兼容性、真实日期末端、与原浏览器工具的ID差异。
- 未做全量上游测试（部分需要额外样本/依赖）；不能把144项称为上游全部测试。
- 没有迁移生产任务、导入原五账号会话或原研究数据；没有新增About请求。

当前结论仅为“研究附加层的隔离功能验证通过，具备小样本真实试采入口”，不是“更快”“抓齐”或“可以取代生产工具”。配置入口见RESEARCH_README.md；会话只存在本机忽略目录。
