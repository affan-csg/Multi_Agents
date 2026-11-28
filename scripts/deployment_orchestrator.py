from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable


DIGEST_PREFIX = "sha256:"
DEFAULT_HEALTH_CHECK_TIMEOUT = float(os.environ.get("DEPLOYMENT_HEALTH_CHECK_TIMEOUT", "30"))


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentReceipt:
    commit_sha: str
    build_id: str
    image: str
    image_digest: str
    api_previous_revision: str
    api_new_revision: str
    worker_previous_revision: str
    worker_new_revision: str


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, capture_output=True, text=True)


def normalize_digest(value: str) -> str:
    """Normalize a digest to ``sha256:<64 lowercase hex>``.

    Accepts either a full ``sha256:<hex>`` digest or a bare 64-character
    hexadecimal digest. Rejects anything else.
    """
    if not isinstance(value, str):
        raise DeploymentError("digest must be a string")
    value = value.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "sha256:" + value
    raise DeploymentError(f"Invalid sha256 manifest digest: {value!r}")


def require_digest(image: str) -> str:
    if "@" not in image:
        raise DeploymentError("Deployment image must use an immutable @sha256 manifest digest")
    digest = image.rsplit("@", 1)[1]
    return normalize_digest(digest)


def gcloud_command(*args: str) -> list[str]:
    """Resolve the gcloud CLI invocation.

    Prefers an operator-overridable path via ``CSG_GCLOUD_PYTHON`` (or a
    ``gcloud`` on PATH), and falls back to the Google Cloud SDK's Python
    entrypoint so the orchestrator runs on hosts without a shell-script-safe
    ``gcloud`` binary (CI containers, Windows).
    """
    override = (os.environ.get("CSG_GCLOUD_PYTHON") or "").strip()
    if override:
        return ["python", override, *args]
    if shutil.which("gcloud"):
        return ["gcloud", *args]
    return ["python", "C:\\gcloud\\google-cloud-sdk\\lib\\gcloud.py", *args]


def gcloud_json(args: list[str]) -> dict:
    result = run([*gcloud_command(*args), "--format=json"])
    return json.loads(result.stdout)


def active_revision(service: str, region: str, project: str) -> str:
    data = gcloud_json([
        "run", "services", "describe", service,
        "--region", region,
        "--project", project,
    ])
    for status in data.get("status", {}).get("traffic", []):
        if status.get("percent") == 100:
            return status["revisionName"]
    raise DeploymentError(f"Could not determine 100%-traffic revision for {service}")


def _latest_created_revision(data: dict) -> str | None:
    status = data.get("status") or {}
    name = status.get("latestCreatedRevisionName") or status.get("latestReadyRevisionName")
    return name if isinstance(name, str) and name else None


def mint_identity_token(audience: str) -> str:
    """Mint a Google ID token for a private Cloud Run service.

    The audience must be the service/revision URL. Returns the token or
    raises DeploymentError. Never prints the token.

    User credentials cannot mint audience-scoped ID tokens, so the deployer
    can set DEPLOYMENT_INVOKER_SERVICE_ACCOUNT to impersonate the runtime
    identity that must invoke the private service (it needs
    iam.serviceAccounts.getOpenIdToken on that account).
    """
    command = list(gcloud_command("auth", "print-identity-token", f"--audiences={audience}"))
    impersonate = (os.environ.get("DEPLOYMENT_INVOKER_SERVICE_ACCOUNT") or "").strip()
    if impersonate:
        command.append(f"--impersonate-service-account={impersonate}")
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        raise DeploymentError(f"Could not mint identity token for {audience}: {exc}") from exc
    token = (result.stdout or "").strip()
    if not token:
        raise DeploymentError(f"gcloud returned an empty identity token for {audience}")
    return token


def http_ok(url: str, timeout: float = 5.0, headers: dict | None = None) -> tuple[bool, object]:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return 200 <= response.status < 300, body
    except Exception:
        return False, None


