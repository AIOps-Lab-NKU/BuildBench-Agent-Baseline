"""Compatibility backend for the original online OBS workflow."""

from __future__ import annotations

from pathlib import Path

from tools.auto_repair.check_build_res import check_main
from tools.auto_repair.upload_files import main_upload

from .models import BuildRequest, BuildResult


class ObsBuildBackend:
    name = "obs"

    def build(self, request: BuildRequest) -> BuildResult:
        package_name = request.package_name
        workspace = request.workspace
        upload_result = main_upload(package_name, str(workspace))
        upload_text = str(upload_result)
        if "error" in upload_text.lower() or "failed" in upload_text.lower():
            return BuildResult(
                backend=self.name,
                status="infrastructure_error",
                success=False,
                message=f"OBS upload failed: {upload_text}",
                case_id=package_name,
            )

        raw_result = str(check_main(str(workspace), package_name))
        lowered = raw_result.lower()
        success = "build succeeded" in lowered or lowered.strip() in {
            "succeeded",
            "success",
            "passed",
        }
        status = "succeeded" if success else "failed"
        if "unresolvable" in lowered:
            status = "unresolvable"
        elif "timeout" in lowered:
            status = "timeout"

        log_path = workspace / "log_failed.txt"
        return BuildResult(
            backend=self.name,
            status=status,
            success=success,
            message=raw_result,
            case_id=package_name,
            timed_out=status == "timeout",
            log_path=str(log_path) if log_path.is_file() else None,
        )
