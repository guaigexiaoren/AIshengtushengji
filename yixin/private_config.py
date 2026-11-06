from __future__ import annotations

import os


# 支持用环境变量覆盖，便于部署时轮换数据库密码和主密钥而无需改代码。
DATABASE = {
    "host": os.environ.get("YIXIN_DB_HOST", "10.9.9.8"),
    "port": int(os.environ.get("YIXIN_DB_PORT", "3306")),
    "database": os.environ.get("YIXIN_DB_NAME", "ai作图"),
    "user": os.environ.get("YIXIN_DB_USER", "AI作图"),
    "password": os.environ.get("YIXIN_DB_PASSWORD", "AI/作图"),
    "charset": "utf8mb4",
}

SHARED_ROOT = os.environ.get("YIXIN_SHARED_ROOT") or (
    r"\\10.6.6.6\AI作图"
)

# The application must be able to decrypt account API keys on every AI client.
# This keeps database values encrypted at rest; it is not a server-side secret.
API_KEY_MASTER_SECRET = (
    os.environ.get("YIXIN_API_KEY_SECRET")
    or "yixin-ai-image-workbench-api-keys-v2-2026"
)
