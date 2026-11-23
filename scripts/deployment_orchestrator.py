from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable


DIGEST_PREFIX = "sha256:"


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


def require_digest(image: str) -> str:
    if "@sha256:" not in image:
        raise DeploymentError("Deployment image must use an immutable @sha256 manifest digest")
    digest = image.rsplit("@", 1)[1]
    if len(digest) != 71 or not digest.startswith(DIGEST_PREFIX):
        raise DeploymentError("Invalid sha256 manifest digest")
    return digest


def gcloud_json(args: list[str]) -> dict:
    result = run(["gcloud", *args, "--format=json"])
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


def active_revision_digest(service: str, region: str, project: str) -> str:
    data = gcloud_json([
        "run", "services", "describe", service,
        "--region", region,
        "--project", project,
    ])
    image = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "")
    if "@sha256:" not in image:
        raise DeploymentError(f"Current {service} revision is not digest-pinned")
    return require_digest(image)


def revision_for_image(service: str, region: str, project: str, digest: str) -> str:
    data = gcloud_json([
        "run", "services", "describe", service,
        "--region", region,
        "--project", project,
    ])
    revisions = data.get("status", {}).get("traffic", [])
    for revision in revisions:
        if revision.get("percent") == 100:
            return revision["revisionName"]
    raise DeploymentError(f"Could not identify new {service} revision")


def http_ok(url: str, timeout: float = 5.0) -> tuple[bool, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return 200 <= response.status < 300, body
    except Exception:
        return False, None


def verify_api(url: str) -> bool:
    ok, body = http_ok(url.rstrip("/") + "/healthz")
    return bool(ok and isinstance(body, dict) and body.get("status") == "ok")


def verify_worker(url: str) -> bool:
    ok_health, health = http_ok(url.rstrip("/") + "/healthz")
    ok_ready, ready = http_ok(url.rstrip("/") + "/readyz")
    if not ok_health or not isinstance(health, dict):
        return False
    if health.get("status") != "ok" or health.get("worker_enabled") is not True:
        return False
    if health.get("gemini_queue_backend") != "shared_supabase":
        return False
    return bool(ok_ready and isinstance(ready, dict) and ready.get("status") == "ready")


def deploy_revision(
    service: str,
    region: str,
    project: str,
    image: str,
    *,
    worker: bool = False,
) -> str:
    command = [
        "gcloud", "run", "deploy", service,
        "--region", region,
        "--project", project,
        "--image", image,
        "--no-traffic",
        "--quiet",
    ]
    if worker:
        command.extend(["--command", "python", "--args=-m,csg.worker"])
    result = run(command)
    output = result.stdout
    for token in output.split():
        if token.startswith("multi-agents-research-worker-") or token.startswith("multi-agents-"):
            return token.rstrip(".,")
    data = gcloud_json([
        "run", "services", "describe", service,
        "--region", region,
        "--project", project,
    ])
    return data["status"]["latestReadyRevisionName"]


def switch_traffic(service: str, revision: str, region: str, project: str) -> None:
    run([
        "gcloud", "run", "services", "update-traffic", service,
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
    api_url: str,
    worker_url: str,
    verify_api_fn: Callable[[str], bool] = verify_api,
    verify_worker_fn: Callable[[str], bool] = verify_worker,
    deploy_fn: Callable[..., str] = deploy_revision,
    switch_fn: Callable[..., None] = switch_traffic,
    rollback_fn: Callable[..., None] = rollback,
    revision_url_fn: Callable[..., str] = revision_url,
) -> DeploymentReceipt:
    digest = require_digest(image)

    api_previous = active_revision(api_service, region, project)
    worker_previous = active_revision(worker_service, region, project)

    api_new = ""
    worker_new = ""
    api_switched = False
    worker_switched = False

    try:
        api_new = deploy_fn(api_service, region, project, image, worker=False)
        api_revision_url = revision_url_fn(api_service, api_new, region, project)
        if not verify_api_fn(api_revision_url):
            raise DeploymentError(f"API revision {api_new} failed /healthz verification")

        worker_new = deploy_fn(worker_service, region, project, image, worker=True)
        worker_revision_url = revision_url_fn(worker_service, worker_new, region, project)
        if not verify_worker_fn(worker_revision_url):
            raise DeploymentError(f"Worker revision {worker_new} failed /healthz or /readyz verification")

        api_switched = True
        switch_fn(api_service, api_new, region, project)
        worker_switched = True
        switch_fn(worker_service, worker_new, region, project)

        if not verify_api_fn(api_revision_url):
            raise DeploymentError("API verification failed after traffic switch")
        if not verify_worker_fn(worker_revision_url):
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
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--worker-url", required=True)
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


if __name__ == "__main__":
    raise SystemExit(main())
