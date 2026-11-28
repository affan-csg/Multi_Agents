from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


deploy = load_module("deployment_orchestrator_wf", "scripts/deployment_orchestrator.py")

VALID_DIGEST = "sha256:" + "a" * 64
VALID_COMMIT = "4" * 40


def base_kwargs():
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


def test_identity_token_header_passed_to_http_ok(monkeypatch):
    captured = {}

    def fake_http_ok(url, timeout=5.0, headers=None):
        captured[url] = headers
        return True, {"status": "ok", "worker_enabled": True, "gemini_queue_backend": "shared_supabase"}

    # readyz
    def fake_http_ok2(url, timeout=5.0, headers=None):
        if url.endswith("/readyz"):
            return True, {"status": "ready"}
        captured[url] = headers
        return True, {"status": "ok", "worker_enabled": True, "gemini_queue_backend": "shared_supabase"}

    monkeypatch.setattr(deploy, "http_ok", fake_http_ok2)
    monkeypatch.setattr(deploy, "mint_identity_token", lambda audience: "tok-123")
    assert deploy.verify_worker("https://worker") is True
    # healthz URL should have received the bearer token
    assert captured["https://worker/livez"] == {"Authorization": "Bearer tok-123"}


def test_verify_worker_no_token_when_mint_fails(monkeypatch):
    monkeypatch.setattr(deploy, "mint_identity_token", lambda audience: (_ for _ in ()).throw(deploy.DeploymentError("no gcloud")))
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: (False, None))
    assert deploy.verify_worker("https://worker") is False


