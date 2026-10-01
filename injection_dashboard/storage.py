from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.fernet import Fernet, InvalidToken


def _secret(secrets: Any, name: str, default: Optional[str] = None) -> Optional[str]:
    try:
        value = secrets.get(name, default)
        return str(value) if value is not None else None
    except Exception:
        return os.getenv(name, default)


class SnapshotStore:
    def __init__(self, secrets: Any):
        self.repo = _secret(secrets, "GITHUB_DATA_REPO", "lhs5435555-star/autocoin")
        self.branch = _secret(secrets, "GITHUB_DATA_BRANCH", "injection-dashboard-render")
        self.path = _secret(secrets, "GITHUB_DATA_PATH", "injection_dashboard/data/snapshots.enc")
        self.token = _secret(secrets, "GITHUB_TOKEN")
        self.key = _secret(secrets, "DATA_ENCRYPTION_KEY")
        if not self.key:
            raise RuntimeError("DATA_ENCRYPTION_KEY 환경변수가 설정되지 않았습니다.")
        try:
            self.fernet = Fernet(self.key.encode("utf-8"))
        except Exception as exc:
            raise RuntimeError("DATA_ENCRYPTION_KEY 형식이 올바르지 않습니다.") from exc
        self.remote = True

    @property
    def backend_name(self) -> str:
        return "Encrypted GitHub"

    def _headers(self, write: bool = False) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "injection-dashboard",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if write and not self.token:
            raise RuntimeError("GITHUB_TOKEN이 없어 GitHub에 데이터를 저장할 수 없습니다.")
        return headers

    def _api_url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{self.path}"

    def _fetch_blob(self) -> Tuple[bytes, Optional[str]]:
        response = requests.get(
            self._api_url(),
            headers=self._headers(write=False),
            params={"ref": self.branch},
            timeout=15,
        )
        if response.status_code == 404:
            return b"", None
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload.get("content", ""))
        return content, payload.get("sha")

    def _load_payload(self) -> Dict[str, Any]:
        encrypted, _ = self._fetch_blob()
        if not encrypted:
            return {"snapshots": []}
        try:
            decrypted = self.fernet.decrypt(encrypted)
        except InvalidToken as exc:
            raise RuntimeError("GitHub 데이터 암호 해독에 실패했습니다. DATA_ENCRYPTION_KEY를 확인하세요.") from exc
        data = json.loads(decrypted.decode("utf-8"))
        if not isinstance(data, dict):
            return {"snapshots": []}
        return data

    def _save_payload(self, payload: Dict[str, Any], message: str) -> None:
        _, current_sha = self._fetch_blob()
        plaintext = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        encrypted = self.fernet.encrypt(plaintext)
        body: Dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(encrypted).decode("ascii"),
            "branch": self.branch,
        }
        if current_sha:
            body["sha"] = current_sha
        response = requests.put(
            self._api_url(),
            headers=self._headers(write=True),
            json=body,
            timeout=20,
        )
        if response.status_code == 409:
            _, latest_sha = self._fetch_blob()
            if latest_sha:
                body["sha"] = latest_sha
            response = requests.put(
                self._api_url(),
                headers=self._headers(write=True),
                json=body,
                timeout=20,
            )
        response.raise_for_status()

    def list_snapshots(self) -> List[Dict[str, Any]]:
        data = self._load_payload()
        snapshots = data.get("snapshots", [])
        if not isinstance(snapshots, list):
            return []
        valid = [s for s in snapshots if isinstance(s, dict) and "year" in s and "month" in s]
        return sorted(valid, key=lambda x: (int(x.get("year", 0)), int(x.get("month", 0))), reverse=True)

    def save_snapshot(self, snapshot: Dict[str, Any]) -> None:
        snapshot = dict(snapshot)
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
        year = int(snapshot["year"])
        month = int(snapshot["month"])

        snapshots = self.list_snapshots()
        snapshots = [s for s in snapshots if not (int(s.get("year", 0)) == year and int(s.get("month", 0)) == month)]
        snapshots.append(snapshot)
        snapshots.sort(key=lambda x: (int(x.get("year", 0)), int(x.get("month", 0))), reverse=True)
        self._save_payload({"snapshots": snapshots}, f"Update dashboard data: {year}-{month:02d}")

    def delete_snapshot(self, year: int, month: int) -> None:
        year = int(year)
        month = int(month)
        snapshots = self.list_snapshots()
        snapshots = [s for s in snapshots if not (int(s.get("year", 0)) == year and int(s.get("month", 0)) == month)]
        self._save_payload({"snapshots": snapshots}, f"Delete dashboard data: {year}-{month:02d}")
