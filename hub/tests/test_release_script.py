"""scripts/dev/release.sh: the order of the steps and that a dry run changes nothing. The script
itself talks to Docker and ssh; those parts are only printed here."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "release.sh"


def run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k != "TOPCOAT_PROD_SSH"} | env
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def tags() -> str:
    return subprocess.run(
        ["git", "tag", "--list", "backup/*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def test_a_dry_run_lists_every_step_in_order_and_changes_nothing() -> None:
    before = tags()
    r = run("--dry-run", TOPCOAT_PROD_SSH="ops@prod.example.lan")
    out = r.stdout
    names = re.findall(r"step \d+ \((\w+)\)", out)
    assert names == ["tag", "check", "build", "smoke", "wayback", "transfer", "prod"], out
    assert "(dry run)" in out and "update-hub.sh --dry-run" in out
    assert tags() == before  # the backup tag is only announced
    # production is always updated through the script that dumps, checks and rolls back
    assert out.index("(transfer)") < out.index("(prod)")


def test_a_registry_image_is_pulled_instead_of_built() -> None:
    """D123: CI builds the image, the maintainer rolls it out; every other step stays."""
    ref = "ghcr.io/example-owner/sloppy-topcoat-hub:sha-0123456"
    r = run("--dry-run", "--image", ref, TOPCOAT_PROD_SSH="ops@prod.example.lan")
    assert r.returncode == 0, r.stdout + r.stderr
    names = re.findall(r"step \d+ \((\w+)\)", r.stdout)
    assert names == ["tag", "check", "pull", "smoke", "wayback", "transfer", "prod"], r.stdout
    assert f"(dry run) docker pull {ref}" in r.stdout
    assert "docker build" not in r.stdout
    assert "smoke-test-hub-image.sh ghcr.io/example-owner/sloppy-topcoat-hub@sha256:" in r.stdout
    assert f"update-hub.sh --dry-run --load /tmp/topcoat-image.tar.gz {ref}" in r.stdout


def test_a_name_without_a_registry_is_still_built_locally() -> None:
    for ref in ("topcoat-hub:mine", "team/topcoat-hub:mine"):
        r = run("--dry-run", "--image", ref, "--stop-after", "build")
        assert r.returncode == 0 and "stopped after build" in r.stdout, r.stdout + r.stderr
        assert f"docker build -f hub/Dockerfile -t {ref} ." in r.stdout
        assert "docker pull" not in r.stdout


def test_an_unknown_stop_after_step_refuses_before_anything_runs() -> None:
    """A pulled image has no step "build": --stop-after build must not run on to the server."""
    ref = "ghcr.io/example-owner/sloppy-topcoat-hub:sha-0123456"
    for args in (("--image", ref, "--stop-after", "build"), ("--stop-after", "biuld")):
        r = run("--dry-run", *args, TOPCOAT_PROD_SSH="ops@prod.example.lan")
        assert r.returncode == 64 and "--stop-after" in r.stderr, r.stdout + r.stderr
        assert "step 1" not in r.stdout
    r = run("--dry-run", "--image", ref, "--stop-after", "pull")
    assert r.returncode == 0 and "stopped after pull" in r.stdout, r.stdout + r.stderr


def test_the_test_hub_is_gone() -> None:
    r = run("--dry-run", "--skip-test-hub", TOPCOAT_PROD_SSH="ops@prod.example.lan")
    assert r.returncode == 64 and "unknown option" in r.stderr
    assert "test hub" not in run("--dry-run", TOPCOAT_PROD_SSH="ops@prod.example.lan").stdout


def test_stop_after_ends_cleanly_and_the_remote_steps_need_a_server() -> None:
    r = run("--dry-run", "--stop-after", "build")
    assert r.returncode == 0 and "stopped after build" in r.stdout
    r = run("--dry-run", "--stop-after", "smoke")
    assert "(wayback)" not in r.stdout
    r = run("--dry-run")  # no TOPCOAT_PROD_SSH
    assert r.returncode == 64 and "TOPCOAT_PROD_SSH" in r.stderr


def test_without_a_main_ref_a_dry_run_goes_on_and_a_real_run_stops(tmp_path: Path) -> None:
    """A shallow checkout of a pull request has neither origin/main nor main: the dry run only
    announces the backup tag, a real run refuses (it could not mark what it would replace)."""
    (tmp_path / "scripts" / "dev").mkdir(parents=True)
    script = tmp_path / "scripts" / "dev" / "release.sh"
    script.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    git = ["git", "-c", "user.email=ci@example.lan", "-c", "user.name=ci"]
    for args in (["init", "-q", "-b", "work"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run([*git, *args], cwd=tmp_path, check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if k != "TOPCOAT_PROD_SSH"}

    def go(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip

    dry = go("--dry-run", "--stop-after", "tag")
    assert dry.returncode == 0 and "stopped after tag" in dry.stdout, dry.stdout + dry.stderr
    real = go("--stop-after", "tag")
    assert real.returncode == 65 and "origin/main" in real.stderr


DIGEST = "0123456789abcdef" * 4
CI_REPO = "ghcr.io/example-owner/sloppy-topcoat-hub"


def test_a_tagged_registry_image_is_smoke_tested_by_its_digest() -> None:
    """#66: what was smoke-tested is what goes out; the tag is only how it is named."""
    ref = f"{CI_REPO}:sha-0123456"
    r = run("--dry-run", "--image", ref, TOPCOAT_PROD_SSH="ops@prod.example.lan")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"smoke-test-hub-image.sh {CI_REPO}@sha256:<digest of the pull>" in r.stdout
    assert "refuse if the image ID" in r.stdout
    assert f"docker save {ref} |" in r.stdout
    assert f"--load /tmp/topcoat-image.tar.gz {ref}" in r.stdout


