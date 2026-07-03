# Agent OS Inline Diff

A VS Code extension that shows real-time inline diffs for [Agent OS](../) workspaces. When an agent edits a file, you see the changes directly in the editor as green (added) / red (removed) lines, with `Accept` / `Reject` buttons above each diff block.

- **Real-time**: subscribes to Agent OS SSE event stream; diffs appear while the agent is still running.
- **Edit events**: `Accept` clears the decoration (keeps the change); `Reject` reverse-patches the file with `old_string`.
- **Write events**: `Reject` restores the pre-Write file content (or deletes the file if the agent created it). The extension races to snapshot the file before the Write tool executes.
- **Persistent**: pending diff blocks survive file close/reopen and editor restart.
- **Pure client-side**: no changes to the Agent OS backend.

## Install (development mode)

1. Open this folder in VS Code: `code .agent_os/vscode-extension`
2. Run `npm install` then `npm run compile`.
3. Press `F5` (or **Run → Start Debugging**) to launch an **Extension Development Host** with the extension loaded.
4. In the host window, start Agent OS in your project: `python .agent_os/main.py --cli codebuddy --port 8420`.
5. Trigger a run that edits files. Open those files in the host window — you should see green/red lines with `Accept | Reject` CodeLens above each block.

## Package as VSIX (optional)

```bash
cd .agent_os/vscode-extension
npx @vscode/vsce package
# Installs the produced .vsix via:
code --install-extension agent-os-inline-diff-0.1.0.vsix
```

## Configuration

Settings (in `settings.json`):

| Key | Default | Description |
|-----|---------|-------------|
| `agentOs.host` | `127.0.0.1` | Agent OS server host |
| `agentOs.port` | `8420` | Agent OS server port |
| `agentOs.autoReconnect` | `true` | Reconnect SSE stream on drop |
| `agentOs.pollInterval` | `5000` | ms between `/api/tree` polls for new active runs |

## Commands

- `Agent OS: Accept Block` — accept the focused diff block (invoked from CodeLens)
- `Agent OS: Reject Block` — reject and roll back the focused block (invoked from CodeLens)
- `Agent OS: Accept All Changes in File` — accept all pending blocks in the active editor
- `Agent OS: Reject All Changes in File` — reject all pending blocks in the active editor
- `Agent OS: Connect` / `Agent OS: Disconnect` — manual connection control

## Status bar

- `Agent OS: (radio-tower) N active` — connected, N active runs
- `Agent OS: (loading~spin)` — connecting
- `Agent OS: (circle-slash)` — disconnected (click to retry)

## How it works

1. On activate, the extension pings `GET /api/runs` on the Agent OS server.
2. Every `pollInterval` ms it calls `GET /api/tree` and subscribes to the SSE stream (`GET /api/run/{id}/stream`) of any run with status `running` or `waiting`.
3. For each `tool_use` event of kind `Edit` or `Write`:
   - **Edit**: computes line-level diff hunks between `old_string` and `new_string`, locates the `new_string` in the current document, and registers a `DiffBlock` with green (added) and red (removed) lines.
   - **Write**: races to read the file's current on-disk content as the pre-Write snapshot, then registers a `DiffBlock` covering the entire new content as green lines.
4. `AddedLineDecorationManager` / `RemovedLineDecorationManager` (modelled after [Continue.dev](https://github.com/continuedev/continue)'s vertical diff) render the green/red decorations.
5. A `CodeLensProvider` renders `Accept | Reject` above each block.
6. **Accept**: removes the block + clears decorations (file content untouched).
7. **Reject**: for Edit, replaces the green block with the original red lines via a single `editor.edit`; for Write, restores `snapshotBefore` (or deletes the file if it was new).

## Known limitations (MVP)

- If the user manually edits a file while an agent diff is pending, the block's `old_string` may no longer match — the block is marked **stale** (greyed out, Accept/Reject disabled).
- Snapshot capture for Write is best-effort: if the Write tool executes before the snapshot read completes, Reject is disabled for that block (the CodeLens shows `Reject (unavailable)`).
- No agent-side awareness of reject: the agent will continue operating on the post-Write file even if the user rejects a change. Subsequent edits may land on stale assumptions.
- Diff hunks use a simple O(n·m) LCS — capped to 200k cells, falling back to a single whole-file hunk for very large edits.
