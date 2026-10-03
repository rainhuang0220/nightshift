"""Default overnight policy: deny rules, allow rules, and the advisory preamble.

The preamble is an instruction to the model. It is not a security boundary.
Allow rules are Grok tool-call filters. They are not filesystem containment.
The PATH shim is defense in depth. It is not the principal boundary.
Filesystem containment is the seatbelt profile. Source protection is the
independent clone plus the integrity gate. Human review is the final approval.
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

# Name allowlists are defense in depth. An allowed interpreter can still call
# absolute binaries. Filesystem containment is the seatbelt profile.
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
_DROPPED_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "KAGGLE_", "NPM_", "PYPI_", "GH_", "GITHUB_")

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
    if job.work_order and "commit" not in job.work_order["actions"]["allowed"]:
        deny += ("Bash(git commit*)",)
    if job.write_scope == "workspace" and job.repository:
        # Extra Grok-level denies for the source checkout. These are tool-call
        # filters. The isolated clone and the seatbelt are the containment.
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
            "Permission rules are not filesystem containment.",
            "Nightshift runs you inside an isolated workspace. The original",
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
            "Stay inside the workspace. Local commits in this workspace are allowed",
            "when the job write scope is workspace. Leave verification to Nightshift.",
            "",
            "---",
            "",
        ]
    )


def _sensitive_name(name: str) -> bool:
    if SECRET_ENV_RE.search(name):
        return True
    upper = name.upper()
    return upper.startswith(_DROPPED_PREFIXES)


def minimal_env(parent: dict[str, str], *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Child environment with secret-looking variables removed.

    HOME stays in the whitelist so callers can supply a directory. The runner
    replaces it with an isolated runtime home before a provider starts. This
    function is not the extension-isolation boundary.
    """
    kept: dict[str, str] = {}
    for name in _KEPT_ENV:
        if name in parent and not _sensitive_name(name):
            kept[name] = parent[name]
    for name, value in (extra or {}).items():
        if _sensitive_name(name):
            continue
        if "\n" in name or "\x00" in name:
            continue
        kept[name] = value
    return kept


def scrub_text(text: str) -> str:
    """Best-effort redaction for logs. Not a cryptographic guarantee.

    Arbitrary repository content can contain secret formats this pattern does
    not recognize. Logs stay on disk; they are not deleted after redaction.
    """
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+\S+", r"\1[redacted]", text)
    text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})", "[redacted]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)",
        r"\1[redacted]",
        text,
    )
    return text


def package_pythonpath() -> str:
    import nightshift

    return str(Path(nightshift.__file__).resolve().parent.parent)


def scrub_data(value):
    """Scrub string values before JSON encoding, preserving valid JSON."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, list):
        return [scrub_data(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_data(item) for key, item in value.items()}
    return value