def verify_api(url: str, *, timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT, audience: str | None = None) -> bool:
    # Cloud Run answers 404 for the reserved path /healthz before the request
    # reaches the container, so probe the alias /livez instead.
    ok, body = http_ok(url.rstrip("/") + "/livez", timeout=timeout)
    return bool(ok and isinstance(body, dict) and body.get("status") == "ok")


def verify_worker(url: str, *, timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT, audience: str | None = None) -> bool:
    # Best-effort identity token for the private worker. If minting fails we
    # proceed without a token; the private service will reject the request and
    # verification fails closed. The request URL and token audience are kept
    # separate so the exact revision URL can be checked while the service URL
    # is used as the ID-token audience.
    token_audience = audience if audience else url
    token = None
    try:
        token = mint_identity_token(token_audience)
    except DeploymentError:
        token = None
    headers = {"Authorization": f"Bearer {token}"} if token else None
    # /healthz is reserved by Cloud Run's front end (404), so the liveness
    # probe uses /livez; /readyz stays the readiness gate.
    ok_health, health = http_ok(url.rstrip("/") + "/livez", timeout=timeout, headers=headers)
    ok_ready, ready = http_ok(url.rstrip("/") + "/readyz", timeout=timeout, headers=headers)
    if not ok_health or not isinstance(health, dict):
        return False
    if health.get("status") != "ok" or health.get("worker_enabled") is not True:
        return False
    if health.get("gemini_queue_backend") != "shared_supabase":
        return False
    return bool(ok_ready and isinstance(ready, dict) and ready.get("status") == "ready")


def revision_image_digest(service: str, revision: str, region: str, project: str) -> str:
    data = gcloud_json([
        "run", "revisions", "describe", revision,
        "--region", region,
        "--project", project,
    ])
    image_digest = (data.get("status") or {}).get("imageDigest")
    if isinstance(image_digest, str):
        try:
            return normalize_digest(image_digest)
        except DeploymentError:
            pass
    spec = data.get("spec") or {}
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        template = spec.get("template") or {}
        containers = (template.get("spec") or {}).get("containers") or []
    if containers and isinstance(containers[0], dict):
        image = containers[0].get("image", "")
        if isinstance(image, str) and "@" in image:
            try:
                return normalize_digest(image.rsplit("@", 1)[1])
            except DeploymentError:
                pass
    raise DeploymentError(f"Could not determine image digest for revision {revision}")


def revision_runtime_config(service: str, revision: str, region: str, project: str) -> dict:
    """Runtime settings that a new revision must inherit from the previous one.

    Deploying with only ``--image`` must not silently change the runtime
    service account, container port, environment variable names, or the
    container command/args. Values are not compared (they can be secrets);
    only their names are.
    """
    data = gcloud_json([
        "run", "revisions", "describe", revision,
        "--region", region,
        "--project", project,
    ])
    spec = data.get("spec") or {}
    containers = spec.get("containers") or []
    container = containers[0] if containers and isinstance(containers[0], dict) else {}
    ports = container.get("ports") or []
    port = ports[0].get("containerPort") if ports and isinstance(ports[0], dict) else None
    return {
        "service_account": spec.get("serviceAccountName") or "",
        "port": port,
        "env": sorted(
            entry.get("name", "")
            for entry in container.get("env") or []
            if isinstance(entry, dict)
        ),
        "command": list(container.get("command") or []),
        "args": list(container.get("args") or []),
    }


def _config_diff(expected: dict, actual: dict) -> str:
    keys = sorted(set(expected) | set(actual))
    return ", ".join(
        f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}"
        for key in keys
        if expected.get(key) != actual.get(key)
    )


