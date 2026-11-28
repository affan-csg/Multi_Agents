from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CB = ROOT / "cloudbuild.yaml"
DOCKERFILE = ROOT / "Dockerfile"


def load_cb():
    return yaml.safe_load(CB.read_text(encoding="utf-8"))


def test_cloudbuild_parses():
    data = load_cb()
    assert isinstance(data, dict)
    assert "steps" in data
    assert len(data["steps"]) == 5
    assert [step["id"] for step in data["steps"]][-1] == "Deploy API and Research Worker"


def test_no_trigger_id_substitution():
    text = CB.read_text(encoding="utf-8")
    assert "TRIGGER_ID" not in text
    assert "trigger-id" not in text
    assert "cloudbuild.trigger" not in text


def test_bash_steps_have_env_substitutions():
    data = load_cb()
    bash_steps = [s for s in data["steps"] if s.get("entrypoint") == "bash"]
    assert len(bash_steps) >= 2
    for step in bash_steps:
        env = step.get("env") or []
        env_text = " ".join(env)
        assert "COMMIT_SHA=$COMMIT_SHA" in env_text, f"missing COMMIT_SHA env in {step.get('id')}"
        assert "BUILD_ID=$BUILD_ID" in env_text, f"missing BUILD_ID env in {step.get('id')}"


def test_bash_scripts_use_escaped_shell_vars():
    data = load_cb()
    for step in data["steps"]:
        if step.get("entrypoint") != "bash":
            continue
        script = "\n".join(step.get("args") or [])
        # COMMIT_SHA/BUILD_ID references inside the shell script must be
        # escaped ($$) so Cloud Build leaves them for bash, not substitutes.
        # Any unescaped $COMMIT_SHA/$BUILD_ID inside the script body is a bug.
        # We check the script portion (after the -c).
        # Strip env lines (they legitimately use single $).
        for bad in ("$COMMIT_SHA", "$BUILD_ID"):
            # allowed only as $$COMMIT_SHA / $$BUILD_ID
            import re
            # find occurrences not preceded by $
            for m in re.finditer(re.escape(bad), script):
                start = m.start()
                if start > 0 and script[start - 1] == "$":
                    continue
                # also allow inside env? env is separate; script here is args
                raise AssertionError(f"unescaped {bad} in bash step {step.get('id')}")


def test_non_bash_steps_use_single_substitution():
    data = load_cb()
    # Push/validate steps use $COMMIT_SHA (Cloud Build substitution), not $$
    push_step = next(s for s in data["steps"] if s.get("id", "").lower().startswith("push"))
    args_text = " ".join(push_step.get("args") or [])
    assert "$COMMIT_SHA" in args_text
    assert "$$COMMIT_SHA" not in args_text


def test_images_section_tagged_by_commit():
    data = load_cb()
    images = data.get("images") or []
    assert any("$COMMIT_SHA" in img for img in images)


def test_dockerfile_has_no_trigger_id():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "TRIGGER_ID" not in text
    assert "trigger" not in text.lower()


def test_dockerfile_args_match_build_args():
    text = DOCKERFILE.read_text(encoding="utf-8")
    for arg in ("SOURCE_COMMIT", "BUILD_ID", "BUILD_TIMESTAMP"):
        assert f"ARG {arg}" in text


def test_deploy_region_declared():
    data = load_cb()
    assert "_DEPLOY_REGION" in (data.get("substitutions") or {})


def test_strict_shell_handling_preserved():
    text = CB.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


def test_validate_step_invokes_provenance_script():
    data = load_cb()
    step = next(s for s in data["steps"] if s.get("id") == "Validate shared provenance")
    script = "\n".join(step.get("args") or [])
    assert "scripts/validate_image_provenance.py" in script
    assert "--skip-build-metadata" in script


def test_validate_step_ships_its_own_python():
    data = load_cb()
    step = next(s for s in data["steps"] if s.get("id") == "Validate shared provenance")
    # cloud-sdk:slim has no python3 on PATH; the full image bundles one.
    assert ":slim" not in step["name"]
    assert "bundledpythonunix" in "\n".join(step.get("args") or [])


def deploy_script() -> str:
    data = load_cb()
    step = next(s for s in data["steps"] if s.get("id") == "Deploy API and Research Worker")
    return "\n".join(step.get("args") or [])


def test_deploy_step_runs_the_orchestrator_for_both_services():
    script = deploy_script()
    assert "scripts/deployment_orchestrator.py" in script
    assert "--api-service multi-agents" in script
    assert "--worker-service multi-agents-research-worker" in script


def test_deploy_step_pins_the_image_by_digest():
    script = deploy_script()
    # The deployable reference must be <repo>@sha256:<64 hex>, never a tag.
    assert "image_summary.digest" in script
    assert 'IMAGE="$$IMAGE_BASE@$$DIGEST"' in script
    assert "^sha256:[0-9a-f]{64}" in script
    assert ":latest" not in script


def test_deploy_step_rejects_a_missing_digest_before_deploying():
    script = deploy_script()
    guard = script.index("Could not resolve an immutable digest")
    deploy_call = script.index('"$$PYTHON" scripts/deployment_orchestrator.py')
    assert guard < deploy_call
    assert "exit 1" in script[guard:deploy_call]


def test_deploy_step_passes_service_urls_for_audience():
    script = deploy_script()
    assert "run services describe multi-agents " in script
    assert "run services describe multi-agents-research-worker " in script
    assert "--worker-url" in script
    assert "--api-url" in script


def test_only_the_deploy_step_issues_cloud_run_changes():
    data = load_cb()
    for step in data["steps"]:
        script = "\n".join(step.get("args") or [])
        if step.get("id") == "Deploy API and Research Worker":
            continue
        assert "run deploy" not in script
        assert "update-traffic" not in script