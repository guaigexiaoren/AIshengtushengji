from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


ROLE_OPERATIONS = "operations"
ROLE_AI = "ai"
ROLE_ADMIN = "admin"
ROLES = (ROLE_OPERATIONS, ROLE_AI, ROLE_ADMIN)
ROLE_LABELS = {
    ROLE_OPERATIONS: "运营",
    ROLE_AI: "AI 制作",
    ROLE_ADMIN: "管理员",
}

MENU_KEYS = {
    "new_task": "新建任务",
    "submitted": "已提交",
    "pending": "待审核",
    "completed": "已完成",
    "task_center": "任务中心",
    "create": "普通创作",
    "batch": "批量换装",
    "history": "历史记录",
    "library": "作品库",
    "admin": "管理控制台",
}
ROLE_DEFAULT_MENUS = {
    ROLE_OPERATIONS: (
        "new_task",
        "submitted",
        "pending",
        "completed",
    ),
    ROLE_AI: (
        "new_task",
        "submitted",
        "pending",
        "completed",
        "task_center",
        "create",
        "batch",
        "history",
        "library",
    ),
    ROLE_ADMIN: tuple(MENU_KEYS),
}
ROLE_ALLOWED_MENUS = {
    ROLE_OPERATIONS: (
        "new_task",
        "submitted",
        "pending",
        "completed",
        "task_center",
    ),
    ROLE_AI: ROLE_DEFAULT_MENUS[ROLE_AI],
    ROLE_ADMIN: ROLE_DEFAULT_MENUS[ROLE_ADMIN],
}

PROJECT_SCOPES = ("internal", "external")
PROJECT_SCOPE_LABELS = {
    "internal": "内部项目",
    "external": "外部项目",
}

PROJECT_SUBMITTED = "submitted"
PROJECT_IN_PROGRESS = "in_progress"
PROJECT_AWAITING_ACCEPTANCE = "awaiting_acceptance"
PROJECT_COMPLETED = "completed"
PROJECT_CANCELLED = "cancelled"
PROJECT_STATUS_LABELS = {
    PROJECT_SUBMITTED: "已提交",
    PROJECT_IN_PROGRESS: "制作中",
    PROJECT_AWAITING_ACCEPTANCE: "待审核",
    PROJECT_COMPLETED: "已完成",
    PROJECT_CANCELLED: "已取消",
}

TASK_AVAILABLE = "available"
TASK_CLAIMED = "claimed"
TASK_WORKING = "working"
TASK_AWAITING_ACCEPTANCE = "awaiting_acceptance"
TASK_RETURNED = "returned"
TASK_APPROVED = "approved"
TASK_SKIPPED = "skipped"
TASK_CANCELLED = "cancelled"
TASK_STATUS_LABELS = {
    TASK_AVAILABLE: "已提交",
    TASK_CLAIMED: "制作中",
    TASK_WORKING: "制作中",
    TASK_AWAITING_ACCEPTANCE: "待审核",
    TASK_RETURNED: "已退回",
    TASK_APPROVED: "已完成",
    TASK_SKIPPED: "无需制作",
    TASK_CANCELLED: "已取消",
}

PAIRING_MANUAL = "manual"
PAIRING_ALL = "all"
PAIRING_FILENAME = "filename"
PAIRING_LABELS = {
    PAIRING_MANUAL: "手动配对",
    PAIRING_ALL: "全部组合",
    PAIRING_FILENAME: "G/M 编号配对",
}

PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_LABELS = {
    "low": "低",
    "normal": "普通",
    "high": "高",
    "urgent": "紧急",
}

ATTEMPT_QUEUED = "queued"
ATTEMPT_RUNNING = "running"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool = False
    is_active: bool = True
    urgent_quota_per_day: int = 3
    max_generation_concurrency: int = 2
    menu_permissions: tuple[str, ...] = ()
    project_access: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ProjectAssetInput:
    role: str
    relative_path: str
    original_name: str
    content_hash: str
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class PairingInput:
    garment_index: int
    model_index: int


