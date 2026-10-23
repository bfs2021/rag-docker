#!/usr/bin/env bash
# The build and run check: step 8 of SKILL.md runs this file, from the
# maintainer's skills directory, as
#   bash {skills}/rag-pr-review/build-check.sh <bundle>
# It is a file rather than a heredoc in SKILL.md because Claude Code substitutes
# positional-argument tokens in a skill's text when the skill is invoked with
# arguments, which mangled the script (#502).
set -u
bundle=$1
main=$(git worktree list --porcelain | awk 'NR==1 {print $2}')
# The trusted harness: the main checkout's must be develop's ("The verify project" in reference.md).
git -C "$main" fetch -q origin develop || { echo "STOP: could not fetch develop"; exit 1; }
git -C "$main" diff --quiet origin/develop -- scripts/verify/stack.sh scripts/verify/lock.sh docker-compose.verify.yml docker-compose.yml || { echo "STOP: the harness isn't develop's"; exit 1; }
# Held until this script exits; exits 3 if another verify run holds it.
. "$main/scripts/verify/lock.sh"
smoke() { for i in $(seq 1 60); do c=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$1"); [ "$c" = 200 ] && { echo "200 after ${i}s"; return 0; }; sleep 1; done; echo "$c: no 200 in 60 s"; return 1; }
rc=0
if bash "$main/scripts/verify/stack.sh" up --checkout "$bundle/merged" --pull; then
  smoke http://localhost:8081/api/health || rc=1
  smoke http://localhost:8081/ || rc=1
  curl -s http://localhost:8081/api/health | python3 -c 'import json,sys; print(json.load(sys.stdin))' || rc=1
else
  echo "stack.sh up failed"; rc=1
fi
bash "$main/scripts/verify/stack.sh" down || rc=1
exit "$rc"
