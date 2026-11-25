# Topcoat as a tool of the assistant (prototype)

Goal: the assistant can read Topcoat itself, so it can guide a user through the app and explain every
function — beyond a single conversation, and safe for more than one user.

## What exists
Two read-only virtual tools of the hub, tool group **topcoat** (switch per conversation in the chat's
tool picker, default on; hub-wide off with `TOPCOAT_SELF_TOOL_ENABLED=false`):

| tool | evidence tag | content |
|---|---|---|
| `topcoat.guide` | doc | what Topcoat can do and where it is in the GUI (text shipped with the build, `FEATURES` in `topcoat_self.py`) |
| `topcoat.state` | live | `overview`, `chats` (own, optional title filter), `chat` (own, one conversation, shortened), `settings` (own), `nodes` (connected servers and tools this user may run; for an admin also every machine of the hub with state, memory, load, GPUs, loaded models and the real connection), `config` (hub configuration, **admin only**) |

Rules (tested in `hub/tests/test_topcoat_self.py`):
- Nothing is written. No tool input reaches a SQL string or a path.
- Every read is scoped by `user_id` (ChatStore). An admin sees only their own chats through it;
  another user's chat looks like a chat that does not exist.
- Anything that looks like a secret is blackened; config fields named token/secret/password/key and
  host paths/addresses are never listed.
- A question about Topcoat itself ("wie funktioniert Topcoat", "wo finde ich meine Einstellungen")
  starts the check without a model decision when the group is on (`chat_tools.check_need`).

## Decisions taken (defaults, change on the owner's word)
1. Read-only; changes stay a single click on one concrete action (D15).
2. Per user only the own chats; settings/config without secrets; config only for admins; other
   people's chats not even for an admin without a separate approval (not built).
3. The switch is per conversation (existing tool picker, remembered as the user's last choice), plus a hub-wide off.
4. No migration: the existing tool scope carries the switch.

## Known limits / next steps
- Offered even with no node connected (owner: Topcoat must explain itself on its own). The two tests that
  needed "no tool at all" switch the tool off hub-wide instead.
- `guide` is hand-written; keep `FEATURES` in step with the GUI (add a test that each menu entry has a topic).
- Also readable: own memory notes, knowledge sources (no paths; shared ones plus own private), own tasks.
  Still not readable: API key names, personas, inventory (the inventory tool covers it).
- Real model behaviour (does it call the tool, is the guide enough) is untested.
