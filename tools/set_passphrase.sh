#!/bin/bash
# Set the payment passphrase typed into Telegram.
#
# Reads with `read -s`, so it is never echoed to the terminal, never stored in
# shell history, and never passed as an argument (where `ps` would show it).
# It is written only to .env, which is 0600 and gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."

printf 'New payment passphrase (not shown as you type): '
read -rs PASS; echo
printf 'Again to confirm: '
read -rs PASS2; echo

[ "$PASS" = "$PASS2" ]  || { echo "They do not match. Nothing changed."; exit 1; }
[ -n "$PASS" ]          || { echo "Empty passphrase refused. Nothing changed."; exit 1; }
[ ${#PASS} -ge 6 ]      || { echo "Too short — use at least 6 characters. Nothing changed."; exit 1; }

# Backup OUTSIDE the repo. Writing it alongside .env put a file holding every
# live secret into the working tree, where only an exact-name ignore rule stood
# between it and a public push.
BACKUP="${TMPDIR:-/tmp}/grocery-env-backup.$(date +%s)"
cp .env "$BACKUP"; chmod 600 "$BACKUP"
echo "Backed up current .env to $BACKUP"
if grep -q '^BOT_PASSPHRASE=' .env; then
  # A temp file rather than sed -i with the secret on the command line.
  TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
  while IFS= read -r line; do
    case "$line" in
      BOT_PASSPHRASE=*) printf '%s\n' "BOT_PASSPHRASE=$PASS" ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < .env > "$TMP"
  cat "$TMP" > .env
else
  printf '%s\n' "BOT_PASSPHRASE=$PASS" >> .env
fi
chmod 600 .env
unset PASS PASS2

echo
echo "Saved to .env (0600). Re-provisioning the hash..."
PYTHONPATH=. ./.venv/bin/python -c "
from console.auth import bot_passphrase
print('  provisioned:', bot_passphrase.provision_from_config())"
echo
echo "Now restart the Console so it reloads:"
echo "  pkill -f console.app && sleep 2 && nohup ./.venv/bin/python -m console.app > /tmp/grocery-console.log 2>&1 &"
