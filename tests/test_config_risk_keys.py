import importlib
import os
import sys
import unittest
from unittest.mock import patch


def _load_settings(env: dict[str, str]):
    with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv", return_value=False):
        sys.modules.pop("config", None)
        import config

        importlib.reload(config)
        return config.Settings()


class ConfigRiskKeysTests(unittest.TestCase):
    def test_reads_anchored_risk_workflow_api_key(self):
        settings = _load_settings(
            {
                "DIFY_BASE_URL": "http://fake.local/v1",
                "DIFY_CLAUSE_WORKFLOW_API_KEY": "app-clause",
                "DIFY_ANCHORED_RISK_WORKFLOW_API_KEY": "app-anchored",
                "DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY": "app-mm",
            }
        )
        self.assertEqual(settings.dify_anchored_risk_workflow_api_key, "app-anchored")

    def test_reads_missing_multi_risk_workflow_api_key(self):
        settings = _load_settings(
            {
                "DIFY_BASE_URL": "http://fake.local/v1",
                "DIFY_CLAUSE_WORKFLOW_API_KEY": "app-clause",
                "DIFY_ANCHORED_RISK_WORKFLOW_API_KEY": "app-anchored",
                "DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY": "app-mm",
            }
        )
        self.assertEqual(settings.dify_missing_multi_risk_workflow_api_key, "app-mm")

    def test_anchored_key_priority_prefers_new_key(self):
        settings = _load_settings(
            {
                "DIFY_BASE_URL": "http://fake.local/v1",
                "DIFY_CLAUSE_WORKFLOW_API_KEY": "app-clause",
                "DIFY_RISK_WORKFLOW_API_KEY": "app-legacy",
                "DIFY_ANCHORED_RISK_WORKFLOW_API_KEY": "app-anchored",
            }
        )
        self.assertEqual(settings.anchored_risk_api_key(), "app-anchored")

    def test_missing_multi_key_priority_prefers_new_key(self):
        settings = _load_settings(
            {
                "DIFY_BASE_URL": "http://fake.local/v1",
                "DIFY_CLAUSE_WORKFLOW_API_KEY": "app-clause",
                "DIFY_RISK_WORKFLOW_API_KEY": "app-legacy",
                "DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY": "app-mm",
            }
        )
        self.assertEqual(settings.missing_multi_risk_api_key(), "app-mm")

    def test_legacy_key_compatibility_for_both_streams(self):
        settings = _load_settings(
            {
                "DIFY_BASE_URL": "http://fake.local/v1",
                "DIFY_CLAUSE_WORKFLOW_API_KEY": "app-clause",
                "DIFY_RISK_WORKFLOW_API_KEY": "app-legacy",
                "DIFY_FAST_SCREEN_WORKFLOW_API_KEY": "app-fast-screen",
            }
        )
        self.assertEqual(settings.anchored_risk_api_key(), "app-legacy")
        self.assertEqual(settings.missing_multi_risk_api_key(), "app-legacy")
        settings.validate_for_live_call()


if __name__ == "__main__":
    unittest.main()
