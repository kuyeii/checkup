from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(slots=True)
class Settings:
    dify_base_url: str = os.getenv("DIFY_BASE_URL", "http://localhost/v1")
    dify_clause_workflow_api_key: str = os.getenv("DIFY_CLAUSE_WORKFLOW_API_KEY", "")
    dify_risk_workflow_api_key: str = os.getenv("DIFY_RISK_WORKFLOW_API_KEY", "")
    dify_anchored_risk_workflow_api_key: str = os.getenv("DIFY_ANCHORED_RISK_WORKFLOW_API_KEY", "")
    dify_missing_multi_risk_workflow_api_key: str = os.getenv("DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY", "")
    dify_fast_screen_workflow_api_key: str = os.getenv("DIFY_FAST_SCREEN_WORKFLOW_API_KEY", "")
    dify_rewrite_workflow_api_key: str = os.getenv("DIFY_REWRITE_WORKFLOW_API_KEY", "")
    dify_aggregate_rewrite_workflow_api_key: str = os.getenv("DIFY_AGGREGATE_REWRITE_WORKFLOW_API_KEY", "")
    review_side: str = os.getenv("REVIEW_SIDE", "")  # 必须设置：supplier=供应商视角，customer=采购方视角
    contract_type_hint: str = os.getenv("CONTRACT_TYPE_HINT", "service_agreement")
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
    dify_max_concurrency: int = int(os.getenv("DIFY_MAX_CONCURRENCY", "6"))
    clause_split_max_concurrency: int = int(os.getenv("CLAUSE_SPLIT_MAX_CONCURRENCY", "3"))
    run_root: Path = Path(os.getenv("RUN_ROOT", "data/runs"))
    debug_save_intermediate: bool = os.getenv("DEBUG_SAVE_INTERMEDIATE", "1") == "1"
    fast_screen_enabled: bool = os.getenv("FAST_SCREEN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    fast_screen_max_candidates: str = str(os.getenv("FAST_SCREEN_MAX_CANDIDATES", "12"))
    analysis_scope: str = os.getenv("ANALYSIS_SCOPE", "full_detail")

    def anchored_risk_api_key(self) -> str:
        return self.dify_anchored_risk_workflow_api_key or self.dify_risk_workflow_api_key

    def missing_multi_risk_api_key(self) -> str:
        return self.dify_missing_multi_risk_workflow_api_key or self.dify_risk_workflow_api_key

    def aggregate_rewrite_api_key(self) -> str:
        return self.dify_aggregate_rewrite_workflow_api_key or self.dify_rewrite_workflow_api_key

    def validate_for_live_call(self) -> None:
        missing = []
        if not self.dify_clause_workflow_api_key:
            missing.append("DIFY_CLAUSE_WORKFLOW_API_KEY")
        if not self.anchored_risk_api_key():
            missing.append("DIFY_ANCHORED_RISK_WORKFLOW_API_KEY or DIFY_RISK_WORKFLOW_API_KEY")
        if not self.missing_multi_risk_api_key():
            missing.append("DIFY_MISSING_MULTI_RISK_WORKFLOW_API_KEY or DIFY_RISK_WORKFLOW_API_KEY")
        if not self.review_side:
            missing.append("REVIEW_SIDE")
        if self.fast_screen_enabled and not self.dify_fast_screen_workflow_api_key:
            missing.append("DIFY_FAST_SCREEN_WORKFLOW_API_KEY (required when FAST_SCREEN_ENABLED=1)")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


settings = Settings()
