"""deploy/update-hub.sh: which old images the last step removes (#68). Docker and curl are fakes
on PATH; the script itself runs unchanged."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "update-hub.sh"

CI = "ghcr.io/example-owner/sloppy-topcoat-hub"
LOCAL = "sloppy-topcoat-hub"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  images) cat "$FAKE_IMAGES" ;;
  compose) case "$*" in *pg_dump*) echo dump ;; esac ;;
esac
exit 0
"""


def update(tmp_path: Path, prev: str, new: str, images: list[str]) -> tuple[str, list[str]]:
    """Runs update-hub.sh from prev to new with `docker images` listing images; returns the
    output and the images it removed."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "update-hub.sh").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (deploy / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (deploy / "hub.env").write_text(f"TOPCOAT_IMAGE={prev}\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", "#!/usr/bin/env bash\nexit 0\n")):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "images.txt").write_text("".join(f"{i}\n" for i in images), encoding="utf-8")
    log = tmp_path / "docker.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOPCOAT_")} | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_IMAGES": str(tmp_path / "images.txt"),
        "TOPCOAT_BACKUP_DIR": str(tmp_path / "backup"),
    }
    r = subprocess.run(
        ["bash", str(deploy / "update-hub.sh"), "--db-revision", "abc123", new],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    return r.stdout, [c.split(" ", 1)[1] for c in calls if c.startswith("rmi ")]


def test_a_ci_image_prunes_old_images_of_its_registry_repository_and_of_local_builds(
    tmp_path: Path,
) -> None:
    prev, new = f"{CI}:sha-1111111", f"{CI}:sha-2222222"
    images = [
        new,
        prev,
        f"{CI}:sha-0000000",
        f"{CI}:sha-0000001",
        f"{LOCAL}:local-aaaaaaa",
        f"{CI}-other:sha-0000000",  # another repository with the same prefix stays
        "postgres:16",
        f"{CI}:<none>",
    ]
    out, removed = update(tmp_path, prev, new, images)
    assert sorted(removed) == sorted(
        [f"{CI}:sha-0000000", f"{CI}:sha-0000001", f"{LOCAL}:local-aaaaaaa"]
    )
    assert f"kept for the way back: {prev}" in out


def test_a_local_build_still_prunes_only_the_local_repository(tmp_path: Path) -> None:
    prev, new = f"{LOCAL}:local-1111111", f"{LOCAL}:local-2222222"
    images = [new, prev, f"{LOCAL}:local-0000000", f"{CI}:sha-0000000"]
    _, removed = update(tmp_path, prev, new, images)
    assert removed == [f"{LOCAL}:local-0000000"]


def test_the_previous_image_stays_when_it_came_from_the_other_repository(tmp_path: Path) -> None:
    """The first switch from a local build to a CI image keeps the local build as the way back."""
    prev, new = f"{LOCAL}:local-1111111", f"{CI}:sha-2222222"
    images = [new, prev, f"{LOCAL}:local-0000000", f"{CI}:sha-0000000"]
    _, removed = update(tmp_path, prev, new, images)
    assert sorted(removed) == sorted([f"{LOCAL}:local-0000000", f"{CI}:sha-0000000"])


def test_a_registry_with_a_port_is_not_taken_for_a_tag(tmp_path: Path) -> None:
    repo = "localhost:5000/sloppy-topcoat-hub"
    prev, new = f"{repo}:sha-1111111", f"{repo}:sha-2222222"
    images = [new, prev, f"{repo}:sha-0000000", "localhost:5000/other:x"]
    _, removed = update(tmp_path, prev, new, images)
    assert removed == [f"{repo}:sha-0000000"]
