"""
Agent 运行时拆包：媒体 Hub、入库、工具定义、工具分发、消息解析。

- 入口仍从项目根目录的 ``agent_core`` 使用（uvicorn main:app 无需改 import）。
- ``tool/`` 下 ``generation_tools`` / ``ai`` 依赖 ``sys.path`` 含 ``tool`` 目录，由 ``agent_core`` 最先完成注入。
"""
