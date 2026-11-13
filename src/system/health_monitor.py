from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from typing import Optional

import requests

from config.settings import get_settings

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors external service availability and system resources."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._results: dict[str, dict] = {}

    def check_all(self) -> dict[str, dict]:
        self._results = {
            "ollama": self.check_ollama(),
            "comfyui": self.check_comfyui(),
            "ffmpeg": self.check_ffmpeg(),
            "disk": self.check_disk(),
            "ram": self.check_ram(),
        }
        failed = [k for k, v in self._results.items() if not v.get("ok")]
        if failed:
            logger.warning("Health check failures: %s", failed)
        else:
            logger.info("All health checks passed")
        return self._results

    def check_ollama(self) -> dict:
        try:
            resp = requests.get(
                f"{self.settings.ollama_host}/api/tags", timeout=10
            )
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                has_model = self.settings.ollama_model in models
                return {
                    "ok": True,
                    "service": "ollama",
                    "model_available": has_model,
                    "detail": f"Found {len(models)} models" if has_model else f"Model '{self.settings.ollama_model}' not found",
                }
            return {"ok": False, "service": "ollama", "detail": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "service": "ollama", "detail": str(e)}

    def check_comfyui(self) -> dict:
        try:
            resp = requests.get(
                f"{self.settings.comfyui_host}/system_stats", timeout=10
            )
            if resp.status_code == 200:
                return {"ok": True, "service": "comfyui", "detail": "Running"}
            return {"ok": False, "service": "comfyui", "detail": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "service": "comfyui", "detail": str(e)}

    def check_ffmpeg(self) -> dict:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.split("\n")[0] if result.stdout else "unknown"
                return {"ok": True, "service": "ffmpeg", "detail": version}
            return {"ok": False, "service": "ffmpeg", "detail": result.stderr[:200]}
        except Exception as e:
            return {"ok": False, "service": "ffmpeg", "detail": str(e)}

    def check_disk(self) -> dict:
        try:
            usage = shutil.disk_usage(self.settings.project_root)
            percent = (usage.used / usage.total) * 100
            free_gb = usage.free / (1024 ** 3)
            warnings = []
            if percent > self.settings.storage_warning_threshold:
                warnings.append(f"Disk usage {percent:.1f}% exceeds {self.settings.storage_warning_threshold}%")
            if free_gb < self.settings.free_storage_warning_gb:
                warnings.append(f"Free space {free_gb:.1f}GB below {self.settings.free_storage_warning_gb}GB")
            return {
                "ok": len(warnings) == 0,
                "service": "disk",
                "percent_used": round(percent, 1),
                "free_gb": round(free_gb, 1),
                "total_gb": round(usage.total / (1024**3), 1),
                "warnings": warnings,
                "detail": "; ".join(warnings) if warnings else f"{percent:.1f}% used, {free_gb:.1f}GB free",
            }
        except Exception as e:
            return {"ok": False, "service": "disk", "detail": str(e)}

    def check_ram(self) -> dict:
        try:
            import psutil
            mem = psutil.virtual_memory()
            return {
                "ok": mem.percent < 90,
                "service": "ram",
                "percent_used": mem.percent,
                "available_gb": round(mem.available / (1024**3), 1),
                "total_gb": round(mem.total / (1024**3), 1),
                "detail": f"{mem.percent}% used, {mem.available / (1024**3):.1f}GB free",
            }
        except ImportError:
            return {"ok": True, "service": "ram", "detail": "psutil not installed, skipping"}
        except Exception as e:
            return {"ok": False, "service": "ram", "detail": str(e)}

    def get_summary(self) -> str:
        if not self._results:
            self.check_all()
        parts = ["## Health Check Summary\n"]
        for name, result in self._results.items():
            status = "✓" if result.get("ok") else "✗"
            detail = result.get("detail", "")
            parts.append(f"- {status} **{name}**: {detail}")
        return "\n".join(parts)

    def all_ok(self) -> bool:
        if not self._results:
            self.check_all()
        return all(r.get("ok", False) for r in self._results.values())
