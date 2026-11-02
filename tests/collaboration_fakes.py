from yixin.collaboration.reports import aggregate_report
from yixin.collaboration.types import ROLE_AI, AuthUser


class EmptyCentralRepository:
    def find_tasks_by_product(self, *_args):
        return []

    def list_projects(self, **_kwargs):
        return []

    def list_project_tasks(self, _project_id):
        return []

    def list_board_tasks(self, _actor, **_kwargs):
        return []

    def list_attempts(self, _task_id):
        return []

    def list_results(self, _task_id):
        return []

    def list_users(self, role=None):
        return []

    def api_key_statuses(self):
        return {}

    def api_key_statuses_by_provider(self):
        return {}

    def api_key_status(self, _user_id, _provider_id):
        return None

    def list_api_providers(self, **_kwargs):
        return []

    def list_provider_models(self, _provider_id, **_kwargs):
        return []

    def list_model_prices(self):
        return {}

    def list_templates(self, _actor):
        return []

    def get_shared_setting(self, _key):
        return None

    def get_account_policy(self, _user_id):
        return {
            "urgent_quota_per_day": 3,
            "max_generation_concurrency": 2,
        }

    def load_actor_api_key(self, _actor, **_kwargs):
        return None

    def report(self, _filter):
        return aggregate_report([])

    def test_connection(self):
        return {"db": "test", "version": "8.4"}


def fake_ai_user() -> AuthUser:
    return AuthUser(99, "ai_test", "AI测试", ROLE_AI)
