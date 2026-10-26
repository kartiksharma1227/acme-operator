"""Runtime configuration: environment (.env) plus the company knowledge folder.

The runtime knows nothing about any particular company or task. Everything specific
comes from the folder at COMPANY_DIR: a system directory, policies and SOPs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None or value == "" else value.lower() not in {"0", "false", "no"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass
class Config:
    root: Path
    company_dir: Path
    systems: dict
    policies: dict
    runs_dir: Path
    headless: bool = True
    max_steps: int = 150
    max_cost_usd: float = 3.0
    agent_model: str = ""
    verifier_model: str = ""

    @property
    def sops_dir(self) -> Path:
        return self.company_dir / "sops"

    def web_systems(self) -> dict:
        return {k: v for k, v in self.systems.items() if v.get("kind") == "web"}

    def folder_systems(self) -> dict:
        return {k: v for k, v in self.systems.items() if v.get("kind") == "folder"}

    def default_web_system(self) -> str | None:
        web = list(self.web_systems())
        return web[0] if len(web) == 1 else None

    def base_url(self, system: str) -> str:
        # Read at call time so tests and evals can point a system at another server.
        env_name = self.systems[system]["base_url_env"]
        url = os.environ.get(env_name, "")
        if not url:
            raise RuntimeError(f"System '{system}' has no URL: set {env_name} in .env")
        return url.rstrip("/")


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    company_dir = Path(os.environ.get("COMPANY_DIR") or ROOT / "company").resolve()
    systems = yaml.safe_load((company_dir / "systems.yaml").read_text())["systems"]
    policies = yaml.safe_load((company_dir / "policies.yaml").read_text())
    return Config(
        root=ROOT,
        company_dir=company_dir,
        systems=systems,
        policies=policies,
        runs_dir=Path(os.environ.get("RUNS_DIR") or ROOT / "runs").resolve(),
        headless=_env_bool("HEADLESS", True),
        max_steps=int(os.environ.get("MAX_STEPS") or 150),
        max_cost_usd=_env_float("MAX_COST_USD", 3.0),
        agent_model=os.environ.get("AGENT_MODEL", ""),
        verifier_model=os.environ.get("VERIFIER_MODEL") or os.environ.get("AGENT_MODEL", ""),
    )
