#!/usr/bin/env bash
# Build a demo repo at /tmp/git-cola-rewind-check-demo for manual verification
# of the "Rewind check" menu in the right-hand DAG view.
#
# Expected flow after running this script:
#   cd /tmp/git-cola-rewind-check-demo
#   python -m cola dag        # or: garden -C /path/to/git-cola run
#   In the right DAG, click the "feature" branch label -> "Rewind check".
#   The search should hit c3, prompt to confirm, and on accept create
#   "rewind-feature" backup branch and reset --hard to c3.

set -euo pipefail

DEMO=/tmp/git-cola-rewind-check-demo

rm -rf "$DEMO"
mkdir -p "$DEMO"
cd "$DEMO"

git init -q
git symbolic-ref HEAD refs/heads/main
git config user.email demo@example.com
git config user.name demo
git config commit.gpgsign false
git config tag.gpgsign false

printf 'a1\n' > a.txt
printf 'b1\n' > b.txt
git add . && git commit -q -m c1

printf 'a2\n' > a.txt
git commit -q -am c2

printf 'a3\n' > a.txt
printf 'b2\n' > b.txt
git commit -q -am c3

printf 'a4\n' > a.txt
printf 'b3\n' > b.txt
git commit -q -am c4

git checkout -q -b feature

# Restore the worktree to c3's contents -> the "Rewind check" menu should
# detect c3 as the matching old commit (both a.txt and b.txt are now dirty).
printf 'a3\n' > a.txt
printf 'b2\n' > b.txt

echo
echo "Demo repo ready at: $DEMO"
echo
git log --oneline --decorate --all
echo
echo "Worktree status:"
git status --short
echo
echo "Next steps:"
echo "  cd $DEMO"
echo "  python -m cola dag"
echo "  -> right DAG: click 'feature' label -> 'Rewind check'"
echo "  -> confirm; verify a new 'rewind-feature' branch and HEAD at c3."
