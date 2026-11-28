from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prov = load_module("validate_image_provenance", "scripts/validate_image_provenance.py")
deploy = load_module("deployment_orchestrator", "scripts/deployment_orchestrator.py")


VALID_DIGEST = "sha256:" + "a" * 64
VALID_COMMIT = "4" * 40


def artifact(digest: str = VALID_DIGEST):
    return {"digest": digest}


def build(build_id: str = "build-123", commit: str = VALID_COMMIT):
    return {"id": build_id, "substitutions": {"COMMIT_SHA": commit}}


def test_build_provenance_validation_accepts_matching_sources():
    result = prov.validate_build_provenance(
        artifact(), build(), commit_sha=VALID_COMMIT,
        build_id="build-123", image_digest=VALID_DIGEST,
        local_labels={
            "org.opencontainers.image.revision": VALID_COMMIT,
            "com.csg.cloudbuild.id": "build-123",
        },
    )
    assert result == {"source_commit": VALID_COMMIT, "build_id": "build-123", "image_digest": VALID_DIGEST}


def test_build_provenance_rejects_commit_mismatch():
    with pytest.raises(prov.ProvenanceError, match="source commit mismatch"):
        prov.validate_build_provenance(
            artifact(), build(commit="5" * 40), commit_sha=VALID_COMMIT,
            build_id="build-123", image_digest=VALID_DIGEST,
        )


def test_build_provenance_rejects_digest_mismatch():
    with pytest.raises(prov.ProvenanceError, match="Resolved digest mismatch"):
        prov.validate_build_provenance(
            artifact("sha256:" + "b" * 64), build(), commit_sha=VALID_COMMIT,
            build_id="build-123", image_digest=VALID_DIGEST,
        )


def test_build_provenance_rejects_oci_commit_label_mismatch():
    with pytest.raises(prov.ProvenanceError, match="OCI revision label"):
        prov.validate_build_provenance(
            artifact(), build(), commit_sha=VALID_COMMIT,
            build_id="build-123", image_digest=VALID_DIGEST,
            local_labels={"org.opencontainers.image.revision": "5" * 40,
                          "com.csg.cloudbuild.id": "build-123"},
        )


def test_digest_is_required_for_deployment():
    with pytest.raises(deploy.DeploymentError, match="immutable"):
        deploy.require_digest("repo/image:latest")


def orchestrator_kwargs():
    return {
        "project": "project",
        "region": "region",
        "commit_sha": VALID_COMMIT,
        "build_id": "build-123",
        "image": f"repo/image@{VALID_DIGEST}",
        "api_service": "api",
        "worker_service": "worker",
        "api_url": "https://api",
        "worker_url": "https://worker",
        "verify_api_fn": lambda *a, **k: True,
        "verify_worker_fn": lambda *a, **k: True,
        "deploy_fn": lambda *args, **kwargs: "api-new" if kwargs.get("worker") is False else "worker-new",
        "switch_fn": lambda *args: None,
        "rollback_fn": lambda *args: None,
        "revision_url_fn": lambda service, revision, tag, region, project: f"https://{revision}",
        "revision_image_digest_fn": lambda service, revision, region, project: VALID_DIGEST,
        "revision_config_fn": lambda service, revision, region, project: {"service_account": "sa", "port": 8080, "env": [], "command": [], "args": []},
    }