def test_a_digest_reference_is_pulled_and_rolled_out_under_a_tag_of_its_digest() -> None:
    ref = f"{CI_REPO}@sha256:{DIGEST}"
    tag = f"{CI_REPO}:digest-{DIGEST[:12]}"
    r = run("--dry-run", "--image", ref, TOPCOAT_PROD_SSH="ops@prod.example.lan")
    assert r.returncode == 0, r.stdout + r.stderr
    names = re.findall(r"step \d+ \((\w+)\)", r.stdout)
    assert names == ["tag", "check", "pull", "smoke", "wayback", "transfer", "prod"], r.stdout
    assert f"(dry run) docker pull {ref}" in r.stdout
    assert f"(dry run) docker tag {ref} {tag}" in r.stdout
    assert f"smoke-test-hub-image.sh {ref}" in r.stdout
    # docker save | load keeps tags, not digests: the server and hub.env get the tag
    assert f"docker save {tag} |" in r.stdout
    assert f"update-hub.sh --dry-run --load /tmp/topcoat-image.tar.gz {tag}" in r.stdout


def test_a_bad_or_local_digest_reference_is_refused() -> None:
    for ref in (
        f"{CI_REPO}@sha256:1234",
        f"{CI_REPO}@md5:{DIGEST}",
        f"sloppy-topcoat-hub@sha256:{DIGEST}",
    ):
        r = run("--dry-run", "--image", ref, TOPCOAT_PROD_SSH="ops@prod.example.lan")
        assert r.returncode == 64 and "digest" in r.stderr, ref + r.stdout + r.stderr
        assert "step 1" not in r.stdout


FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "image inspect --format {{.Id}} "*) cat "$FAKE_ID" ;;
  "image inspect --format "*) printf '%s\\n' "$FAKE_REPO_DIGEST" ;;
  save*) echo image-bytes ;;
esac
exit 0
"""


def _real_run(
    tmp_path: Path, image: str, smoke_moves_tag: bool
) -> subprocess.CompletedProcess[str]:
    """A real run (not --dry-run) up to the transfer, in a scratch repository with fake docker,
    make, ssh and smoke test; the smoke test can move the tag to another image meanwhile."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "dev").mkdir(parents=True)
    (repo / "scripts" / "dev" / "release.sh").write_text(
        SCRIPT.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    id_file = tmp_path / "id"
    id_file.write_text("sha256:aaaa\n", encoding="utf-8")
    smoke = f"#!/usr/bin/env bash\nprintf '%s\\n' \"$1\" > '{tmp_path / 'smoke.arg'}'\n" + (
        f"echo sha256:bbbb > '{id_file}'\n" if smoke_moves_tag else ""
    )
    (repo / "scripts" / "smoke-test-hub-image.sh").write_text(smoke, encoding="utf-8", newline="\n")
    (repo / "scripts" / "smoke-test-hub-image.sh").chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {"docker": FAKE_DOCKER, "make": "#!/usr/bin/env bash\nexit 0\n",
             "ssh": "#!/usr/bin/env bash\ncat > /dev/null\nexit 0\n"}  # fmt: skip
    for name, body in fakes.items():
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    git = ["git", "-c", "user.email=ci@example.lan", "-c", "user.name=ci"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOPCOAT_")} | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "docker.log"),
        "FAKE_ID": str(id_file),
        "FAKE_REPO_DIGEST": f"{CI_REPO}@sha256:{DIGEST}",
        "TOPCOAT_PROD_SSH": "ops@prod.example.lan",
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "dev" / "release.sh"), "--yes", "--image", image,
         "--stop-after", "transfer"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def test_the_transfer_refuses_an_image_that_changed_since_the_smoke_test(tmp_path: Path) -> None:
    r = _real_run(tmp_path, f"{CI_REPO}:sha-0123456", smoke_moves_tag=True)
    assert r.returncode == 65 and "REFUSED" in r.stderr, r.stdout + r.stderr
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "save" not in log


def test_the_transfer_sends_the_image_the_smoke_test_ran_on(tmp_path: Path) -> None:
    r = _real_run(tmp_path, f"{CI_REPO}:sha-0123456", smoke_moves_tag=False)
    assert r.returncode == 0 and "stopped after transfer" in r.stdout, r.stdout + r.stderr
    assert f"pinned: {CI_REPO}@sha256:{DIGEST} (image ID sha256:aaaa)" in r.stdout
    log = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert f"save {CI_REPO}:sha-0123456" in log
    # the smoke test ran on the pinned digest, not on the tag
    smoked = (tmp_path / "smoke.arg").read_text(encoding="utf-8").strip()
    assert smoked == f"{CI_REPO}@sha256:{DIGEST}"


def test_a_digest_reference_is_tagged_after_the_pull(tmp_path: Path) -> None:
    ref = f"{CI_REPO}@sha256:{DIGEST}"
    r = _real_run(tmp_path, ref, smoke_moves_tag=False)
    assert r.returncode == 0, r.stdout + r.stderr
    log = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    tag = f"{CI_REPO}:digest-{DIGEST[:12]}"
    assert log.index(f"pull {ref}") < log.index(f"tag {ref} {tag}") < log.index(f"save {tag}")
