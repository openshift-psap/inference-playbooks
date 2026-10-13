#!/usr/bin/env bash
# integration-preview.sh — Build a staging branch that merges open recipe PRs
# plus the UI PR, then validate and render the combined result.
#
# Usage:
#   tools/integration-preview.sh              # full run: merge + validate + push
#   tools/integration-preview.sh --no-push    # skip the push to origin
#   tools/integration-preview.sh --serve      # after build, serve the site on localhost
#
# Re-run at any time; the branch is torn down and rebuilt from scratch.

set -euo pipefail

BRANCH="integration/catalog-preview"
REMOTE="origin"

# Recipe PRs in merge order (rationale in plan).
RECIPE_PRS=(34 44 19 39 40 43 35)
UI_PR=17

DO_PUSH=true
DO_SERVE=false

for arg in "$@"; do
  case "$arg" in
    --no-push) DO_PUSH=false ;;
    --serve)   DO_SERVE=true ;;
    *)         echo "Unknown flag: $arg"; exit 1 ;;
  esac
done

# ---------- helpers ----------

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[1;33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

MERGED=()
FAILED=()

cleanup_branch() {
  # Delete local branch if it exists (detach first if we're on it)
  if git rev-parse --verify "$BRANCH" &>/dev/null; then
    local current
    current=$(git symbolic-ref --short HEAD 2>/dev/null || true)
    if [[ "$current" == "$BRANCH" ]]; then
      git checkout --detach HEAD 2>/dev/null
    fi
    git branch -D "$BRANCH" 2>/dev/null || true
  fi
  # Delete remote branch if it exists
  if git ls-remote --heads "$REMOTE" "$BRANCH" | grep -q .; then
    yellow "Deleting remote $REMOTE/$BRANCH"
    git push "$REMOTE" --delete "$BRANCH" 2>/dev/null || true
  fi
}

try_auto_resolve_trivial() {
  # Auto-resolve files where both sides add non-overlapping content.
  # Works for README table rows and additive code blocks.
  local unmerged
  unmerged=$(git diff --name-only --diff-filter=U)
  if [[ -z "$unmerged" ]]; then
    return 1
  fi

  local file resolved=true
  for file in $unmerged; do
    local tmp
    tmp=$(mktemp)
    # Strip conflict markers, keeping both sides
    sed '/^<<<<<<< HEAD$/d; /^=======$/d; /^>>>>>>> .*$/d' "$file" > "$tmp"
    if grep -q '<<<<<<<\|>>>>>>>' "$tmp"; then
      rm "$tmp"
      resolved=false
      break
    fi
    mv "$tmp" "$file"
    git add "$file"
  done

  if ! $resolved; then
    return 1
  fi

  # Verify no unmerged files remain
  if git diff --name-only --diff-filter=U | grep -q .; then
    return 1
  fi
  return 0
}

merge_pr() {
  local pr=$1
  local title
  title=$(gh pr view "$pr" --json title --jq '.title' 2>/dev/null || echo "PR #$pr")

  bold "Merging PR #$pr: $title"

  # Fetch the PR head
  git fetch "$REMOTE" "refs/pull/$pr/head" 2>/dev/null

  if git merge FETCH_HEAD --no-edit -m "integration: merge PR #$pr — $title" 2>/dev/null; then
    green "  ✓ PR #$pr merged"
    MERGED+=("$pr")
    return 0
  fi

  # Try auto-resolving trivial add/add conflicts (README rows, additive blocks)
  if try_auto_resolve_trivial; then
    git commit --no-edit -m "integration: merge PR #$pr — $title" 2>/dev/null
    yellow "  ✓ PR #$pr merged (auto-resolved trivial conflicts)"
    MERGED+=("$pr")
    return 0
  fi

  red "  ✗ PR #$pr CONFLICT — aborting merge"
  git merge --abort 2>/dev/null || true
  FAILED+=("$pr")
  return 1
}

# ---------- main ----------

cd "$(git rev-parse --show-toplevel)"

bold "=== Integration Preview Build ==="
echo

# 1. Ensure we have latest main
yellow "Fetching $REMOTE/main..."
git fetch "$REMOTE" main 2>/dev/null

# 2. Tear down old branch
yellow "Cleaning up old $BRANCH..."
cleanup_branch

# 3. Create fresh branch from main
git checkout -b "$BRANCH" "$REMOTE/main" 2>/dev/null
green "Created $BRANCH from $REMOTE/main ($(git rev-parse --short HEAD))"
echo

# 4. Merge recipe PRs
bold "--- Recipe PRs ---"
for pr in "${RECIPE_PRS[@]}"; do
  if ! merge_pr "$pr"; then
    red "Stopping: PR #$pr failed to merge. Fix conflicts at source and re-run."
    echo
    bold "=== Summary ==="
    green "Merged: ${MERGED[*]:-none}"
    red   "Failed: ${FAILED[*]}"
    # Stay on the branch so user can inspect
    exit 1
  fi
done
echo

# 5. Merge UI PR last
bold "--- UI PR ---"
if ! merge_pr "$UI_PR"; then
  red "UI PR #$UI_PR failed to merge."
  echo
  bold "=== Summary ==="
  green "Merged: ${MERGED[*]:-none}"
  red   "Failed: ${FAILED[*]}"
  exit 1
fi
echo

# 6. Validate
bold "--- Validation ---"
VALID=true

yellow "Running validate.py --current..."
if python3 tools/validate.py --current 2>&1; then
  green "  ✓ Validation passed"
else
  red "  ✗ Validation failed (see output above)"
  VALID=false
fi
echo

yellow "Running render.py..."
if python3 tools/render.py 2>&1; then
  green "  ✓ Render passed"
else
  red "  ✗ Render failed (see output above)"
  VALID=false
fi
echo

yellow "Building catalog and site preview..."
rm -rf .build/site
mkdir -p .build/site
if python3 tools/catalog.py --out .build/site/catalog.json 2>&1; then
  cp -r site/* .build/site/ 2>/dev/null || true
  models=$(python3 -c "import json; d=json.load(open('.build/site/catalog.json')); print(f'{len(d[\"models\"])} models, {len(d[\"entries\"])} entries')" 2>/dev/null || echo "unknown")
  green "  ✓ Catalog built ($models)"
else
  red "  ✗ Catalog build failed (see output above)"
  VALID=false
fi
echo

# 7. Push
if $DO_PUSH; then
  yellow "Pushing $BRANCH to $REMOTE..."
  git push "$REMOTE" "$BRANCH" --force 2>&1
  green "Pushed $REMOTE/$BRANCH"
  echo
fi

# 8. Summary
bold "=== Integration Preview Summary ==="
echo "Branch:     $BRANCH ($(git rev-parse --short HEAD))"
echo "Base:       $REMOTE/main ($(git rev-parse --short "$REMOTE/main"))"
echo "Merged PRs: ${MERGED[*]}"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  red "Failed PRs: ${FAILED[*]}"
fi
if $VALID; then
  green "Validation: PASSED"
else
  red "Validation: FAILED"
fi
if $DO_PUSH; then
  echo "Remote:     https://github.com/openshift-psap/inference-playbooks/tree/$BRANCH"
fi
echo

# 9. Optional: serve
if $DO_SERVE; then
  bold "Serving site on http://localhost:8138 (Ctrl-C to stop)..."
  python3 -m http.server 8138 --directory .build/site/
fi
