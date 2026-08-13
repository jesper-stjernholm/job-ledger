#!/usr/bin/env bash
#
# One-time setup. Run from inside the job-agent folder:
#     ./setup.sh [repo-name]
#
# Creates the repo, pushes the files, sets both secrets, enables Pages, grants
# the workflow write access, and kicks off the first run.
#
# Secrets are typed at an interactive prompt — they never touch your shell
# history or a file on disk.

set -euo pipefail

REPO_NAME="${1:-job-ledger}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- preflight --------------------------------------------------------------

command -v git >/dev/null || die "git not found"
command -v gh  >/dev/null || die "gh not found — install from https://cli.github.com"
[ -f agent.py ] || die "run this from inside the job-agent folder"

gh auth status >/dev/null 2>&1 || {
  say "Signing in to GitHub"
  gh auth login
}

# --- repo -------------------------------------------------------------------

say "Creating repository: $REPO_NAME"

if [ ! -d .git ]; then
  git init -b main -q
fi

# gh auth login does not configure git's commit identity. On a fresh machine
# the first commit fails with "Author identity unknown" — borrow the identity
# from the authenticated GitHub account, repo-locally.
if ! git config user.email >/dev/null; then
  GH_LOGIN=$(gh api user -q .login)
  GH_EMAIL=$(gh api user -q '.email // empty')
  [ -n "$GH_EMAIL" ] || GH_EMAIL="${GH_LOGIN}@users.noreply.github.com"
  git config user.name  "$(gh api user -q '.name // .login')"
  git config user.email "$GH_EMAIL"
  echo "Set git identity for this repo: $GH_EMAIL"
fi

cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.venv/
.env
# Your profile is uploaded as an encrypted GitHub secret, never committed.
# This repo is PUBLIC — do not remove this line.
profile.txt
EOF

git add -A
git diff --staged --quiet || git commit -qm "job agent: initial commit"

# --public is required for free GitHub Pages. Swap to --private if you have
# GitHub Pro, but then Pages will need a paid plan or an external host.
gh repo create "$REPO_NAME" --public --source=. --remote=origin --push

SLUG=$(gh repo view --json nameWithOwner -q .nameWithOwner)
say "Repository ready: https://github.com/$SLUG"

# --- secrets ----------------------------------------------------------------

say "Secrets"
echo "Two values are needed. Both are prompted for, not echoed."
echo

echo "1/2  ANTHROPIC_API_KEY  (from https://console.anthropic.com)"
gh secret set ANTHROPIC_API_KEY

echo
if [ -f profile.txt ]; then
  echo "2/2  PROFILE  — reading from profile.txt ($(wc -w < profile.txt | tr -d ' ') words)"
  gh secret set PROFILE < profile.txt
else
  echo "2/2  PROFILE  — your ~150-word profile."
  echo "     Multi-line input: paste it, then press Ctrl-D on a blank line."
  echo "     (On Windows this is fiddly — Ctrl-C out, put your profile in"
  echo "      profile.txt next to this script, and re-run instead.)"
  gh secret set PROFILE
fi

# --- permissions ------------------------------------------------------------
# The workflow commits state/ and docs/ back to the repo. New repos default to
# a read-only GITHUB_TOKEN, which caps the workflow's own permissions block and
# makes the push fail — so raise the repo-level ceiling here.

say "Granting the workflow write access"
gh api -X PUT "repos/$SLUG/actions/permissions/workflow" \
  -f default_workflow_permissions=write >/dev/null

# --- pages ------------------------------------------------------------------

say "Enabling GitHub Pages"
if gh api -X POST "repos/$SLUG/pages" \
     --input - <<< '{"source":{"branch":"main","path":"/docs"}}' >/dev/null 2>&1
then
  echo "Pages enabled on main:/docs"
else
  echo "Pages may already be enabled, or needs a moment. Check:"
  echo "  https://github.com/$SLUG/settings/pages"
fi

# --- first run --------------------------------------------------------------

say "Triggering the first run"
sleep 3   # give GitHub a moment to register the workflow file
gh workflow run "daily job run" 2>/dev/null \
  && echo "Started. Watch it with: gh run watch" \
  || echo "Start it by hand from the Actions tab."

USER_LOGIN="${SLUG%%/*}"
cat <<EOF

────────────────────────────────────────────────
Done.

  Repo      https://github.com/$SLUG
  Runs      https://github.com/$SLUG/actions
  Board     https://$USER_LOGIN.github.io/$REPO_NAME/

The board 404s until the first run finishes and Pages builds — usually
two or three minutes.

Next: edit config.yaml with the companies you actually want to watch,
then commit and push. Test rule changes locally with:

  python agent.py --dry-run
────────────────────────────────────────────────
EOF
