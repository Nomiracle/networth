"""networth 后端包（净值管家）。

模块划分：
    config.py    环境变量配置
    store.py     SQLite 建表/迁移与 CRUD
    domain.py    业务口径（重算/指标/导入导出/鉴权）
    importer.py  M0 产物导入 CLI
    api.py       HTTP 路由与鉴权
    server.py    进程入口
"""

__all__ = ["config", "store", "domain", "importer", "api", "server"]
__version__ = "1.0.0"