def _parse_revision_from_output(output: str, service: str) -> str | None:
    patterns = (
        r"revision\s*\[([^\]]+)\]",
        r"Revision:\s*(\S+)",
        r"latestReadyRevisionName['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, output or "")
        if match:
            candidate = match.group(1).rstrip(".,")
            if candidate:
                return candidate
    return None


def wait_for_new_revision(
    service: str,
    region: str,
    project: str,
    previous_revision: str | None,
    *,
    digest: str | None = None,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> str:
    deadline = time.monotonic() + timeout
    normalized_digest = normalize_digest(digest) if digest else None
    while True:
        try:
            data = gcloud_json([
                "run", "services", "describe", service,
                "--region", region,
                "--project", project,
            ])
        except Exception:
            data = {}
        name = _latest_created_revision(data) if data else None
        if name and name != previous_revision:
            if normalized_digest is None:
                return name
            try:
                actual = revision_image_digest(service, name, region, project)
            except DeploymentError:
                actual = None
            if actual == normalized_digest:
                return name
        if time.monotonic() >= deadline:
            raise DeploymentError(
                f"Timed out waiting for new revision of {service}"
                + (f" matching {normalized_digest}" if normalized_digest else "")
            )
        time.sleep(poll_interval)


def deploy_revision(
    service: str,
    region: str,
    project: str,
    image: str,
    *,
    worker: bool = False,
    digest: str | None = None,
) -> str:
    # Capture the pre-deploy latest created revision so we can detect the new one.
    previous_revision = None
    try:
        pre_data = gcloud_json([
            "run", "services", "describe", service,
            "--region", region,
            "--project", project,
        ])
        previous_revision = _latest_created_revision(pre_data)
    except Exception:
        previous_revision = None

    resolved_digest = digest
    if resolved_digest is None:
        try:
            resolved_digest = require_digest(image)
        except DeploymentError:
            resolved_digest = None

    command = [
        *gcloud_command("run", "deploy", service),
        "--region", region,
        "--project", project,
        "--image", image,
        "--no-traffic",
        "--quiet",
    ]
    if worker:
        # The Research Worker must never become publicly invokable. Passing the
        # flag explicitly makes the revision private even if the previous
        # service configuration was drifted open; `gcloud run deploy` keeps
        # every other existing setting (env, secrets, probes, scaling, CPU,
        # memory, timeout, ingress) because only --image/--command/--args are set.
        command.extend(["--command", "python", "--args=-m,csg.worker", "--no-allow-unauthenticated"])
    result = run(command)
    output = result.stdout or ""
    revision = _parse_revision_from_output(output, service)
    if revision:
        return revision
    return wait_for_new_revision(service, region, project, previous_revision, digest=resolved_digest)


def switch_traffic(service: str, revision: str, region: str, project: str) -> None:
    run([
        *gcloud_command("run", "services", "update-traffic", service),
        "--region", region,
        "--project", project,
        "--to-revisions", f"{revision}=100",
        "--quiet",
    ])


def rollback(service: str, revision: str, region: str, project: str) -> None:
    switch_traffic(service, revision, region, project)


def revision_url(service: str, revision: str, region: str, project: str) -> str:
    data = gcloud_json([
        "run", "revisions", "describe", revision,
        "--region", region,
        "--project", project,
    ])
    url = data.get("status", {}).get("url")
    if not isinstance(url, str) or not url:
        raise DeploymentError(f"Could not determine URL for revision {revision}")
    return url


def orchestrate_deployment(
    *,
    project: str,
    region: str,
    commit_sha: str,
    build_id: str,
    image: str,
    api_service: str,
    worker_service: str,
    api_url: str = "",
    worker_url: str = "",
    verify_api_fn: Callable[[str], bool] = verify_api,
    verify_worker_fn: Callable[[str], bool] = verify_worker,
    deploy_fn: Callable[..., str] = deploy_revision,
    switch_fn: Callable[..., None] = switch_traffic,
    rollback_fn: Callable[..., None] = rollback,
    revision_url_fn: Callable[..., str] = revision_url,
    revision_image_digest_fn: Callable[[str, str, str, str], str] = revision_image_digest,
    revision_config_fn: Callable[[str, str, str, str], dict] = revision_runtime_config,
) -> DeploymentReceipt:
    digest = require_digest(image)

    api_previous = active_revision(api_service, region, project)
    worker_previous = active_revision(worker_service, region, project)
    api_previous_config = revision_config_fn(api_service, api_previous, region, project)
    worker_previous_config = revision_config_fn(worker_service, worker_previous, region, project)

    api_new = ""
    worker_new = ""
    api_switched = False
    worker_switched = False

    try:
        api_new = deploy_fn(api_service, region, project, image, worker=False, digest=digest)
        api_revision_url = revision_url_fn(api_service, api_new, region, project)

        actual_api_digest = normalize_digest(revision_image_digest_fn(api_service, api_new, region, project))
        if actual_api_digest != digest:
            raise DeploymentError(
                f"API revision {api_new} image digest mismatch: deployed={actual_api_digest}, requested={digest}"
            )

        actual_api_config = revision_config_fn(api_service, api_new, region, project)
        if actual_api_config != api_previous_config:
            raise DeploymentError(
                f"API revision {api_new} did not preserve the previous runtime configuration: "
                + _config_diff(api_previous_config, actual_api_config)
            )

        if not verify_api_fn(api_revision_url, audience=api_url or None):
            raise DeploymentError(f"API revision {api_new} failed /healthz verification")

        worker_new = deploy_fn(worker_service, region, project, image, worker=True, digest=digest)
        worker_revision_url = revision_url_fn(worker_service, worker_new, region, project)

        actual_worker_digest = normalize_digest(revision_image_digest_fn(worker_service, worker_new, region, project))
        if actual_worker_digest != digest:
            raise DeploymentError(
                f"Worker revision {worker_new} image digest mismatch: deployed={actual_worker_digest}, requested={digest}"
            )

        actual_worker_config = revision_config_fn(worker_service, worker_new, region, project)
        if actual_worker_config != worker_previous_config:
            raise DeploymentError(
                f"Worker revision {worker_new} did not preserve the previous runtime configuration: "
                + _config_diff(worker_previous_config, actual_worker_config)
            )

        worker_audience = worker_url or None
        if not verify_worker_fn(worker_revision_url, audience=worker_audience):
            raise DeploymentError(f"Worker revision {worker_new} failed /healthz or /readyz verification")

        api_switched = True
        switch_fn(api_service, api_new, region, project)
        worker_switched = True
        switch_fn(worker_service, worker_new, region, project)

        if not verify_api_fn(api_revision_url, audience=api_url or None):
            raise DeploymentError("API verification failed after traffic switch")
        if not verify_worker_fn(worker_revision_url, audience=worker_url or None):
            raise DeploymentError("Worker verification failed after traffic switch")

    except Exception as exc:
        rollback_errors: list[str] = []
        if worker_switched:
            try:
                rollback_fn(worker_service, worker_previous, region, project)
            except Exception as rollback_exc:
                rollback_errors.append(f"worker rollback failed: {rollback_exc}")
        if api_switched:
            try:
                rollback_fn(api_service, api_previous, region, project)
            except Exception as rollback_exc:
                rollback_errors.append(f"api rollback failed: {rollback_exc}")
        if rollback_errors:
            raise DeploymentError(f"{exc}; {'; '.join(rollback_errors)}") from exc
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError(str(exc)) from exc

    return DeploymentReceipt(
        commit_sha=commit_sha,
        build_id=build_id,
        image=image,
        image_digest=digest,
        api_previous_revision=api_previous,
        api_new_revision=api_new,
        worker_previous_revision=worker_previous,
        worker_new_revision=worker_new,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="csgroup-514114")
    parser.add_argument("--region", default="asia-northeast1")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--api-url", default="")
    parser.add_argument("--worker-url", default="")
    parser.add_argument("--api-service", default="multi-agents")
    parser.add_argument("--worker-service", default="multi-agents-research-worker")
    args = parser.parse_args()

    try:
        receipt = orchestrate_deployment(
            project=args.project,
            region=args.region,
            commit_sha=args.commit,
            build_id=args.build_id,
            image=args.image,
            api_service=args.api_service,
            worker_service=args.worker_service,
            api_url=args.api_url,
            worker_url=args.worker_url,
        )
        print(json.dumps(receipt.__dict__, sort_keys=True))
        return 0
    except DeploymentError as exc:
        print(f"DEPLOYMENT_FAILED: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        print(f"DEPLOYMENT_FAILED: {exc}\n{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
