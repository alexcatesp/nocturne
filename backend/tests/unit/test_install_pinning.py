"""What counts as a pinned upstream ref — ADR 0006.

The guard decides whether a source tree may be built. It got the question wrong
in two ways at once, and both were found by asking it about real repositories
rather than by reading it:

* It accepted **only tags**, refusing a full commit SHA — which is the strongest
  pin there is, since a tag is a label upstream can move or delete and a commit
  is the commit. That refusal is what made KStars look unpinnable: KStars stops
  tagging at v17.08.3 and cuts modern releases from ``stable-3.x.y`` branches.
* It asked ``git describe --exact-match --tags HEAD``, which establishes that
  HEAD is at *some* tag and never that it is at the *requested* one. A checkout
  that landed elsewhere passed, and ``versions.lock`` then recorded the ref that
  was asked for beside a SHA belonging to something else.

These run the shipped function against real git repositories built in tmp_path,
because a guard tested only against its own author's mental model is exactly the
kind that passes while enforcing nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install.sh"

#: Stubs for the helpers install.sh defines elsewhere. ``fail`` exits non-zero,
#: which is the behaviour under test.
HARNESS = """
set -euo pipefail
ALLOW_UNPINNED=0
info() { :; }
warn() { echo "WARN: $*" >&2; }
fail() { echo "FAIL: $*" >&2; exit 1; }
"""


def extract_function(name: str) -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", source, re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name}() is no longer defined in scripts/install.sh"
    return match.group(0)


def run_guard(
    ref: str, directory: Path, *, allow_unpinned: bool = False
) -> subprocess.CompletedProcess[str]:
    script = HARNESS
    if allow_unpinned:
        script += "ALLOW_UNPINNED=1\n"
    script += extract_function("is_full_sha") + "\n"
    script += extract_function("require_pinned_ref") + "\n"
    script += f'require_pinned_ref upstream "{ref}" "{directory}"\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A repository with two commits, the first tagged twice.

    Two tags on one commit is not contrived: indi-3rdparty ships v2.2.3 and
    v2.2.3.1 pointing at the same commit, which is what makes "HEAD is at some
    tag" a useless question.
    """
    directory = tmp_path / "upstream"
    directory.mkdir()
    git(directory, "init", "--quiet", "-b", "main")
    git(directory, "config", "user.email", "test@example.invalid")
    git(directory, "config", "user.name", "Test")

    (directory / "a.txt").write_text("one", encoding="utf-8")
    git(directory, "add", "-A")
    git(directory, "commit", "--quiet", "-m", "first")
    git(directory, "tag", "v1.0")
    git(directory, "tag", "v1.0.1")

    (directory / "a.txt").write_text("two", encoding="utf-8")
    git(directory, "add", "-A")
    git(directory, "commit", "--quiet", "-m", "second")
    git(directory, "tag", "v2.0")
    return directory


def checkout(directory: Path, revision: str) -> str:
    git(directory, "checkout", "--quiet", revision)
    return git(directory, "rev-parse", "HEAD")


class TestGitIsAvailable:
    def test_the_tools_these_tests_need_exist(self) -> None:
        """Guard: without these every test below would pass for the wrong reason."""
        assert shutil.which("bash") is not None
        assert shutil.which("git") is not None


class TestAFullShaIsAcceptedAsPinned:
    """The defect that made KStars look unpinnable."""

    def test_a_full_sha_at_head_passes(self, upstream: Path) -> None:
        head = checkout(upstream, "v2.0")
        result = run_guard(head, upstream)
        assert result.returncode == 0, result.stderr

    def test_a_full_sha_on_an_untagged_commit_passes(self, upstream: Path) -> None:
        """KStars' situation exactly: a real commit that carries no tag at all."""
        (upstream / "b.txt").write_text("three", encoding="utf-8")
        git(upstream, "add", "-A")
        git(upstream, "commit", "--quiet", "-m", "untagged")
        head = git(upstream, "rev-parse", "HEAD")
        assert git(upstream, "tag", "--points-at", "HEAD") == ""

        result = run_guard(head, upstream)
        assert result.returncode == 0, result.stderr

    def test_a_sha_that_is_not_what_was_checked_out_is_refused(self, upstream: Path) -> None:
        """The verification that makes the branch-hint clone strategy safe."""
        other = git(upstream, "rev-parse", "v1.0")
        head = checkout(upstream, "v2.0")
        assert other != head

        result = run_guard(other, upstream)
        assert result.returncode != 0
        assert other in result.stderr
        assert head in result.stderr
        assert "Nothing has been built" in result.stderr

    def test_a_mismatched_sha_is_refused_even_with_allow_unpinned(self, upstream: Path) -> None:
        """--allow-unpinned relaxes "is it pinned", never "is it the right tree"."""
        other = git(upstream, "rev-parse", "v1.0")
        checkout(upstream, "v2.0")

        result = run_guard(other, upstream, allow_unpinned=True)
        assert result.returncode != 0, result.stdout + result.stderr


