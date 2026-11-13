"""Source-CLI contracts: a skill skipped during integration is never cleaned up as stale."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.component, pytest.mark.integration, pytest.mark.lifecycle_smoke]

_SKILL_DIR = ".claude/skills/demo"
_REMOTE_URL = "https://github.com/apm-fixture-org/skipped-skill"


def _write_skill(package: Path, body: str) -> None:
    directory = package / ".apm/skills/demo"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: Skip fixture\n---\n{body}\n", encoding="ascii"
    )
    (directory / "asset.md").write_text("Asset.\n", encoding="ascii")


def _drop_directory_claim(lock_path: Path) -> None:
    """Keep the skill's member claims but drop the claim on its directory.

    The skill integrator decides "managed" on the directory entry while stale
    cleanup works on member entries, so this lock makes the skill look
    user-authored to integration and APM-owned to cleanup.
    """
    lock = load_yaml(lock_path)
    assert lock is not None
    (dep,) = lock["dependencies"]
    assert _SKILL_DIR in dep["deployed_files"]
    dep["deployed_files"] = [path for path in dep["deployed_files"] if path != _SKILL_DIR]
    lock["deployments"] = [row for row in lock["deployments"] if row["value"] != _SKILL_DIR]
    dump_yaml(lock, lock_path)


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("command", ["install", "update"])
def test_skipped_skill_survives_stale_cleanup_and_is_reported(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    user_scope: bool,
    command: str,
) -> None:
    """A skipped skill keeps its files and lock claims, and the skip is printed.

    ``install`` re-integrates a changed local-path package; ``update`` moves a
    branch-tracking Git dependency to a new commit that changes the skill.
    """
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    state_root = isolated.config_root if user_scope else project
    deploy_root = isolated.home if user_scope else project
    env = isolated.subprocess_env()

    package = isolated.work_root / "pkg"
    _write_skill(package, "Version one.")
    dump_yaml({"name": "pkg", "version": "1.0.0"}, package / "apm.yml")
    if command == "update":
        repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
        repository = repositories.create("skipped-skill", source_tree=package)
        repositories.commit(repository, message="version one")
        env = repositories.url_rewrite_subprocess_env(repository, _REMOTE_URL)
        dependency: dict[str, str] = {"git": _REMOTE_URL, "ref": "main"}
    else:
        dependency = {"path": str(package)}
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["claude"],
            "dependencies": {"apm": [dependency]},
        },
        state_root / "apm.yml",
    )
    runner = ApmLifecycleRunner(apm_engine_command)
    scope_args = ("--global",) if user_scope else ()
    initial = runner.run(("install", *scope_args, "--no-policy"), cwd=project, env=env)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    deployed = deploy_root / _SKILL_DIR
    assert (deployed / "SKILL.md").is_file()

    lock_path = state_root / "apm.lock.yaml"
    _drop_directory_claim(lock_path)
    if command == "update":
        _write_skill(repository.worktree, "Version two.")
        repositories.commit(repository, message="version two")
        args: tuple[str, ...] = ("update", *scope_args, "--yes")
    else:
        _write_skill(package, "Version two.")
        args = ("install", *scope_args, "--no-policy")

    result = runner.run(args, cwd=project, env=env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 0, output
    if command == "update":
        assert "Updated 1 APM dependency" in output, output

    assert (deployed / "SKILL.md").read_text(encoding="ascii").endswith("Version one.\n"), output
    assert (deployed / "asset.md").is_file(), output
    lock = load_yaml(lock_path)
    assert lock is not None
    (dep,) = lock["dependencies"]
    assert f"{_SKILL_DIR}/SKILL.md" in dep["deployed_files"], output
    assert f"{_SKILL_DIR}/asset.md" in dep["deployed_files"], output
    assert "1 file skipped -- local files exist, not managed by APM" in output, output
