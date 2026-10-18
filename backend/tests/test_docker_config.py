"""Daemon-free regression checks; these do NOT replace a real Docker smoke test."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def compose_configs(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=10)
    if version.returncode:
        pytest.skip("Docker Compose plugin is not installed")
    # Never resolve or read the developer's private .env in a test.
    env_file = tmp_path_factory.mktemp("compose") / "synthetic.env"
    env_file.write_text("APP_API_KEY=synthetic-compose-test-only\nAPP_ANALYZER_MODE=fake\n")
    env = {**os.environ, "FORTE_ENV_FILE": str(env_file)}
    configs = []
    for overlay in ([], ["-f", "compose.local-fake.yaml"]):
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", "compose.yaml",
             *overlay, "config", "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=15, check=True,
        )
        configs.append(json.loads(result.stdout))
    return configs


def test_fake_requires_explicit_local_overlay(compose_configs):
    base, fake = compose_configs
    assert base["services"]["backend"]["environment"]["APP_ANALYZER_MODE"] == "disabled"
    assert fake["services"]["backend"]["environment"]["APP_ANALYZER_MODE"] == "fake"
    for config in compose_configs:
        assert config["services"]["backend"]["environment"]["APP_ENVIRONMENT"] == "development"
        assert set(config["services"]) == {"backend", "frontend"}


def test_only_loopback_nginx_is_published(compose_configs):
    for config in compose_configs:
        backend, frontend = (config["services"][name] for name in ("backend", "frontend"))
        assert not backend.get("ports")
        assert set(backend["networks"]) == {"backend"}
        assert config["networks"]["backend"]["internal"] is True
        assert len(frontend["ports"]) == 1
        port = frontend["ports"][0]
        assert (port["host_ip"], str(port["published"]), port["target"]) == ("127.0.0.1", "8080", 8080)


def test_runtime_secret_and_persistent_storage(compose_configs):
    for config in compose_configs:
        backend = config["services"]["backend"]
        env = backend["environment"]
        assert env["APP_API_KEY"] == "synthetic-compose-test-only"
        assert env["APP_DATABASE_PATH"] == "/var/lib/forteguard/jobs.sqlite3"
        assert env["APP_STORAGE_DIR"] == "/var/lib/forteguard/uploads"
        assert int(env["APP_MAX_UPLOAD_BYTES"]) == 128 * 1024 * 1024
        assert json.loads(env["APP_UI_ALLOWED_ORIGINS"]) == ["http://localhost:8080", "http://127.0.0.1:8080"]
        assert backend["volumes"][0]["type"] == "volume"
        assert backend["volumes"][0]["target"] == "/var/lib/forteguard"
        assert backend["volumes"][0]["source"] == "backend-data"
        assert not config["services"]["frontend"].get("environment")
        for service in config["services"].values():
            assert not service["build"].get("args")
            assert not service["build"].get("secrets")
            assert service["read_only"] is True
            assert service["cap_drop"] == ["ALL"]
            assert "no-new-privileges:true" in service["security_opt"]


def test_image_defaults_and_build_context_policy():
    backend = (ROOT / "backend/Dockerfile").read_text()
    frontend = (ROOT / "frontend/Dockerfile").read_text()
    assert "FROM python:3.11-slim-bookworm" in backend
    assert "APP_ENVIRONMENT=production" in backend
    assert "APP_ANALYZER_MODE=disabled" in backend
    assert '"--workers", "1"' in backend
    assert "USER 10001:10001" in backend
    assert "npm ci" in frontend and "npm run test:production" in frontend
    assert "FROM nginx:stable-alpine AS runtime" in frontend and "USER nginx" in frontend
    for dockerfile in (backend, frontend):
        assert "COPY . " not in dockerfile and "COPY .env" not in dockerfile
        assert "APP_API_KEY" not in dockerfile
    ignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert next(line for line in ignore if line and not line.startswith("#")) == "**"
    for pattern in ("**/.env", "**/*.mov", "**/*.sqlite*", "**/*.pt", "**/*.safetensors"):
        assert pattern in ignore
    assert not any(line.startswith(("!ml", "!data", "!weights")) for line in ignore)
    nginx = (ROOT / "frontend/nginx.conf").read_text()
    # All compiled-in HTTP modules can register temp paths even when unused.
    assert "pid /tmp/nginx.pid;" in nginx
    for directive in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        assert f"{directive}_temp_path /tmp/" in nginx
    assert "client_max_body_size 129m;" in nginx
    assert "proxy_request_buffering off;" in nginx
    assert "try_files $uri $uri/ /index.html;" in nginx


def test_optional_swagger_overlay_preserves_isolation(tmp_path):
    if not (ROOT / "compose.swagger.yaml").exists() or shutil.which("docker") is None:
        pytest.skip("Optional Swagger overlay or Docker CLI unavailable")
    env_file = tmp_path / "synthetic.env"
    env_file.write_text("APP_API_KEY=synthetic-compose-test-only\n")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", "compose.yaml",
         "-f", "compose.local-fake.yaml", "-f", "compose.swagger.yaml", "config", "--format", "json"],
        cwd=ROOT, env={**os.environ, "FORTE_ENV_FILE": str(env_file)},
        capture_output=True, text=True, timeout=15, check=True,
    )
    config = json.loads(result.stdout)
    backend = config["services"]["backend"]
    assert config["networks"]["backend"]["internal"] is True
    assert set(backend["networks"]) == {"backend"}
    assert len(backend["ports"]) == 1
    port = backend["ports"][0]
    assert (port["host_ip"], str(port["published"]), port["target"]) == ("127.0.0.1", "8000", 8000)
    assert backend["read_only"] and backend["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in backend["security_opt"]