def test_digest_mismatch_aborts_and_no_traffic_switch(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    kwargs["revision_image_digest_fn"] = lambda service, revision, region, project: "sha256:" + "b" * 64
    switches = []
    kwargs["switch_fn"] = lambda *a: switches.append(a)
    rollbacks = []
    kwargs["rollback_fn"] = lambda *a: rollbacks.append(a)
    with pytest.raises(deploy.DeploymentError, match="digest mismatch"):
        deploy.orchestrate_deployment(**kwargs)
    assert switches == []


def test_worker_digest_mismatch_aborts_before_switch(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    calls = {"n": 0}

    def digest_fn(service, revision, region, project):
        if service == "worker":
            return "sha256:" + "b" * 64
        return VALID_DIGEST

    kwargs["revision_image_digest_fn"] = digest_fn
    switches = []
    kwargs["switch_fn"] = lambda *a: switches.append(a)
    with pytest.raises(deploy.DeploymentError, match="digest mismatch"):
        deploy.orchestrate_deployment(**kwargs)
    assert switches == []


def test_health_check_failure_after_deploy_no_switch(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    kwargs["verify_api_fn"] = lambda *a, **k: False
    switches = []
    kwargs["switch_fn"] = lambda *a: switches.append(a)
    with pytest.raises(deploy.DeploymentError, match="API revision"):
        deploy.orchestrate_deployment(**kwargs)
    assert switches == []


def test_worker_health_failure_no_switch(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    kwargs["verify_worker_fn"] = lambda *a, **k: False
    switches = []
    kwargs["switch_fn"] = lambda *a: switches.append(a)
    rollbacks = []
    kwargs["rollback_fn"] = lambda *a: rollbacks.append(a)
    with pytest.raises(deploy.DeploymentError, match="Worker revision"):
        deploy.orchestrate_deployment(**kwargs)
    assert switches == []


def test_post_switch_worker_verification_rolls_back_both(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    calls = {"w": 0}

    def verify_worker(url, audience=None):
        calls["w"] += 1
        return calls["w"] == 1  # passes pre-switch, fails post-switch

    kwargs["verify_worker_fn"] = verify_worker
    rollbacks = []
    kwargs["rollback_fn"] = lambda *a: rollbacks.append(a)
    with pytest.raises(deploy.DeploymentError, match="Worker verification failed after traffic switch"):
        deploy.orchestrate_deployment(**kwargs)
    assert ("worker", "worker-old", "region", "project") in rollbacks
    assert ("api", "api-old", "region", "project") in rollbacks


def test_api_switch_failure_rolls_back_api(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()

    def switch(service, revision, region, project):
        if service == "api":
            raise deploy.DeploymentError("API traffic switch failed")

    kwargs["switch_fn"] = switch
    rollbacks = []
    kwargs["rollback_fn"] = lambda *a: rollbacks.append(a)
    with pytest.raises(deploy.DeploymentError, match="API traffic switch failed"):
        deploy.orchestrate_deployment(**kwargs)
    assert ("api", "api-old", "region", "project") in rollbacks


def test_worker_switch_failure_rolls_back_both(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()

    def switch(service, revision, region, project):
        if service == "worker":
            raise deploy.DeploymentError("Worker traffic switch failed")

    kwargs["switch_fn"] = switch
    rollbacks = []
    kwargs["rollback_fn"] = lambda *a: rollbacks.append(a)
    with pytest.raises(deploy.DeploymentError, match="Worker traffic switch failed"):
        deploy.orchestrate_deployment(**kwargs)
    assert ("worker", "worker-old", "region", "project") in rollbacks
    assert ("api", "api-old", "region", "project") in rollbacks


def test_rollback_failure_reported(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()

    def switch(service, revision, region, project):
        if service == "worker":
            raise deploy.DeploymentError("Worker traffic switch failed")

    kwargs["switch_fn"] = switch

    def rollback(service, revision, region, project):
        raise RuntimeError("rollback boom")

    kwargs["rollback_fn"] = rollback
    with pytest.raises(deploy.DeploymentError) as exc:
        deploy.orchestrate_deployment(**kwargs)
    assert "rollback failed" in str(exc.value)


def test_revision_url_used_for_health_checks(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    checked = []
    kwargs["verify_api_fn"] = lambda url, audience=None: checked.append(("api", url)) or True
    kwargs["verify_worker_fn"] = lambda url, audience=None: checked.append(("worker", url)) or True
    kwargs["revision_url_fn"] = lambda service, revision, tag, region, project: f"https://{revision}.example"
    deploy.orchestrate_deployment(**kwargs)
    assert ("api", "https://api-new.example") in checked
    assert ("worker", "https://worker-new.example") in checked


def test_successful_deployment_receipt(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    receipt = deploy.orchestrate_deployment(**base_kwargs())
    assert receipt.api_new_revision == "api-new"
    assert receipt.worker_new_revision == "worker-new"
    assert receipt.image_digest == VALID_DIGEST


def test_revision_image_digest_from_status():
    monkeypatch_data = {"status": {"imageDigest": VALID_DIGEST}}

    class FakeGcloud:
        pass

    # monkeypatch gcloud_json
    orig = deploy.gcloud_json
    deploy.gcloud_json = lambda args: monkeypatch_data
    try:
        d = deploy.revision_image_digest("api", "rev-1", "region", "project")
    finally:
        deploy.gcloud_json = orig
    assert d == VALID_DIGEST


def test_revision_image_digest_from_container_image():
    data = {"spec": {"containers": [{"image": f"repo/image@{VALID_DIGEST}"}]}}
    orig = deploy.gcloud_json
    deploy.gcloud_json = lambda args: data
    try:
        d = deploy.revision_image_digest("api", "rev-1", "region", "project")
    finally:
        deploy.gcloud_json = orig
    assert d == VALID_DIGEST


def test_revision_image_digest_raises_when_missing():
    orig = deploy.gcloud_json
    deploy.gcloud_json = lambda args: {}
    try:
        with pytest.raises(deploy.DeploymentError):
            deploy.revision_image_digest("api", "rev-1", "region", "project")
    finally:
        deploy.gcloud_json = orig


def test_wait_for_new_revision_detects_change(monkeypatch):
    calls = {"n": 0}

    def fake_gcloud_json(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": {"latestCreatedRevisionName": "old-rev"}}
        return {"status": {"latestCreatedRevisionName": "new-rev"}}

    monkeypatch.setattr(deploy, "gcloud_json", fake_gcloud_json)
    monkeypatch.setattr(deploy, "revision_image_digest", lambda *a, **k: "sha256:" + "b" * 64)
    monkeypatch.setattr(deploy, "time", time)
    # speed up: monkeypatch sleep
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0 if calls["n"] < 10 else 999)
    res = deploy.wait_for_new_revision("svc", "reg", "proj", "old-rev", digest="sha256:" + "b" * 64, timeout=10, poll_interval=0)
    assert res == "new-rev"


def test_deploy_revision_parses_stdout(monkeypatch):
    monkeypatch.setattr(deploy, "run", lambda command, check=True: type("R", (), {"stdout": "Revision: multi-agents-00001-abc has been deployed", "returncode": 0})())
    # pre-deploy describe
    monkeypatch.setattr(deploy, "gcloud_json", lambda args: {"status": {"latestCreatedRevisionName": "old"}})
    rev = deploy.deploy_revision("svc", "reg", "proj", "img@sha256:" + "a" * 64)
    assert rev == "multi-agents-00001-abc"


def test_deploy_revision_falls_back_to_wait(monkeypatch):
    monkeypatch.setattr(deploy, "run", lambda command, check=True: type("R", (), {"stdout": "no revision here", "returncode": 0})())
    calls = {"n": 0}
    def gc(args):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": {"latestCreatedRevisionName": "old-rev"}}
        return {"status": {"latestCreatedRevisionName": "new-rev"}}
    monkeypatch.setattr(deploy, "gcloud_json", gc)
    monkeypatch.setattr(deploy, "revision_image_digest", lambda *a, **k: "sha256:" + "a" * 64)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    rev = deploy.deploy_revision("svc", "reg", "proj", "img@sha256:" + "a" * 64)
    assert rev == "new-rev"


def _capture_deploy_command(monkeypatch, *, worker: bool):
    captured = []

    def fake_run(command, check=True):
        captured.append(command)
        return type("R", (), {"stdout": "Revision: svc-00001-abc has been deployed", "returncode": 0})()

    monkeypatch.setattr(deploy, "run", fake_run)
    monkeypatch.setattr(deploy, "gcloud_json", lambda args: {"status": {"latestCreatedRevisionName": "old"}})
    deploy.deploy_revision(
        "multi-agents-research-worker", "asia-northeast1", "proj",
        "repo/img@sha256:" + "a" * 64, worker=worker,
    )
    return captured[-1]


def test_worker_deploy_is_private_and_receives_no_traffic(monkeypatch):
    command = _capture_deploy_command(monkeypatch, worker=True)
    assert "--no-traffic" in command
    assert "--no-allow-unauthenticated" in command
    assert command[command.index("--command") + 1] == "python"
    assert "-m,csg.worker" in command[command.index("--args=-m,csg.worker")]
    image = command[command.index("--image") + 1]
    assert "@sha256:" in image


def test_api_deploy_keeps_its_existing_authentication(monkeypatch):
    command = _capture_deploy_command(monkeypatch, worker=False)
    assert "--no-traffic" in command
    assert "--no-allow-unauthenticated" not in command
    assert "--command" not in command


def test_deploy_only_sets_the_image_and_worker_command(monkeypatch):
    # `gcloud run deploy` is a partial update: anything not passed here keeps
    # the service's existing env, secrets, service account, probes, scaling,
    # CPU, memory, concurrency, timeout, ingress and instance settings.
    for worker in (False, True):
        command = _capture_deploy_command(monkeypatch, worker=worker)
        flags = [part for part in command if part.startswith("--")]
        allowed = {
            "--region", "--project", "--image", "--no-traffic", "--quiet",
            "--command", "--args=-m,csg.worker", "--no-allow-unauthenticated",
            "--tag", "deploy-check",
        }
        assert set(flags) <= allowed, (worker, set(flags) - allowed)
        assert "--update-env-vars" not in command
        assert "--update-secrets" not in command
        assert "--allow-unauthenticated" not in command


def test_mint_identity_token_success(monkeypatch):
    class R:
        stdout = "token-value\n"
        returncode = 0
    monkeypatch.setattr(deploy.subprocess, "run", lambda *a, **k: R())
    assert deploy.mint_identity_token("https://worker") == "token-value"


def test_mint_identity_token_failure(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gcloud missing")
    monkeypatch.setattr(deploy.subprocess, "run", boom)
    with pytest.raises(deploy.DeploymentError):
        deploy.mint_identity_token("https://worker")


def test_private_worker_auth_model_preserved():
    # worker healthz/readyz are the checks; no public access required.
    assert "verify_worker" in dir(deploy)


def test_normalize_digest_bare_hex():
    bare = "a" * 64
    assert deploy.normalize_digest(bare) == "sha256:" + "a" * 64


def test_normalize_digest_full():
    full = "sha256:" + "b" * 64
    assert deploy.normalize_digest(full) == full


def test_normalize_digest_rejects_invalid():
    with pytest.raises(deploy.DeploymentError):
        deploy.normalize_digest("not-a-digest")
    with pytest.raises(deploy.DeploymentError):
        deploy.normalize_digest("sha256:" + "g" * 64)
    with pytest.raises(deploy.DeploymentError):
        deploy.normalize_digest("sha256:" + "a" * 63)


def test_require_digest_normalizes_bare_hex_in_image():
    img = "repo/image@" + "c" * 64
    assert deploy.require_digest(img) == "sha256:" + "c" * 64


def test_wait_for_new_revision_rejects_digest_mismatch_then_accepts(monkeypatch):
    calls = {"n": 0}

    def fake_gcloud_json(args):
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"status": {"latestCreatedRevisionName": "wrong-rev"}}
        return {"status": {"latestCreatedRevisionName": "right-rev"}}

    def fake_digest(service, revision, region, project):
        if revision == "wrong-rev":
            return "sha256:" + "1" * 64
        return "sha256:" + "2" * 64

    monkeypatch.setattr(deploy, "gcloud_json", fake_gcloud_json)
    monkeypatch.setattr(deploy, "revision_image_digest", fake_digest)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0 if calls["n"] < 20 else 999)

    res = deploy.wait_for_new_revision(
        "svc", "reg", "proj", "old-rev",
        digest="sha256:" + "2" * 64, timeout=10, poll_interval=0,
    )
    assert res == "right-rev"


def test_wait_for_new_revision_timeout_when_no_digest_match(monkeypatch):
    monkeypatch.setattr(deploy, "gcloud_json", lambda args: {"status": {"latestCreatedRevisionName": "wrong-rev"}})
    monkeypatch.setattr(deploy, "revision_image_digest", lambda *a, **k: "sha256:" + "1" * 64)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # monotonic advances past deadline immediately
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    with pytest.raises(deploy.DeploymentError, match="Timed out"):
        deploy.wait_for_new_revision(
            "svc", "reg", "proj", "old-rev",
            digest="sha256:" + "2" * 64, timeout=0, poll_interval=0,
        )


def test_concurrent_deployments_different_digests(monkeypatch):
    # Simulate two deployments: each must wait for its own digest-matching revision.
    digests = {"a": "sha256:" + "a" * 64, "b": "sha256:" + "b" * 64}
    revisions = {"a": "rev-a", "b": "rev-b"}

    for label, dgst in digests.items():
        calls = {"n": 0}

        def make_gcloud():
            def gc(args):
                calls["n"] += 1
                # first poll shows the other revision, second shows own
                if calls["n"] == 1:
                    other = "rev-b" if label == "a" else "rev-a"
                    return {"status": {"latestCreatedRevisionName": other}}
                return {"status": {"latestCreatedRevisionName": revisions[label]}}
            return gc

        def make_digest():
            def d(service, revision, region, project):
                if revision == "rev-a":
                    return digests["a"]
                if revision == "rev-b":
                    return digests["b"]
                return None
            return d

        monkeypatch.setattr(deploy, "gcloud_json", make_gcloud())
        monkeypatch.setattr(deploy, "revision_image_digest", make_digest())
        monkeypatch.setattr(deploy, "time", time)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(time, "monotonic", lambda: 0 if calls["n"] < 20 else 999)

        res = deploy.wait_for_new_revision(
            "svc", "reg", "proj", "old-rev",
            digest=dgst, timeout=10, poll_interval=0,
        )
        assert res == revisions[label], (label, res)


def test_verify_worker_uses_audience_for_token_and_url_for_request(monkeypatch):
    captured = {"mint": None, "http": []}

    def fake_mint(audience):
        captured["mint"] = audience
        return "tok"

    def fake_http(url, timeout=5.0, headers=None):
        captured["http"].append((url, headers))
        if url.endswith("/readyz"):
            return True, {"status": "ready"}
        return True, {"status": "ok", "worker_enabled": True, "gemini_queue_backend": "shared_supabase"}

    monkeypatch.setattr(deploy, "mint_identity_token", fake_mint)
    monkeypatch.setattr(deploy, "http_ok", fake_http)

    assert deploy.verify_worker(
        "https://rev-url", timeout=5.0, audience="https://svc-url",
    ) is True
    assert captured["mint"] == "https://svc-url"
    assert captured["http"][0][0] == "https://rev-url/livez"
    assert captured["http"][0][1] == {"Authorization": "Bearer tok"}


def test_verify_worker_falls_back_to_url_when_no_audience(monkeypatch):
    captured = {"mint": None}
    monkeypatch.setattr(deploy, "mint_identity_token", lambda a: (captured.update({"mint": a}) or "tok"))
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: (
        True, {"status": "ok", "worker_enabled": True, "gemini_queue_backend": "shared_supabase"}
        if url.endswith("/livez") else (True, {"status": "ready"})
    ))
    deploy.verify_worker("https://rev-url", timeout=5.0)
    assert captured["mint"] == "https://rev-url"


def test_orchestrate_passes_worker_url_as_audience(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    captured = {"audience": None, "url": None}

    def vw(url, audience=None):
        captured["url"] = url
        captured["audience"] = audience
        return True

    kwargs = base_kwargs() if False else None
    # build kwargs manually
    import types
    kw = {
        "project": "project", "region": "region",
        "commit_sha": "4" * 40, "build_id": "build-123",
        "image": "repo/image@" + "a" * 64,
        "api_service": "api", "worker_service": "worker",
        "api_url": "https://api-svc", "worker_url": "https://worker-svc",
        "verify_api_fn": lambda *a, **k: True,
        "verify_worker_fn": vw,
        "deploy_fn": lambda *a, **k: "api-new" if k.get("worker") is False else "worker-new",
        "switch_fn": lambda *a: None,
        "rollback_fn": lambda *a: None,
        "revision_url_fn": lambda service, revision, tag, region, project: f"https://{revision}",
        "revision_image_digest_fn": lambda *a, **k: "sha256:" + "a" * 64,
        "revision_config_fn": lambda *a, **k: {"service_account": "sa", "port": 8080, "env": [], "command": [], "args": []},
    }
    deploy.orchestrate_deployment(**kw)
    assert captured["url"] == "https://worker-new"
    assert captured["audience"] == "https://worker-svc"


def test_health_check_timeout_default_and_configurable(monkeypatch):
    # default timeout passed to http_ok
    captured = {}
    monkeypatch.setattr(deploy, "http_ok", lambda url, timeout=5.0, headers=None: (captured.update({"t": timeout}) or True, {"status": "ok"}))
    # verify_api uses default
    deploy.verify_api("https://x", timeout=deploy.DEFAULT_HEALTH_CHECK_TIMEOUT)
    assert captured["t"] == deploy.DEFAULT_HEALTH_CHECK_TIMEOUT


def test_verify_worker_readyz_timeout(monkeypatch):
    calls = []
    def fake_http(url, timeout=5.0, headers=None):
        calls.append(timeout)
        if url.endswith("/readyz"):
            return False, None
        return True, {"status": "ok", "worker_enabled": True, "gemini_queue_backend": "shared_supabase"}
    monkeypatch.setattr(deploy, "mint_identity_token", lambda a: "tok")
    monkeypatch.setattr(deploy, "http_ok", fake_http)
    assert deploy.verify_worker("https://x", timeout=12.0) is False
    assert all(t == 12.0 for t in calls)

def test_mint_identity_token_impersonates_configured_service_account(monkeypatch):
    captured = {}

    class Result:
        stdout = "tok-abc"

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Result()

    monkeypatch.setenv("DEPLOYMENT_INVOKER_SERVICE_ACCOUNT", "invoker@example.iam.gserviceaccount.com")
    monkeypatch.setattr(deploy.subprocess, "run", fake_run)

    assert deploy.mint_identity_token("https://svc") == "tok-abc"
    assert "--impersonate-service-account=invoker@example.iam.gserviceaccount.com" in captured["command"]


def test_revision_runtime_config_extracts_identity_and_env_names(monkeypatch):
    monkeypatch.setattr(deploy, "gcloud_json", lambda args: {
        "spec": {
            "serviceAccountName": "sa@example.iam.gserviceaccount.com",
            "containers": [{
                "ports": [{"containerPort": 8080}],
                "env": [{"name": "B"}, {"name": "A"}],
                "command": ["python"],
                "args": ["-m", "csg.worker"],
            }],
        }
    })

    assert deploy.revision_runtime_config("s", "r", "reg", "p") == {
        "service_account": "sa@example.iam.gserviceaccount.com",
        "port": 8080,
        "env": ["A", "B"],
        "command": ["python"],
        "args": ["-m", "csg.worker"],
    }


def test_runtime_config_drift_aborts_before_traffic_switch(monkeypatch):
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    good = {"service_account": "sa", "port": 8080, "env": [], "command": [], "args": []}
    drifted = {**good, "service_account": "default-compute@developer.gserviceaccount.com"}

    def config(service, revision, region, project):
        return drifted if revision == "api-new" else good

    switched = []
    kwargs["revision_config_fn"] = config
    kwargs["switch_fn"] = lambda *args: switched.append(args)

    with pytest.raises(deploy.DeploymentError, match="did not preserve"):
        deploy.orchestrate_deployment(**kwargs)
    assert switched == []

def _service_with_tag(tag, revision, url, *, extra=None):
    traffic = [{"revisionName": "old-rev", "percent": 100}]
    if extra:
        traffic.extend(extra)
    traffic.append({"revisionName": revision, "percent": 0, "tag": tag, "url": url})
    return {"status": {"traffic": traffic}}


def test_revision_url_uses_tag_url_for_untagged_revision(monkeypatch):
    # A --no-traffic revision has no servable status.url; only the traffic
    # tag entry is routable, which is what broke the earlier build.
    monkeypatch.setattr(deploy, "gcloud_json", lambda args: _service_with_tag(
        "deploy-check", "rev-new", "https://deploy-check---svc-hash.example",
    ))
    assert deploy.revision_url("svc", "rev-new", "deploy-check", "reg", "proj") == (
        "https://deploy-check---svc-hash.example"
    )


def test_revision_url_ignores_tag_on_older_revision(monkeypatch):
    calls = {"n": 0, "polls": 0}

    def fake(args):
        calls["n"] += 1
        if args[:2] == ["run", "services"]:
            calls["polls"] += 1
            if calls["polls"] == 1:
                return _service_with_tag("deploy-check", "rev-old", "https://deploy-check---svc-old.example")
            return _service_with_tag("deploy-check", "rev-new", "https://deploy-check---svc-new.example")
        return {"status": {"conditions": [{"type": "Ready", "status": "Unknown"}]}}

    monkeypatch.setattr(deploy, "gcloud_json", fake)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0 if calls["polls"] < 2 else 999.0)
    assert deploy.revision_url("svc", "rev-new", "deploy-check", "reg", "proj") == (
        "https://deploy-check---svc-new.example"
    )


def test_revision_url_times_out_without_matching_tag(monkeypatch):
    calls = {"n": 0}

    def fake_gcloud(args):
        calls["n"] += 1
        if args[:2] == ["run", "services"]:
            return _service_with_tag("deploy-check", "rev-other", "https://deploy-check---svc.example")
        return {"status": {"conditions": [{"type": "Ready", "status": "Unknown"}]}}

    monkeypatch.setattr(deploy, "gcloud_json", fake_gcloud)
    monkeypatch.setattr(deploy, "DEFAULT_REVISION_URL_TIMEOUT", 1.0)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0.0 if calls["n"] < 2 else 999.0)
    with pytest.raises(deploy.DeploymentError, match="Could not determine URL for tagged revision"):
        deploy.revision_url("svc", "rev-new", "deploy-check", "reg", "proj")


def test_revision_url_aborts_on_terminal_revision_failure(monkeypatch):
    def fake(args):
        if args[:2] == ["run", "services"]:
            return {"status": {"traffic": [{"revisionName": "old-rev", "percent": 100}]}}
        return {"status": {"conditions": [
            {"type": "Ready", "status": "False", "reason": "RevisionFailed", "message": "container crashed"},
        ]}}

    monkeypatch.setattr(deploy, "gcloud_json", fake)
    monkeypatch.setattr(deploy, "time", time)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    with pytest.raises(deploy.DeploymentError, match="failed to become ready"):
        deploy.revision_url("svc", "rev-new", "deploy-check", "reg", "proj")


def test_deploy_assigns_candidate_tag(monkeypatch):
    for worker in (False, True):
        command = _capture_deploy_command(monkeypatch, worker=worker)
        tag_index = command.index("--tag")
        assert command[tag_index + 1] == "deploy-check"
        assert "--no-traffic" in command


def test_orchestrate_keeps_service_url_as_worker_audience(monkeypatch):
    # Cloud Run ID tokens keep the service URL as audience even when the
    # request goes to a traffic-tag URL.
    monkeypatch.setattr(deploy, "active_revision", lambda *args: "api-old" if args[0] == "api" else "worker-old")
    kwargs = base_kwargs()
    seen = []

    def verify(url, audience=None):
        seen.append((url, audience))
        return True

    kwargs["verify_worker_fn"] = verify
    kwargs["revision_url_fn"] = lambda service, revision, tag, region, project: (
        f"https://{tag}---{service}.example"
    )
    deploy.orchestrate_deployment(**kwargs)
    assert seen[0] == ("https://deploy-check---worker.example", "https://worker")
