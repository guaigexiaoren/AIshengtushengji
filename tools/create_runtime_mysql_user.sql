-- 由数据库管理员执行，不要在当前账号权限不足时强行运行。
-- 请替换数据库名、允许来源网段、账号和强密码后再执行。

CREATE USER 'ai_workbench_app'@'10.0.0.%' IDENTIFIED BY '请替换为强密码';
GRANT SELECT, INSERT, UPDATE, DELETE ON `ai作图`.* TO 'ai_workbench_app'@'10.0.0.%';
FLUSH PRIVILEGES;
