import re
import shutil
import subprocess
from pathlib import Path


class GitRepoManager:
    def sync_repo(self, repo_url: str, branch: str, target_dir: Path) -> tuple[str, str]:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        effective_branch = branch

        if not self._is_managed_repo_dir(target_dir, repo_url):
            self._reset_target_dir(target_dir)
            try:
                self._clone_repo(repo_url, effective_branch, target_dir)
            except RuntimeError:
                detected_branch = self.detect_default_branch(repo_url)
                if detected_branch and detected_branch != effective_branch:
                    effective_branch = detected_branch
                    self._clone_repo(repo_url, effective_branch, target_dir)
                else:
                    raise
        else:
            try:
                self._sync_existing_repo(target_dir, effective_branch)
            except RuntimeError:
                detected_branch = self.detect_default_branch(repo_url)
                if detected_branch and detected_branch != effective_branch:
                    effective_branch = detected_branch
                    self._sync_existing_repo(target_dir, effective_branch)
                else:
                    raise

        commit_hash = self._run(["git", "-C", str(target_dir), "rev-parse", "HEAD"]).strip()
        return commit_hash, effective_branch

    def repo_slug(self, repo_url: str) -> str:
        slug = repo_url.rstrip("/").split("/")[-1]
        slug = re.sub(r"\.git$", "", slug)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug)
        return slug or "repository"

    def detect_default_branch(self, repo_url: str) -> str | None:
        try:
            output = self._run(["git", "ls-remote", "--symref", repo_url, "HEAD"])
        except RuntimeError:
            output = ""

        for line in output.splitlines():
            if line.startswith("ref: ") and "\tHEAD" in line:
                ref = line.split()[1]
                return ref.rsplit("/", 1)[-1]

        try:
            heads_output = self._run(["git", "ls-remote", "--heads", repo_url])
        except RuntimeError:
            return None

        candidates = []
        for line in heads_output.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            ref = parts[1]
            if ref.startswith("refs/heads/"):
                candidates.append(ref.removeprefix("refs/heads/"))

        for preferred in ("main", "master", "dev"):
            if preferred in candidates:
                return preferred
        return candidates[0] if candidates else None

    def _clone_repo(self, repo_url: str, branch: str, target_dir: Path) -> None:
        self._run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                branch,
                repo_url,
                str(target_dir),
            ]
        )

    def _sync_existing_repo(self, target_dir: Path, branch: str) -> None:
        self._run(["git", "-C", str(target_dir), "fetch", "origin", branch, "--depth", "1"])
        self._run(["git", "-C", str(target_dir), "checkout", branch])
        self._run(["git", "-C", str(target_dir), "reset", "--hard", f"origin/{branch}"])

    def _is_managed_repo_dir(self, target_dir: Path, repo_url: str) -> bool:
        if not target_dir.exists() or not target_dir.is_dir():
            return False
        top_level = self._git_toplevel(target_dir)
        if not top_level:
            return False
        if top_level.resolve() != target_dir.resolve():
            return False
        origin_url = self._git_origin_url(target_dir)
        if not origin_url:
            return False
        return self._normalize_repo_url(origin_url) == self._normalize_repo_url(repo_url)

    def _git_toplevel(self, target_dir: Path) -> Path | None:
        try:
            output = self._run(["git", "-C", str(target_dir), "rev-parse", "--show-toplevel"]).strip()
        except RuntimeError:
            return None
        if not output:
            return None
        return Path(output)

    def _git_origin_url(self, target_dir: Path) -> str:
        try:
            return self._run(["git", "-C", str(target_dir), "remote", "get-url", "origin"]).strip()
        except RuntimeError:
            return ""

    def _normalize_repo_url(self, repo_url: str) -> str:
        normalized = repo_url.strip().replace("\\", "/").rstrip("/")
        normalized = re.sub(r"\.git$", "", normalized, flags=re.IGNORECASE)
        return normalized.lower()

    def _reset_target_dir(self, target_dir: Path) -> None:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)

    def _run(self, command: list[str]) -> str:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )
            return completed.stdout
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(message) from exc
