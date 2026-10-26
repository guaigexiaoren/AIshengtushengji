from __future__ import annotations


DATABASE = {
    "host": "10.9.9.8",
    "port": 3306,
    "database": "ai作图",
    "user": "AI作图",
    "password": "AI/作图",
    "charset": "utf8mb4",
}

SHARED_ROOT = (
    r"\\fe80--211-32ff-fe87-ba49.ipv6-literal.net\AI作图"
)

# The application must be able to decrypt account API keys on every AI client.
# This keeps database values encrypted at rest; it is not a server-side secret.
API_KEY_MASTER_SECRET = "yixin-ai-image-workbench-api-keys-v2-2026"
