#!/usr/bin/env bash
# Enregistre data/ dans le dépôt, en réessayant si un autre calcul a poussé entre-temps.
set -u
git config user.name "sous-bois-bot"
git config user.email "sous-bois-bot@users.noreply.github.com"
git add data/
git diff --cached --quiet && { echo "Rien de nouveau."; exit 0; }
git commit -q -m "$1"
for i in 1 2 3 4 5; do
  git pull -q --rebase -X theirs origin main && git push -q origin HEAD:main && exit 0
  sleep $((i * 7))
done
exit 1