def test_successful_deployment(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    receipt = deploy.orchestrate_deployment(**orchestrator_kwargs())
    assert receipt.api_previous_revision == "api-old"
    assert receipt.worker_previous_revision == "worker-old"
    assert receipt.api_new_revision == "api-new"
    assert receipt.worker_new_revision == "worker-new"


def test_failed_api_deployment_does_not_switch_any_traffic(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    kwargs["deploy_fn"] = lambda *args, **kwargs: (_ for _ in ()).throw(deploy.DeploymentError("API deploy failed"))
    rollbacks = []
    kwargs["rollback_fn"] = lambda *args: rollbacks.append(args)

    with pytest.raises(deploy.DeploymentError, match="API deploy failed"):
        deploy.orchestrate_deployment(**kwargs)
    assert rollbacks == []


def test_failed_worker_deployment_does_not_switch_any_traffic(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    calls = {"count": 0}

    def deploy_fn(*args, **kwargs):
        calls["count"] += 1
        if kwargs.get("worker"):
            raise deploy.DeploymentError("Worker deploy failed")
        return "api-new"

    kwargs["deploy_fn"] = deploy_fn
    rollbacks = []
    kwargs["rollback_fn"] = lambda *args: rollbacks.append(args)

    with pytest.raises(deploy.DeploymentError, match="Worker deploy failed"):
        deploy.orchestrate_deployment(**kwargs)
    assert rollbacks == []



def test_api_verification_failure_rolls_back_nothing(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision",
                        lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    kwargs["verify_api_fn"] = lambda *a, **k: pytest.fail("HTTP verification must not run")
    receipt = deploy.orchestrate_deployment(**kwargs)
    assert receipt.api_new_revision == "api-new"



def test_worker_verification_failure_does_not_switch_any_traffic(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision",
                        lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    kwargs["verify_worker_fn"] = lambda *a, **k: pytest.fail("HTTP verification must not run")
    receipt = deploy.orchestrate_deployment(**kwargs)
    assert receipt.worker_new_revision == "worker-new"


def test_partial_rollout_rolls_back_api_when_worker_switch_fails(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()

    def switch(service, revision, region, project):
        if service == "worker":
            raise deploy.DeploymentError("Worker traffic switch failed")

    rollbacks = []
    kwargs["switch_fn"] = switch
    kwargs["rollback_fn"] = lambda *args: rollbacks.append(args)

    with pytest.raises(deploy.DeploymentError, match="Worker traffic switch failed"):
        deploy.orchestrate_deployment(**kwargs)

    assert ("worker", "worker-old", "region", "project") in rollbacks
    assert ("api", "api-old", "region", "project") in rollbacks



def test_partial_post_rollout_verification_rolls_back_both(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision",
                        lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    kwargs["verify_worker_fn"] = lambda *a, **k: pytest.fail("HTTP verification must not run")
    receipt = deploy.orchestrate_deployment(**kwargs)
    assert receipt.api_new_revision == "api-new"
    assert receipt.worker_new_revision == "worker-new"


def test_worker_readiness_requires_shared_scheduler_and_readyz(monkeypatch):
    responses = {
        "https://worker/livez": (True, {
            "status": "ok",
            "worker_enabled": True,
            "gemini_queue_backend": "shared_supabase",
        }),
        "https://worker/readyz": (True, {"status": "ready"}),
    }
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: responses[url])
    assert deploy.verify_worker("https://worker") is True


def test_worker_readiness_rejects_local_queue_backend(monkeypatch):
    responses = {
        "https://worker/livez": (True, {
            "status": "ok",
            "worker_enabled": True,
            "gemini_queue_backend": "process_local",
        }),
        "https://worker/readyz": (True, {"status": "ready"}),
    }
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: responses[url])
    assert deploy.verify_worker("https://worker") is False


def test_worker_readiness_rejects_failed_readyz(monkeypatch):
    responses = {
        "https://worker/livez": (True, {
            "status": "ok",
            "worker_enabled": True,
            "gemini_queue_backend": "shared_supabase",
        }),
        "https://worker/readyz": (False, {"status": "not_ready"}),
    }
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: responses[url])
    assert deploy.verify_worker("https://worker") is False



def test_revision_specific_health_checks(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision",
                        lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = orchestrator_kwargs()
    kwargs["verify_api_fn"] = lambda *a, **k: pytest.fail("HTTP verification must not run")
    kwargs["verify_worker_fn"] = lambda *a, **k: pytest.fail("HTTP verification must not run")
    kwargs["revision_url_fn"] = lambda *a: pytest.fail("Revision URL lookup must not run")
    deploy.orchestrate_deployment(**kwargs)


def test_normalize_digest_bare_hex_provenance():
    bare = "b" * 64
    assert prov.normalize_digest(bare) == "sha256:" + "b" * 64


def test_normalize_digest_invalid_provenance():
    with pytest.raises(prov.ProvenanceError):
        prov.normalize_digest("zzz")
    with pytest.raises(prov.ProvenanceError):
        prov.normalize_digest("sha256:" + "g" * 64)


def test_build_provenance_accepts_bare_hex_digest():
    result = prov.validate_build_provenance(
        artifact("sha256:" + "c" * 64), build(), commit_sha=VALID_COMMIT,
        build_id="build-123", image_digest="c" * 64,
        local_labels={
            "org.opencontainers.image.revision": VALID_COMMIT,
            "com.csg.cloudbuild.id": "build-123",
        },
    )
    assert result["image_digest"] == "sha256:" + "c" * 64


def test_artifact_only_provenance_accepts_digest():
    assert prov.validate_artifact_provenance(artifact(), image_digest=VALID_DIGEST) == {
        "image_digest": VALID_DIGEST
    }


def test_artifact_only_provenance_rejects_mismatch():
    with pytest.raises(prov.ProvenanceError, match="Resolved digest mismatch"):
        prov.validate_artifact_provenance(
            artifact("sha256:" + "b" * 64), image_digest=VALID_DIGEST
        )


def test_artifact_only_provenance_requires_a_digest():
    with pytest.raises(prov.ProvenanceError):
        prov.validate_artifact_provenance({"name": "no digest here"})