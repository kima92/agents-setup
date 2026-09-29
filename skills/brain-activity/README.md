# brain-activity

Agent skill that drives Chika Brain conversations (activities) through the
Chikabase REST API — start a session and watch the agent answer, or read an
existing conversation, its messages, and its artifacts back by id.

- **Zero setup inside Chika Brain sessions**: auth and workspace come from the
  injected `CHIKABASE_BASE_URL` / `CHIKABASE_USER_ACCESS_KEY` /
  `CHIKABASE_WORKSPACE_ID` env vars.
- **Standalone too**: with no access key, it logs in via `CHIKABASE_USER_EMAIL`
  + `CHIKABASE_USER_PASS` and adopts your default workspace.
- **Monitors the agent**: `chat` sends a message and polls until the assistant
  reply stops streaming, logging each status change and the files/searches/APIs
  the agent touches, then prints the reply and any generated artifacts.
- **Audits answers**: `messages --logs` attaches each reply's agent execution
  trace (tool calls, inputs, results) so you can see where a claim came from.

See [SKILL.md](SKILL.md) for the full command reference. The CLI is a single
stdlib-only Python script at `scripts/brain_activity.py`.

```bash
python3 scripts/brain_activity.py chat --preset qa-analyst --full \
  --message "What are the most common withdrawal support questions?"
```
