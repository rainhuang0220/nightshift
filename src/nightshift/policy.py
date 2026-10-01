"""Default overnight policy: deny rules, allow rules, and the advisory preamble.

The preamble is an instruction to the model. It is not a security boundary.
Enforcement that Nightshift itself can guarantee lives in the worktree, the
PATH shim, environment minimization, and the post-run source audit.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from nightshift.models import Job, UsageError

# Grok deny rules. Upstream documents that deny wins over allow and over
# always-approve. That enforcement is inside Grok, not an OS boundary.
DENY_RULES: tuple[str, ...] = (
    "Bash(git push*)",
    "Bash(git push *)",
    "Bash(sudo*)",
    "Bash(sudo *)",
    "Bash(rm -rf*)",
    "Bash(rm -rf *)",
    "Bash(rm -fr*)",
    "Bash(gh pr create*)",
    "Bash(gh pr merge*)",
    "Bash(gh pr close*)",
    "Bash(gh pr comment*)",
    "Bash(gh issue create*)",
    "Bash(gh issue comment*)",
    "Bash(gh issue edit*)",
    "Bash(gh release*)",
    "Bash(gh repo create*)",
    "Bash(gh repo delete*)",
    "Bash(kaggle*)",
    "Bash(npm publish*)",
    "Bash(pnpm publish*)",
    "Bash(yarn publish*)",
    "Bash(twine upload*)",
    "Bash(cargo publish*)",
    "Bash(git config --global*)",
    "Bash(git config --system*)",
    "Bash(git clean*)",
    "Bash(git reset --hard*)",
    "Read(**/.ssh/**)",
    "Read(**/.aws/**)",
    "Read(**/.config/gh/**)",
    "Read(**/.git-credentials)",
    "Read(**/.netrc)",
    "Read(**/.npmrc)",
    "Read(**/*.pem)",
    "Read(**/.env)",
    "Read(**/.env.*)",
    "Edit(**/.ssh/**)",
    "Edit(**/.aws/**)",
    "Edit(**/.gitconfig)",
    "Edit(**/.git-credentials)",
    "Edit(**/.config/gh/**)",
)

READ_ALLOW: tuple[str, ...] = (
    "Read",
    "Grep",
    "Bash(git status*)",
    "Bash(git diff*)",
    "Bash(git log*)",
    "Bash(git show*)",
    "Bash(git rev-parse*)",
    "Bash(git ls-files*)",
    "Bash(git blame*)",
    "Bash(ls*)",
    "Bash(cat*)",
    "Bash(pwd*)",
    "Bash(rg*)",
    "Bash(find*)",
)

WORKSPACE_WRITE_ALLOW: tuple[str, ...] = (
    "Edit",
    "Write",
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git checkout*)",
    "Bash(git switch*)",
    "Bash(git restore*)",
    "Bash(git branch*)",
    "Bash(git merge*)",
    "Bash(git rebase*)",
    "Bash(git stash*)",
    "Bash(python*)",
    "Bash(python3*)",
    "Bash(pytest*)",
    "Bash(ruff*)",
    "Bash(mypy*)",
    "Bash(npm test*)",
    "Bash(npm run*)",
    "Bash(cargo test*)",
    "Bash(cargo check*)",
    "Bash(go test*)",
    "Bash(make*)",
)

NETWORK_ALLOW: tuple[str, ...] = (
    "WebSearch",
    "WebFetch",
)

SECRET_ENV_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PASSWD|SESSION)", re.I)

_KEPT_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "PYTHONPATH",
    "PYTHONHOME",
    "SYSTEMROOT",
    "COMSPEC",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


@dataclass(frozen=True)
class Policy:
    permission_mode: str
    sandbox_profile: str
    deny: tuple[str, ...]
    allow: tuple[str, ...]
    disable_web_search: bool
    preamble: str


def build_policy(job: Job, *, permission_mode: str = "dontAsk") -> Policy:
    if permission_mode in {"bypassPermissions", "always-approve", "always_approve"}:
        raise UsageError("refusing a permission mode that bypasses approvals")
    if job.write_scope == "none":
        sandbox = "read-only"
        allow = READ_ALLOW
    else:
        sandbox = "workspace"
        allow = READ_ALLOW + WORKSPACE_WRITE_ALLOW
    extra = []
    for pattern in job.allow_bash:
        extra.append(pattern if pattern.startswith("Bash(") else f"Bash({pattern})")
    if job.network:
        allow = allow + NETWORK_ALLOW
    deny = DENY_RULES
    if job.write_scope == "workspace" and job.repository:
        # Extra Grok-level denies for the source checkout. The worktree is a
        # different path, so edits there still match the Edit allow rule.
        repo = str(Path(job.repository).expanduser())
        deny = deny + (
            f"Edit({repo}/**)",
            f"Write({repo}/**)",
        )
    return Policy(
        permission_mode=permission_mode,
        sandbox_profile=sandbox,
        deny=deny,
        allow=allow + tuple(extra),
        disable_web_search=not job.network,
        preamble=render_preamble(job),
    )


def render_preamble(job: Job) -> str:
    network = "permitted for read-only research" if job.network else "not permitted"
    writes = (
        "You may edit files only inside this workspace."
        if job.write_scope == "workspace"
        else "Do not create, edit, or delete files. This job is read-only."
    )
    return "\n".join(
        [
            "# Nightshift policy",
            "",
            "This preamble is an instruction. It is not a security boundary.",
            "Nightshift runs you inside an isolated git worktree. The original",
            "checkout is off limits.",
            "",
            writes,
            f"Network access is {network}.",
            "",
            "Do not do any of the following:",
            "- git push, or any force push",
            "- open, merge, or comment on a pull request",
            "- submit a Kaggle entry or publish a release or package",
            "- deploy to production",
            "- send email or chat messages",
            "- modify remote issues",
            "- use sudo",
            "- delete or rewrite the original repository",
            "- git reset --hard or git clean against the original checkout",
            "- change global or system git configuration",
            "- read or write credentials, tokens, or SSH keys",
            "",
            "Stay inside the workspace. Local commits in this worktree are allowed",
            "when the job write scope is workspace. Leave verification to Nightshift.",
            "",
            "---",
            "",
        ]
    )


def minimal_env(parent: dict[str, str], *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Child environment with secret-looking variables removed.

    HOME is kept because Grok and git need a home directory. The caller
    redirects GIT_CONFIG_GLOBAL at an empty file so the child cannot change
    the operator's global git config through git itself.
    """
    kept: dict[str, str] = {}
    for name in _KEPT_ENV:
        if name in parent and not SECRET_ENV_RE.search(name):
            kept[name] = parent[name]
    for name, value in (extra or {}).items():
        if SECRET_ENV_RE.search(name):
            continue
        if "\n" in name or "\x00" in name:
            continue
        kept[name] = value
    return kept


def scrub_text(text: str) -> str:
    """Best-effort redaction for logs. Not a guarantee against every secret shape."""
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    return text


def package_pythonpath() -> str:
    import nightshift

    return str(Path(nightshift.__file__).resolve().parent.parent)
