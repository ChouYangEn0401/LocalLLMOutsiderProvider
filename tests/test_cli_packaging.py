"""Static checks of the operations files: Windows .bat scripts, Docker, CI workflows."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llmop.config import parse_config

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "scripts" / "windows"
DOCKER = ROOT / "docker"
BATS = ["_env.bat", "Setup.bat", "Start.bat", "Start-With-Tunnel.bat", "Add-User.bat", "Check.bat"]
COMPOSE = ["compose.ollama.yml", "compose.vllm.yml", "compose.host-ollama.yml"]


# ------------------------------------------------------------------ Windows scripts
@pytest.mark.parametrize("name", BATS)
def test_bat_files_are_ascii_with_crlf(name):
    data = (WIN / name).read_bytes()
    data.decode("ascii")  # cmd.exe reads .bat files in the console code page
    assert b"\n" not in data.replace(b"\r\n", b""), f"{name} must use CRLF line endings"
    text = data.decode("ascii")
    if name == "_env.bat":
        assert "chcp 65001" in text and "PYTHONUTF8=1" in text
        assert r"%~dp0..\.." in text  # repo root is two levels up
    else:
        assert 'call "%~dp0_env.bat"' in text
        assert "-m llmop" in text


def test_scripts_call_the_right_commands():
    assert "serve --tunnel" in (WIN / "Start-With-Tunnel.bat").read_text(encoding="ascii")
    assert "winget install --id Cloudflare.cloudflared" in (WIN / "Start-With-Tunnel.bat").read_text(encoding="ascii")
    setup = (WIN / "Setup.bat").read_text(encoding="ascii")
    for needle in ("py -%%V", "3.13 3.12 3.11 3.10", "-m venv", "pip install", "-e", r"dist\*.whl",
                   "-m llmop init"):
        assert needle in setup
    assert "add-user" in (WIN / "Add-User.bat").read_text(encoding="ascii")
    assert "llmop check" in (WIN / "Check.bat").read_text(encoding="ascii")


def test_gitattributes_keeps_bat_files_crlf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in text
    assert "Dockerfile text eol=lf" in text and "*.py text eol=lf" in text


# ------------------------------------------------------------------ Docker
@pytest.mark.parametrize("name", COMPOSE)
def test_compose_files_publish_only_on_localhost(name):
    data = yaml.safe_load((DOCKER / name).read_text(encoding="utf-8"))
    services = data["services"]
    llmop = services["llmop"]
    assert llmop["build"] == {"context": "..", "dockerfile": "docker/Dockerfile"}
    assert llmop["ports"] == ["127.0.0.1:8780:8780"]
    assert "./config:/config:ro" in llmop["volumes"]
    assert any(v.startswith("llmop-data:") for v in llmop["volumes"])
    for other, svc in services.items():
        if other != "llmop":
            assert "ports" not in svc, f"{other} must not be reachable from outside"


def test_compose_backends():
    ollama = yaml.safe_load((DOCKER / "compose.ollama.yml").read_text(encoding="utf-8"))["services"]
    assert ollama["ollama"]["image"].startswith("ollama/ollama")
    vllm = yaml.safe_load((DOCKER / "compose.vllm.yml").read_text(encoding="utf-8"))["services"]
    assert vllm["vllm"]["image"].startswith("vllm/vllm-openai")
    assert "--model" in vllm["vllm"]["command"]
    assert vllm["vllm"]["deploy"]["resources"]["reservations"]["devices"][0]["driver"] == "nvidia"
    host = yaml.safe_load((DOCKER / "compose.host-ollama.yml").read_text(encoding="utf-8"))["services"]
    assert set(host) == {"llmop"}
    assert "host.docker.internal:host-gateway" in host["llmop"]["extra_hosts"]
    tunnel = yaml.safe_load((DOCKER / "compose.tunnel.yml").read_text(encoding="utf-8"))["services"]
    assert tunnel["cloudflared"]["command"] == ["tunnel", "--no-autoupdate", "--url", "http://llmop:8780"]


def test_docker_config_example_is_valid():
    cfg = parse_config(yaml.safe_load((DOCKER / "config.docker.example.yaml").read_text(encoding="utf-8")))
    assert cfg.server.data_dir == "/data"
    assert cfg.server.host == "0.0.0.0"
    assert cfg.backends[0].base_url == "http://ollama:11434"
    assert "172.16.0.0/12" in cfg.server.trusted_proxies
    assert cfg.tunnel.enabled is False
    assert cfg.accounts["admin"].password_hash.startswith("REPLACE_ME")


def test_dockerfile_essentials():
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    for needle in ("FROM python:3.12-slim AS build", "USER 10001", "LLMOP_IN_CONTAINER=1",
                   "EXPOSE 8780", 'VOLUME ["/data"]', "HEALTHCHECK", "/health",
                   'ENTRYPOINT ["llmop", "--config", "/config/config.yaml"]',
                   'CMD ["serve", "--host", "0.0.0.0"'):
        assert needle in text, needle
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ignore[ignore.index("*")] == "*" and "!src" in ignore and "!pyproject.toml" in ignore


# ------------------------------------------------------------------ GitHub workflows
def _on(data: dict) -> dict:
    return data.get("on", data.get(True))  # YAML 1.1 reads a bare "on" as True


def test_ci_workflow_matrix():
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    matrix = data["jobs"]["test"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert set(matrix["python"]) == {"3.10", "3.12"}
    assert "pull_request" in _on(data)


def test_release_workflow():
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    assert _on(data)["push"]["tags"] == ["v*"]
    assert data["permissions"] == {"contents": "write"}
    steps = data["jobs"]["release"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "python -m build" in runs and "pytest" in runs
    gh = next(s for s in steps if str(s.get("uses", "")).startswith("softprops/action-gh-release@v2"))
    assert gh["with"]["files"] == "dist/*"
