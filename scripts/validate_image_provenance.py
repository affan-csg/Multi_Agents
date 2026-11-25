from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ProvenanceError(RuntimeError):
    pass


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def extract_artifact_digest(artifact: dict[str, Any]) -> str:
    summary = artifact.get("image_summary") or artifact.get("imageSummary") or {}
    candidates = (
        artifact.get("digest"),
        summary.get("digest"),
        artifact.get("imageName"),
        artifact.get("name"),
        artifact.get("uri"),
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            match = re.search(r"(sha256:[0-9a-f]{64})", candidate)
            if match:
                return match.group(1)
    raise ProvenanceError("Artifact Registry response did not contain an image digest")


def extract_build_commit(build: dict[str, Any]) -> str:
    substitutions = build.get("substitutions") or {}
    commit = substitutions.get("COMMIT_SHA")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        return commit

    source = build.get("sourceProvenance") or build.get("source") or {}
    resolved = source.get("resolvedRepoSource") or source.get("resolvedSource") or {}
    if isinstance(resolved, dict):
        commit = resolved.get("commitSha")
        if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit

    repo_source = (build.get("source") or {}).get("repoSource") or {}
    commit = repo_source.get("commitSha")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        return commit

    raise ProvenanceError("Cloud Build response did not contain a verified 40-character commit SHA")


def normalize_digest(value: str) -> str:
    if not isinstance(value, str):
        raise ProvenanceError("digest must be a string")
    value = value.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "sha256:" + value
    raise ProvenanceError(f"Invalid image digest: {value!r}")


def validate_artifact_provenance(
    artifact: dict[str, Any], *, image_digest: str | None = None
) -> dict[str, str]:
    """Verify the Artifact Registry record for the pushed tag resolves to a digest."""
    actual_digest = normalize_digest(extract_artifact_digest(artifact))
    if image_digest and normalize_digest(image_digest) != actual_digest:
        raise ProvenanceError(
            f"Resolved digest mismatch: requested {image_digest}, Artifact Registry reports {actual_digest}"
        )
    return {"image_digest": actual_digest}


def validate_build_provenance(
    artifact: dict[str, Any],
    build: dict[str, Any],
    *,
    commit_sha: str,
    build_id: str,
    image_digest: str | None = None,
    local_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ProvenanceError("SOURCE_COMMIT must be a full 40-character Git SHA")
    resolved_image_digest = normalize_digest(image_digest) if image_digest else None
    if resolved_image_digest is None:
        resolved_image_digest = normalize_digest(extract_artifact_digest(artifact))

    actual_digest = normalize_digest(extract_artifact_digest(artifact))
    if resolved_image_digest != actual_digest:
        raise ProvenanceError(
            f"Resolved digest mismatch: requested {resolved_image_digest}, Artifact Registry reports {actual_digest}"
        )

    build_commit = extract_build_commit(build)
    if build_commit != commit_sha:
        raise ProvenanceError(
            f"Cloud Build source commit mismatch: build={build_commit}, requested={commit_sha}"
        )

    actual_build_id = build.get("id") or build.get("buildId")
    if actual_build_id and actual_build_id != build_id:
        raise ProvenanceError(
            f"Cloud Build ID mismatch: build={actual_build_id}, requested={build_id}"
        )

    if local_labels:
        if local_labels.get("org.opencontainers.image.revision") != commit_sha:
            raise ProvenanceError("Docker image OCI revision label does not match the source commit")
        if local_labels.get("com.csg.cloudbuild.id") != build_id:
            raise ProvenanceError("Docker image OCI build-id label does not match the Cloud Build ID")

    return {
        "source_commit": commit_sha,
        "build_id": build_id,
        "image_digest": actual_digest,
    }


def inspect_local_labels(image_ref: str) -> dict[str, str]:
    result = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    images = json.loads(result.stdout)
    if not images:
        raise ProvenanceError("docker image inspect returned no image")
    return images[0].get("Config", {}).get("Labels") or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-ref", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--local-image", required=False)
    parser.add_argument(
        "--skip-build-metadata",
        action="store_true",
        help=(
            "Validate only the Artifact Registry record. Reading the Cloud Build record needs "
            "cloudbuild.builds.get, which Cloud Build build identities do not have by default."
        ),
    )
    args = parser.parse_args()

    try:
        artifact_ref = args.artifact_ref
        described = _run_json([
            "gcloud", "artifacts", "docker", "images", "describe",
            artifact_ref, "--format=json",
        ])
        image_digest = extract_artifact_digest(described)
        if args.skip_build_metadata:
            result = {
                "requested_commit": args.commit,
                "requested_build_id": args.build_id,
                **validate_artifact_provenance(described, image_digest=image_digest),
            }
            print(json.dumps({**result}, sort_keys=True))
            return 0
        artifact = described

        build = _run_json([
            "gcloud", "builds", "describe", args.build_id, "--format=json",
        ])
        local_labels = inspect_local_labels(args.local_image) if args.local_image else None
        result = validate_build_provenance(
            artifact,
            build,
            commit_sha=args.commit,
            build_id=args.build_id,
            image_digest=image_digest,
            local_labels=local_labels,
        )
        print(json.dumps({**result}, sort_keys=True))
        return 0
    except (ProvenanceError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = exc
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = f"{exc}\n{exc.stderr.strip()}"
        print(f"PROVENANCE_VALIDATION_FAILED: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
