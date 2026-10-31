"""Where the factory's parts live, relative to this repository."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS_DIR = ROOT / "runbooks"
TEMPLATES_DIR = ROOT / "templates"
APPS_DIR = ROOT / "apps"
LLM_CONFIG = ROOT / "llm.yaml"
ROLES_FILE = ROOT / "roles.yaml"