class TestTheRequestedTagMustBeTheOneCheckedOut:
    """ "HEAD is at some tag" is not the question."""

    def test_the_matching_tag_passes(self, upstream: Path) -> None:
        checkout(upstream, "v2.0")
        assert run_guard("v2.0", upstream).returncode == 0

    def test_a_different_tag_on_the_same_commit_passes(self, upstream: Path) -> None:
        """v1.0 and v1.0.1 are one commit; asking for either is honest."""
        checkout(upstream, "v1.0.1")
        assert run_guard("v1.0", upstream).returncode == 0

    def test_a_tag_pointing_somewhere_else_is_refused(self, upstream: Path) -> None:
        """The hole in the old guard: HEAD was tagged, so it passed."""
        checkout(upstream, "v2.0")
        assert git(upstream, "tag", "--points-at", "HEAD") == "v2.0"

        result = run_guard("v1.0", upstream)
        assert result.returncode != 0, (
            "HEAD is at v2.0 but v1.0 was requested, and the guard allowed it"
        )


class TestWhatIsStillRefused:
    def test_a_branch_name_is_refused(self, upstream: Path) -> None:
        checkout(upstream, "main")
        result = run_guard("main", upstream)
        assert result.returncode != 0
        assert "reproducible" in result.stderr

    def test_a_short_sha_is_refused(self, upstream: Path) -> None:
        head = checkout(upstream, "v2.0")
        result = run_guard(head[:12], upstream)
        assert result.returncode != 0
        assert "short SHA" in result.stderr

    def test_an_uppercase_sha_is_refused_rather_than_half_accepted(
        self, upstream: Path
    ) -> None:
        """git prints lowercase; an uppercase ref would never compare equal."""
        head = checkout(upstream, "v2.0")
        assert run_guard(head.upper(), upstream).returncode != 0

    def test_allow_unpinned_lets_a_branch_through_with_a_warning(self, upstream: Path) -> None:
        checkout(upstream, "main")
        result = run_guard("main", upstream, allow_unpinned=True)
        assert result.returncode == 0, result.stderr
        assert "NOT reproducible" in result.stderr


class TestTheShaShapeCheck:
    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            ("61d849b04c42217cf2f0ab956153e56a928ae8a8", True),
            ("478d34a34e5dde5ef574bb23917618508707663d", True),
            ("61d849b0", False),
            ("stable-3.8.3", False),
            ("v2.2.4", False),
            ("", False),
            ("61d849b04c42217cf2f0ab956153e56a928ae8a" + "z", False),
            ("61D849B04C42217CF2F0AB956153E56A928AE8A8", False),
        ],
    )
    def test_only_a_full_lowercase_hex_sha_counts(self, candidate: str, expected: bool) -> None:
        script = f'{HARNESS}\n{extract_function("is_full_sha")}\nis_full_sha "{candidate}"\n'
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        assert (result.returncode == 0) is expected, candidate


class TestThePinsThemselves:
    """The values shipped in install.sh, checked for shape rather than content."""

    def installer(self) -> str:
        return INSTALLER.read_text(encoding="utf-8")

    def test_kstars_is_pinned_to_a_full_commit(self) -> None:
        match = re.search(r'KSTARS_REF="\$\{NOCTURNE_KSTARS_REF:-([^}]*)\}"', self.installer())
        assert match is not None, "KSTARS_REF is no longer defined as expected"
        assert re.fullmatch(r"[0-9a-f]{40}", match.group(1)), match.group(1)

    def test_kstars_carries_a_branch_to_find_that_commit_on(self) -> None:
        assert re.search(
            r'KSTARS_BRANCH="\$\{NOCTURNE_KSTARS_BRANCH:-stable-3\.', self.installer()
        )

    def test_the_installer_says_kstars_has_no_usable_tags(self) -> None:
        """So the next person does not go looking for one."""
        assert "does NOT tag its modern releases" in self.installer()
