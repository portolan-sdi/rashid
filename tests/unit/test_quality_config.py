"""Pin the owner boundaries and prevent gate settings from weakening silently."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
JSCPD_BASELINE_FINGERPRINTS = 32
JSCPD_BASELINE_CLONES = 32


def test_tid251_bans_the_owner_apis() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    assert "TID251" in lint["select"] + lint["extend-select"]
    assert lint["flake8-tidy-imports"]["banned-api"] == {
        "pytest.xfail": {
            "msg": "use @pytest.mark.xfail(strict=True) keyed to an open issue and listed in the ledger"
        },
    }
    assert "TID251" not in lint["per-file-ignores"]["tests/**"]
    source = tomllib.loads((ROOT / "src/ruff.toml").read_text(encoding="utf-8"))
    assert source["extend"] == "../pyproject.toml"
    banned = source["lint"]["flake8-tidy-imports"]["extend-banned-api"]
    assert set(banned) == {
        "urllib.parse.urljoin",
        "urllib.parse.urlsplit",
        "urllib.parse.urlparse",
        "urllib.request.Request",
        "urllib.request.urlopen",
        "json.loads",
        "json.load",
        "orjson.loads",
        "obstore.store.HTTPStore",
        "glob.glob",
        "pyarrow.parquet.ParquetFile",
        "rasterio.open",
    }
    assert all(entry["msg"].strip() for entry in banned.values())


def test_owner_layer_contracts_cover_new_modules() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {row.get("id"): row for row in config["tool"]["importlinter"]["contracts"]}
    rules = contracts["rules-no-network"]
    assert rules["type"] == "forbidden"
    assert not rules.get("ignore_imports")
    assert rules["source_modules"] == ["rashid.rules"]
    assert set(rules["forbidden_modules"]) == {"rashid.live", "rashid.remote", "rashid._store"}
    assert rules.get("as_packages", True) is True
    assert rules.get("allow_indirect_imports", False) is False
    data = contracts["rules-no-data"]
    assert data["type"] == "forbidden"
    assert data["source_modules"] == ["rashid.rules"]
    assert data["forbidden_modules"] == ["rashid.data"]
    assert not data.get("ignore_imports")
    assert data.get("as_packages", True) is True
    assert data.get("allow_indirect_imports", False) is False
    files = contracts["data-files-owner"]
    assert files["type"] == "protected"
    assert files["protected_modules"] == ["rashid.data._files"]
    assert files["as_packages"] is False


def test_jscpd_baseline_only_shrinks() -> None:
    """Delete only: lower the pin after deduplication, never raise it."""
    config = json.loads((ROOT / ".jscpd.json").read_text(encoding="utf-8"))
    assert config["minTokens"] == 50
    assert config["failOnEmpty"] is True
    assert config["format"] == ["python"]
    assert set(config["ignore"]) == {"**/tests/fixtures/**", "**/.venv/**"}
    baseline = json.loads((ROOT / ".jscpd-baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["fingerprints"]) == JSCPD_BASELINE_FINGERPRINTS
    assert sum(baseline["fingerprints"].values()) == JSCPD_BASELINE_CLONES


def test_jscpd_hook_runs_in_the_ci_commit_stage() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (hook,) = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "jscpd"]
    assert hook["entry"] == (
        "uvx jscpd@5.2.1 src/rashid tests --baseline .jscpd-baseline.json --fail-on-new-clones 0"
    )
    assert hook["pass_filenames"] is False
    assert hook["language"] == "system"
    assert hook["stages"] == ["pre-commit"]
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    # This pinned reusable workflow runs prek against both hook stages.
    assert ci["jobs"]["ci"]["uses"] == (
        "portolan-sdi/portolan-ops/.github/workflows/reusable-python-ci.yml@v1.14.0"
    )
    assert "pytest -n 8" in ci["jobs"]["ci"]["with"]["test-command"]
