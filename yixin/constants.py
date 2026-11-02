from __future__ import annotations

APP_NAME = "亦欣AI生图工作台"
APP_ORGANIZATION = "亦欣"

API_BASE_URL = "https://liuguang.ai/v1"
GENERATION_ENDPOINT = "/images/generations"

PROTOCOL_LEGACY_JSON_IMAGES = "legacy_json_images"
PROTOCOL_OPENAI_IMAGES = "openai_images"
PROTOCOL_OPENAI_CHAT_IMAGE = "openai_chat_image"
PROVIDER_PROTOCOLS = (
    PROTOCOL_LEGACY_JSON_IMAGES,
    PROTOCOL_OPENAI_IMAGES,
    PROTOCOL_OPENAI_CHAT_IMAGE,
)
PROVIDER_PROTOCOL_LABELS = {
    PROTOCOL_LEGACY_JSON_IMAGES: "流光图片协议",
    PROTOCOL_OPENAI_IMAGES: "OpenAI 图片协议",
    PROTOCOL_OPENAI_CHAT_IMAGE: "OpenAI 对话图片协议",
}

MODEL_OPTIONS = (
    ("Nano Banana 2.1", "gemini-nano-banana-2.1"),
    ("Nano Banana 2", "gemini-3.1-flash-image"),
    ("Nano Banana Pro", "gemini-3-pro-image"),
)
AUTO_MODEL_ID = "auto"
MODEL_SELECTION_OPTIONS = (
    ("自动推荐", AUTO_MODEL_ID),
    *MODEL_OPTIONS,
)
DEFAULT_MODEL = AUTO_MODEL_ID
MODEL_IDS = frozenset(model_id for _, model_id in MODEL_OPTIONS)
MODEL_LABELS = dict(MODEL_OPTIONS)

ASPECT_RATIOS = (
    "auto",
    "1:1",
    "3:2",
    "4:3",
    "5:4",
    "16:9",
    "21:9",
    "2:3",
    "3:4",
    "4:5",
    "9:16",
)
DEFAULT_ASPECT_RATIO = "3:4"

RESOLUTIONS = ("auto", "1K", "2K", "4K")
DEFAULT_RESOLUTION = "2K"

MAX_BATCH_SIZE = 4
MAX_REFERENCE_IMAGES = 14
MAX_BATCH_ASSETS = 50
DEFAULT_BATCH_SIZE = 1

BUILTIN_PROMPT_TEMPLATES = (
    (
        "内置·基础服装替换",
        """# 任务需求；
将【图2】中的人物的上衣改为【图1】中的服装

# 要求
1. 服装穿着合身自然，【图2】中被替换的服装不再出现，未替换的区域保持不变
2. 其他细节：保持服装的面料质感、颜色、文字、图案、纹理、设计细节、人物、背景不变，不要出现水印""",
    ),
    (
        "内置·多图换装",
        """# 任务需求
根据参考图开头的角色说明，将人物参考图中人物的上衣替换为服装参考图中的服装。

# 要求
1. 服装穿着合身自然，被替换的原服装不再出现，未替换的区域保持不变
2. 保持服装的面料质感、颜色、文字、图案、纹理和设计细节
3. 保持人物身份、姿态和背景不变，不要出现水印""",
    ),
)

TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_STATUSES = frozenset(
    {TASK_QUEUED, TASK_RUNNING, TASK_SUCCESS, TASK_FAILED, TASK_CANCELLED}
)

MODE_TEXT = "text"
MODE_REFERENCE = "reference"
MODE_DRESS_UP = "dress_up"
MODE_FREE_COMPOSE = "free_compose"
MODE_LABELS = {
    MODE_TEXT: "文字生图",
    MODE_REFERENCE: "参考图编辑",
    MODE_DRESS_UP: "批量换装",
    MODE_FREE_COMPOSE: "自由组合",
}

STATUS_LABELS = {
    TASK_QUEUED: "排队中",
    TASK_RUNNING: "执行中",
    TASK_SUCCESS: "成功",
    TASK_FAILED: "失败",
    TASK_CANCELLED: "已取消",
}

CONNECT_TIMEOUT_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 300
DOWNLOAD_ATTEMPTS = 3

TEMP_RETENTION_DAYS = 7
MAX_API_KEY_LENGTH = 512
MIN_API_KEY_LENGTH = 10
