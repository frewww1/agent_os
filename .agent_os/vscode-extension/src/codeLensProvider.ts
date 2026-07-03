import * as vscode from "vscode";
import { DiffStore } from "./diffStore";

/**
 * CodeLens provider that renders `Accept | Reject` above each pending diff
 * block. Lenses are disabled (greyed out via `title`) when the block is
 * stale or when Reject is unavailable (Write with no snapshot).
 *
 * Commands invoked: `agentOs.acceptBlock` / `agentOs.rejectBlock` with
 * arguments `{ fileUri, blockId }`.
 */
export class DiffCodeLensProvider implements vscode.CodeLensProvider {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this._onDidChange.event;

  constructor(private store: DiffStore) {
    store.onDidChange(() => this._onDidChange.fire());
  }

  provideCodeLenses(
    document: vscode.TextDocument,
    _token: vscode.CancellationToken
  ): vscode.ProviderResult<vscode.CodeLens[]> {
    const fileUri = document.uri.fsPath;
    const blocks = this.store.getBlocks(fileUri);
    if (blocks.length === 0) return [];

    const lenses: vscode.CodeLens[] = [];
    for (const block of blocks) {
      // Place the lens on the line *above* the green block start.
      // If the block starts at line 0, attach to line 0 (VS Code will render
      // above anyway since CodeLens always sits above its range).
      const line = Math.max(0, block.startLine);
      const range = new vscode.Range(line, 0, line, 0);

      const acceptTitle = block.stale
        ? "$(check) Accept (stale)"
        : "$(check) Accept";
      const rejectTitle = block.stale
        ? "$(close) Reject (stale)"
        : isRejectable(block.kind, block.snapshotBefore)
        ? "$(close) Reject"
        : "$(close) Reject (unavailable)";

      lenses.push(
        new vscode.CodeLens(range, {
          title: acceptTitle,
          command: "agentOs.acceptBlock",
          arguments: [{ fileUri, blockId: block.id }],
        })
      );
      lenses.push(
        new vscode.CodeLens(range, {
          title: rejectTitle,
          command: block.stale || !isRejectable(block.kind, block.snapshotBefore)
            ? ""
            : "agentOs.rejectBlock",
          arguments: block.stale || !isRejectable(block.kind, block.snapshotBefore)
            ? undefined
            : [{ fileUri, blockId: block.id }],
        })
      );
      lenses.push(
        new vscode.CodeLens(range, {
          title: `$(info) ${block.kind} · ${block.runId.slice(0, 8)}`,
          command: "",
        })
      );
    }
    return lenses;
  }
}

/**
 * A block is rejectable iff:
 *   - edit: always (we can reverse-patch with redLines)
 *   - write: only when snapshotBefore is string (restore) or null (delete file)
 *     — undefined means capture failed, Reject is disabled.
 */
function isRejectable(
  kind: "edit" | "write",
  snapshotBefore?: string | null
): boolean {
  if (kind === "edit") return true;
  // write
  return snapshotBefore !== undefined;
}
