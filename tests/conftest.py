"""全局测试配置。

docs/dev/23：长时记忆库默认开启，而 Generator / Optimizer / Validator 的**默认依赖**在开启时会
访问数据库与 embedding 通道。单元测试一律不碰库、不发真实请求，因此在任何 `get_settings()`
被调用之前关闭总开关；需要记忆库的测试（`test_memory.py`）显式注入内存替身，注入的服务不受
该开关影响。开发者本机若要做集成验证，可在环境里显式设置 `SKILLEVAL_MEMORY_ENABLED=true` 覆盖。
"""

from __future__ import annotations

import os

os.environ.setdefault("SKILLEVAL_MEMORY_ENABLED", "false")