@dataclass(frozen=True)
class ProjectDraft:
    project_code: str
    name: str
    deadline: datetime
    priority: str
    project_relative_root: str
    pairing_mode: str
    assets: tuple[ProjectAssetInput, ...]
    pairs: tuple[PairingInput, ...]
    brand_platform: str = ""
    category: str = ""
    note: str = ""
    source_date: date | None = None
    product_code: str = ""
    project_scope: str = "internal"
    access_project_name: str = ""
    is_urgent: bool = False


@dataclass(frozen=True)
class ProjectRecord:
    id: int
    project_code: str
    name: str
    deadline: datetime
    priority: str
    brand_platform: str
    category: str
    note: str
    status: str
    owner_id: int
    owner_name: str
    project_relative_root: str
    pairing_mode: str
    created_at: datetime
    updated_at: datetime
    task_count: int = 0
    approved_count: int = 0
    awaiting_count: int = 0
    product_code: str = ""
    source_date: date | None = None
    urgent_count: int = 0
    project_scope: str = "internal"
    project_name: str = ""
    is_urgent: bool = False
    urgent_date: date | None = None
    urgent_by: int | None = None


@dataclass(frozen=True)
class ProjectTask:
    id: int
    task_code: str
    project_id: int
    project_code: str
    project_name: str
    project_relative_root: str
    garment_asset_id: int
    garment_path: str
    garment_name: str
    garment_hash: str
    model_asset_id: int
    model_path: str
    model_name: str
    model_hash: str
    status: str
    assignee_id: int | None
    assignee_name: str
    prompt_text: str
    model: str
    aspect_ratio: str
    resolution: str | None
    n: int
    final_quantity: int
    owner_id: int
    created_at: datetime
    updated_at: datetime | None = None
    claimed_at: datetime | None = None
    work_started_at: datetime | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    returned_at: datetime | None = None
    project_deadline: datetime | None = None
    project_priority: str = "normal"
    source_date: date | None = None
    product_code: str = ""
    source_revision: int = 1
    garment_assets: tuple[ProjectAssetInput, ...] = ()
    is_urgent: bool = False
    urgent_date: date | None = None
    urgent_by: int | None = None
    project_scope: str = "internal"
    access_project_name: str = ""
    project_is_urgent: bool = False


@dataclass(frozen=True)
class GenerationAttempt:
    id: int
    task_id: int
    attempt_no: int
    operator_id: int
    model: str
    prompt_text: str
    aspect_ratio: str
    resolution: str | None
    n: int
    status: str
    source_revision: int
    unit_price: Decimal
    generated_image_count: int
    request_id: str = ""
    remote_task_id: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_message: str = ""
    operator_name: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None
    provider_id: int | None = None
    provider_code: str = ""
    provider_name: str = ""
    provider_model: str = ""


@dataclass(frozen=True)
class ApiProvider:
    id: int
    provider_code: str
    display_name: str
    base_url: str
    default_protocol: str
    enabled: bool
    priority: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ProviderModelRoute:
    provider_id: int
    provider_code: str
    provider_name: str
    base_url: str
    default_protocol: str
    logical_model: str
    provider_model: str
    protocol: str
    max_reference_images: int
    images_per_request: int
    priority: int


@dataclass(frozen=True)
class GenerationResult:
    id: int
    attempt_id: int
    result_index: int
    relative_path: str
    thumbnail_relative_path: str
    content_hash: str
    mime: str
    width: int
    height: int
    is_final: bool
    source_type: str = "generated"
    source_revision: int = 1


@dataclass(frozen=True)
class GeneratedResultInput:
    result_index: int
    relative_path: str
    thumbnail_relative_path: str
    content_hash: str
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class ReviewDecision:
    approved: bool
    reason: str = ""


# User-facing names used by the v2 submission workflow.
SubmissionTask = ProjectRecord
SubmissionSubtask = ProjectTask
SubmissionDraft = ProjectDraft
SourceAsset = ProjectAssetInput
GenerationJob = GenerationAttempt
CandidateImage = GenerationResult
AccountPolicy = AuthUser
