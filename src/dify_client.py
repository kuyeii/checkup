from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


class DifyWorkflowError(RuntimeError):
    pass


@dataclass(slots=True)
class DifyWorkflowClient:
    base_url: str
    api_key: str
    timeout_seconds: int = 180

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url = f"{self.base_url}/v1"

    def run_workflow(
        self,
        *,
        inputs: dict[str, Any],
        user: str,
        response_mode: str = "blocking",
    ) -> dict[str, Any]:
        url = f"{self.base_url}/workflows/run"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": inputs,
            "response_mode": response_mode,
            "user": user,
        }
        response = requests.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise DifyWorkflowError(
                f"Workflow request failed: status={response.status_code}, body={response.text[:1000]}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise DifyWorkflowError(f"Workflow response is not valid JSON: {response.text[:1000]}") from exc



def extract_blocking_outputs(workflow_response: dict[str, Any]) -> dict[str, Any]:
    data = workflow_response.get("data") or {}
    status = str(data.get("status", "") or "").strip().lower()
    error = data.get("error")
    if error:
        raise DifyWorkflowError(f"Workflow returned error: {error}")
    if status in {"failed", "error", "stopped", "canceled", "cancelled"}:
        raise DifyWorkflowError(f"Workflow status indicates failure: status={status}, data={data}")
    outputs = data.get("outputs")
    if not isinstance(outputs, dict):
        raise DifyWorkflowError(f"Workflow outputs missing or invalid: {workflow_response}")
    return outputs
