CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT NOT NULL PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    role VARCHAR(24) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    must_change_password TINYINT(1) NOT NULL DEFAULT 1,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    urgent_quota_per_day INT NOT NULL DEFAULT 3,
    max_generation_concurrency INT NOT NULL DEFAULT 2,
    last_login_at DATETIME(6) NULL,
    deleted_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    active_username VARCHAR(64) GENERATED ALWAYS AS (
        IF(deleted_at IS NULL, username, NULL)
    ) STORED,
    active_display_name VARCHAR(100) GENERATED ALWAYS AS (
        IF(deleted_at IS NULL, display_name, NULL)
    ) STORED,
    UNIQUE KEY uk_users_active_username (active_username),
    UNIQUE KEY uk_users_active_display_name (active_display_name),
    INDEX idx_users_role_active (role, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS projects (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_code VARCHAR(128) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    deadline DATETIME(6) NOT NULL,
    priority VARCHAR(16) NOT NULL,
    brand_platform VARCHAR(200) NOT NULL DEFAULT '',
    category VARCHAR(200) NOT NULL DEFAULT '',
    note TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    owner_id BIGINT UNSIGNED NOT NULL,
    project_relative_root VARCHAR(1024) NOT NULL,
    pairing_mode VARCHAR(24) NOT NULL,
    source_date DATE NOT NULL,
    product_code VARCHAR(200) NOT NULL,
    project_scope VARCHAR(24) NOT NULL DEFAULT 'internal',
    project_name VARCHAR(200) NOT NULL DEFAULT '',
    is_urgent TINYINT(1) NOT NULL DEFAULT 0,
    urgent_date DATE NULL,
    urgent_by BIGINT UNSIGNED NULL,
    source_revision INT NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NULL,
    cancelled_at DATETIME(6) NULL,
    CONSTRAINT fk_projects_owner FOREIGN KEY (owner_id) REFERENCES users(id),
    CONSTRAINT fk_projects_urgent_by FOREIGN KEY (urgent_by)
        REFERENCES users(id),
    INDEX idx_projects_status_deadline (status, deadline),
    INDEX idx_projects_owner_created (owner_id, created_at),
    INDEX idx_projects_scope_name_date (
        project_scope, project_name, source_date, id
    ),
    INDEX idx_projects_created_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_menu_permissions (
    user_id BIGINT UNSIGNED NOT NULL,
    menu_key VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, menu_key),
    CONSTRAINT fk_user_menu_permissions_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_project_access (
    user_id BIGINT UNSIGNED NOT NULL,
    project_scope VARCHAR(24) NOT NULL,
    project_name VARCHAR(200) NOT NULL,
    PRIMARY KEY (user_id, project_scope, project_name),
    CONSTRAINT fk_user_project_access_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS api_providers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    provider_code VARCHAR(64) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    default_protocol VARCHAR(64) NOT NULL,
    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    priority INT NOT NULL DEFAULT 100,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS api_provider_models (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    provider_id BIGINT UNSIGNED NOT NULL,
    logical_model VARCHAR(100) NOT NULL,
    provider_model VARCHAR(160) NOT NULL,
    protocol VARCHAR(64) NOT NULL,
    max_reference_images INT NOT NULL DEFAULT 14,
    images_per_request INT NOT NULL DEFAULT 1,
    is_enabled TINYINT(1) NOT NULL DEFAULT 1,
    priority INT NOT NULL DEFAULT 100,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_api_provider_models_provider FOREIGN KEY (provider_id)
        REFERENCES api_providers(id) ON DELETE CASCADE,
    UNIQUE KEY uk_api_provider_model (provider_id, logical_model),
    INDEX idx_api_provider_models_route (
        logical_model, is_enabled, priority
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO api_providers(
    id, provider_code, display_name, base_url, default_protocol,
    is_enabled, priority, created_at, updated_at
) VALUES
    (
        1, 'liuguang', '流光 API', 'https://liuguang.ai/v1',
        'legacy_json_images', 1, 10, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
    ),
    (
        2, '88api', '88API', 'https://api.88api.ai/v1',
        'openai_chat_image', 1, 20, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
    ),
    (
        3, 'a6api', 'A6API', 'https://api.a6api.vip/v1',
        'openai_images', 1, 30, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
    ),
    (
        4, 'amux', 'Amux', 'https://gateway.amux.ai/v1',
        'openai_images', 1, 40, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
    );

INSERT IGNORE INTO api_provider_models(
    provider_id, logical_model, provider_model, protocol,
    max_reference_images, images_per_request, is_enabled, priority,
    created_at, updated_at
) VALUES
    (1, 'gemini-nano-banana-2.1', 'gemini-nano-banana-2.1',
        'legacy_json_images', 14, 4, 1, 10, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (1, 'gemini-3.1-flash-image', 'gemini-3.1-flash-image',
        'legacy_json_images', 14, 4, 1, 10, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (1, 'gemini-3-pro-image', 'gemini-3-pro-image',
        'legacy_json_images', 14, 4, 1, 10, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (2, 'gemini-nano-banana-2.1', 'gemini-nano-banana-2.1',
        'openai_chat_image', 6, 1, 1, 20, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (2, 'gemini-3.1-flash-image', 'gemini-3.1-flash-image',
        'openai_chat_image', 14, 1, 1, 20, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (2, 'gemini-3-pro-image', 'gemini-3-pro-image',
        'openai_chat_image', 14, 1, 1, 20, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (3, 'gemini-nano-banana-2.1', 'gemini-nano-banana-2.1',
        'openai_images', 14, 4, 1, 30, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (3, 'gemini-3.1-flash-image', 'gemini-3.1-flash-image',
        'openai_images', 14, 4, 1, 30, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (3, 'gemini-3-pro-image', 'gemini-3-pro-image',
        'openai_images', 14, 4, 1, 30, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (4, 'gemini-nano-banana-2.1', 'gemini-nano-banana-2.1',
        'openai_images', 14, 4, 1, 40, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (4, 'gemini-3.1-flash-image', 'gemini-3.1-flash-image',
        'openai_images', 14, 4, 1, 40, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)),
    (4, 'gemini-3-pro-image', 'gemini-3-pro-image',
        'openai_images', 14, 4, 1, 40, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6));

CREATE TABLE IF NOT EXISTS project_assets (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT UNSIGNED NOT NULL,
    role VARCHAR(16) NOT NULL,
    relative_path VARCHAR(1024) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    mime VARCHAR(64) NOT NULL,
    width INT NOT NULL,
    height INT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_project_assets_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    INDEX idx_project_assets_role (project_id, role),
    INDEX idx_project_assets_hash (project_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS project_tasks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_code VARCHAR(160) NOT NULL UNIQUE,
    project_id BIGINT UNSIGNED NOT NULL,
    task_no INT NOT NULL,
    garment_asset_id BIGINT UNSIGNED NOT NULL,
    model_asset_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL,
    assignee_id BIGINT UNSIGNED NULL,
    prompt_text LONGTEXT NOT NULL,
    model VARCHAR(100) NOT NULL DEFAULT '',
    aspect_ratio VARCHAR(24) NOT NULL DEFAULT '3:4',
    resolution VARCHAR(24) NULL,
    n INT NOT NULL DEFAULT 1,
    final_quantity INT NOT NULL DEFAULT 0,
    source_revision INT NOT NULL DEFAULT 1,
    is_urgent TINYINT(1) NOT NULL DEFAULT 0,
    urgent_date DATE NULL,
    urgent_by BIGINT UNSIGNED NULL,
    claimed_at DATETIME(6) NULL,
    work_started_at DATETIME(6) NULL,
    submitted_at DATETIME(6) NULL,
    approved_at DATETIME(6) NULL,
    returned_at DATETIME(6) NULL,
    parent_task_id BIGINT UNSIGNED NULL,
    split_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_project_tasks_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    CONSTRAINT fk_project_tasks_garment FOREIGN KEY (garment_asset_id)
        REFERENCES project_assets(id),
    CONSTRAINT fk_project_tasks_model FOREIGN KEY (model_asset_id)
        REFERENCES project_assets(id),
    CONSTRAINT fk_project_tasks_assignee FOREIGN KEY (assignee_id)
        REFERENCES users(id),
    CONSTRAINT fk_project_tasks_urgent_by FOREIGN KEY (urgent_by)
        REFERENCES users(id),
    CONSTRAINT fk_project_tasks_parent FOREIGN KEY (parent_task_id)
        REFERENCES project_tasks(id),
    UNIQUE KEY uk_project_task_no (project_id, task_no),
    INDEX idx_project_tasks_status_created (status, created_at),
    INDEX idx_project_tasks_assignee_status (assignee_id, status),
    INDEX idx_project_tasks_parent (parent_task_id),
    INDEX idx_project_tasks_project_status_created (
        project_id, status, created_at, id
    ),
    INDEX idx_project_tasks_assignee_status_created (
        assignee_id, status, created_at, id
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS generation_attempts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT UNSIGNED NOT NULL,
    attempt_no INT NOT NULL,
    operator_id BIGINT UNSIGNED NOT NULL,
    model VARCHAR(100) NOT NULL,
    prompt_text LONGTEXT NOT NULL,
    aspect_ratio VARCHAR(24) NOT NULL,
    resolution VARCHAR(24) NULL,
    n INT NOT NULL,
    status VARCHAR(24) NOT NULL,
    source_revision INT NOT NULL DEFAULT 1,
    unit_price DECIMAL(12, 4) NOT NULL DEFAULT 0,
    generated_image_count INT NOT NULL DEFAULT 0,
    request_id VARCHAR(255) NOT NULL DEFAULT '',
    remote_task_id VARCHAR(255) NOT NULL DEFAULT '',
    raw_response_json LONGTEXT NULL,
    input_tokens INT NULL,
    output_tokens INT NULL,
    error_code VARCHAR(100) NOT NULL DEFAULT '',
    error_message TEXT NULL,
    provider_id BIGINT UNSIGNED NULL,
    provider_code VARCHAR(64) NOT NULL DEFAULT '',
    provider_name VARCHAR(100) NOT NULL DEFAULT '',
    provider_model VARCHAR(160) NOT NULL DEFAULT '',
    requested_provider_code VARCHAR(64) NOT NULL DEFAULT '',
    requested_provider_name VARCHAR(100) NOT NULL DEFAULT '',
    created_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    CONSTRAINT fk_generation_attempts_task FOREIGN KEY (task_id)
        REFERENCES project_tasks(id) ON DELETE CASCADE,
    CONSTRAINT fk_generation_attempts_operator FOREIGN KEY (operator_id)
        REFERENCES users(id),
    CONSTRAINT fk_generation_attempts_provider FOREIGN KEY (provider_id)
        REFERENCES api_providers(id),
    UNIQUE KEY uk_generation_attempt_no (task_id, attempt_no),
    INDEX idx_generation_attempts_task_created (task_id, created_at),
    INDEX idx_generation_attempts_operator_created (operator_id, created_at),
    INDEX idx_generation_attempts_provider_created (provider_id, created_at),
    INDEX idx_attempts_created_id (created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS generation_provider_calls (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    attempt_id BIGINT UNSIGNED NOT NULL,
    provider_id BIGINT UNSIGNED NULL,
    provider_name VARCHAR(100) NOT NULL,
    provider_model VARCHAR(160) NOT NULL,
    status VARCHAR(24) NOT NULL,
    request_id VARCHAR(255) NOT NULL DEFAULT '',
    remote_task_id VARCHAR(255) NOT NULL DEFAULT '',
    status_code INT NULL,
    error_code VARCHAR(100) NOT NULL DEFAULT '',
    error_message TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_generation_provider_calls_attempt FOREIGN KEY (attempt_id)
        REFERENCES generation_attempts(id) ON DELETE CASCADE,
    CONSTRAINT fk_generation_provider_calls_provider FOREIGN KEY (provider_id)
        REFERENCES api_providers(id),
    INDEX idx_generation_provider_calls_attempt (attempt_id, created_at),
    INDEX idx_generation_provider_calls_provider (provider_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS generation_results (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    attempt_id BIGINT UNSIGNED NOT NULL,
    result_index INT NOT NULL,
    relative_path VARCHAR(1024) NOT NULL,
    thumbnail_relative_path VARCHAR(1024) NOT NULL DEFAULT '',
    content_hash CHAR(64) NOT NULL,
    mime VARCHAR(64) NOT NULL,
    width INT NOT NULL,
    height INT NOT NULL,
    is_final TINYINT(1) NOT NULL DEFAULT 0,
    source_type VARCHAR(24) NOT NULL DEFAULT 'generated',
    source_revision INT NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL,
    expire_at DATETIME(6) NULL,
    CONSTRAINT fk_generation_results_attempt FOREIGN KEY (attempt_id)
        REFERENCES generation_attempts(id) ON DELETE CASCADE,
    UNIQUE KEY uk_generation_result_index (attempt_id, result_index),
    INDEX idx_generation_results_final (attempt_id, is_final),
    INDEX idx_generation_results_expire (is_final, expire_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS task_reviews (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT UNSIGNED NOT NULL,
    reviewer_id BIGINT UNSIGNED NOT NULL,
    decision VARCHAR(16) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_task_reviews_task FOREIGN KEY (task_id)
        REFERENCES project_tasks(id) ON DELETE CASCADE,
    CONSTRAINT fk_task_reviews_reviewer FOREIGN KEY (reviewer_id)
        REFERENCES users(id),
    INDEX idx_task_reviews_task_created (task_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS workflow_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT UNSIGNED NOT NULL,
    task_id BIGINT UNSIGNED NULL,
    actor_id BIGINT UNSIGNED NULL,
    event_type VARCHAR(64) NOT NULL,
    from_status VARCHAR(32) NOT NULL DEFAULT '',
    to_status VARCHAR(32) NOT NULL DEFAULT '',
    detail_json LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_workflow_events_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    CONSTRAINT fk_workflow_events_task FOREIGN KEY (task_id)
        REFERENCES project_tasks(id) ON DELETE CASCADE,
    CONSTRAINT fk_workflow_events_actor FOREIGN KEY (actor_id)
        REFERENCES users(id),
    INDEX idx_workflow_events_project_created (project_id, created_at),
    INDEX idx_workflow_events_task_created (task_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS prompt_templates (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    scope VARCHAR(16) NOT NULL,
    owner_id BIGINT UNSIGNED NULL,
    name VARCHAR(120) NOT NULL,
    prompt LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_prompt_templates_owner FOREIGN KEY (owner_id)
        REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_prompt_templates_scope_owner (scope, owner_id),
    INDEX idx_prompt_templates_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS model_prices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    model VARCHAR(100) NOT NULL,
    unit_price DECIMAL(12, 4) NOT NULL,
    currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
    effective_from DATETIME(6) NOT NULL,
    created_by BIGINT UNSIGNED NOT NULL,
    CONSTRAINT fk_model_prices_creator FOREIGN KEY (created_by)
        REFERENCES users(id),
    INDEX idx_model_prices_model_effective (model, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS shared_settings (
    setting_key VARCHAR(100) NOT NULL PRIMARY KEY,
    value_json LONGTEXT NOT NULL,
    updated_by BIGINT UNSIGNED NULL,
    updated_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_shared_settings_user FOREIGN KEY (updated_by)
        REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS task_garments (
    task_id BIGINT UNSIGNED NOT NULL,
    garment_asset_id BIGINT UNSIGNED NOT NULL,
    position INT NOT NULL,
    PRIMARY KEY (task_id, garment_asset_id),
    CONSTRAINT fk_task_garments_task FOREIGN KEY (task_id)
        REFERENCES project_tasks(id) ON DELETE CASCADE,
    CONSTRAINT fk_task_garments_asset FOREIGN KEY (garment_asset_id)
        REFERENCES project_assets(id),
    INDEX idx_task_garments_position (task_id, position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS task_code_sequences (
    owner_id BIGINT UNSIGNED NOT NULL,
    sequence_date DATE NOT NULL,
    current_value INT NOT NULL,
    PRIMARY KEY (owner_id, sequence_date),
    CONSTRAINT fk_task_code_sequences_owner FOREIGN KEY (owner_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_api_keys (
    user_id BIGINT UNSIGNED NOT NULL,
    provider_id BIGINT UNSIGNED NOT NULL,
    encrypted_key LONGTEXT NOT NULL,
    key_suffix VARCHAR(8) NOT NULL,
    updated_by BIGINT UNSIGNED NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    last_test_status VARCHAR(24) NOT NULL DEFAULT '',
    last_test_at DATETIME(6) NULL,
    PRIMARY KEY (user_id, provider_id),
    CONSTRAINT fk_user_api_keys_user FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_api_keys_provider FOREIGN KEY (provider_id)
        REFERENCES api_providers(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_api_keys_admin FOREIGN KEY (updated_by)
        REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, UTC_TIMESTAMP(6));
