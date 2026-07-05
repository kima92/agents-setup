#!/usr/bin/env bash
# Seed ~/.hevy/token.json from a browser that is logged into hevy.com.
#
# Hevy's login requires a Google reCAPTCHA, so we don't script the password
# login. Instead: log into hevy.com in the agent-browser session named "hevy",
# then run this script to lift the (readable) auth cookie into the token file.
# From then on hevy.py auto-refreshes the token and you won't need this again
# unless the refresh token is revoked (e.g. password change / long inactivity).
#
# Usage:
#   agent-browser --session hevy --headed open https://hevy.com/login   # log in by hand
#   ./seed-token.sh
set -euo pipefail

SESSION="${HEVY_SESSION:-hevy}"
DEST="${HOME}/.hevy/token.json"

read -r -d '' JS <<'EOF' || true
(() => {
  const c = document.cookie.split(';').map(s=>s.trim())
    .find(s=>s.startsWith('auth2.0-token='));
  if (!c) return JSON.stringify({error:"not logged in (no auth2.0-token cookie)"});
  const tok = JSON.parse(decodeURIComponent(c.split('=').slice(1).join('=')));
  const acct = JSON.parse(localStorage.getItem("ACCOUNT_LOCAL_STORAGE_KEY")||"{}");
  return JSON.stringify({
    access_token: tok.access_token,
    refresh_token: tok.refresh_token,
    expires_at: tok.expires_at,
    username: acct.username || null,
    user_id: acct.id || null
  });
})();
EOF

mkdir -p "${HOME}/.hevy"; chmod 700 "${HOME}/.hevy"

echo "$JS" | agent-browser --session "$SESSION" eval --stdin \
  | python3 -c '
import json,sys,os
raw=sys.stdin.read().strip()
data=json.loads(json.loads(raw)) if raw.startswith("\"") else json.loads(raw)
if data.get("error"):
    print("error:",data["error"],file=sys.stderr); sys.exit(1)
p=os.path.expanduser("~/.hevy/token.json")
json.dump(data,open(p,"w"),indent=2); os.chmod(p,0o600)
print("seeded",p,"for",data.get("username"),"(expires",data.get("expires_at")+")")
'
