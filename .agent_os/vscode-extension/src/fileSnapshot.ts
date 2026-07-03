import * as vscode from "vscode";

/**
 * Captures the on-disk content of a file *before* a Write tool executes,
 * so Reject can restore it later.
 *
 * tool_use events fire *before* the tool runs, so when we see a Write
 * tool_use we have a brief window to read the file's current content.
 *
 * snapshot value semantics:
 *   string  → file existed, this was its content
 *   null    → file did not exist (new file); Reject should delete it
 *   undefined → capture was attempted but failed (e.g. read error);
 *               Reject will be disabled for this block
 *
 * Persistence: we store snapshots in workspaceState so they survive editor
 * restart. (The snapshot is keyed by `${runId}:${seq}` which matches the
 * DiffBlock id.)
 */
export class FileSnapshotStore {
  private snapshots = new Map<string, string | null>();
  private failed = new Set<string>();
  private static STATE_KEY = "agentOs.snapshots";

  constructor(private context: vscode.ExtensionContext) {
    this.load();
  }

  private load() {
    try {
      const raw = this.context.workspaceState.get<string>(
        FileSnapshotStore.STATE_KEY
      );
      if (!raw) return;
      const obj = JSON.parse(raw) as {
        ok: Record<string, string | null>;
        failed: string[];
      };
      for (const [k, v] of Object.entries(obj.ok ?? {})) {
        this.snapshots.set(k, v);
      }
      for (const k of obj.failed ?? []) {
        this.failed.add(k);
      }
    } catch {
      // corrupt — start fresh
    }
  }

  private save() {
    const ok: Record<string, string | null> = {};
    for (const [k, v] of this.snapshots) {
      ok[k] = v;
    }
    const payload = JSON.stringify({
      ok,
      failed: Array.from(this.failed),
    });
    this.context.workspaceState.update(FileSnapshotStore.STATE_KEY, payload);
  }

  private key(runId: string, seq: number): string {
    return `${runId}:${seq}`;
  }

  /**
   * Best-effort: read the current on-disk content of `filePath` and store it
   * as the pre-Write snapshot. Must be called as soon as the Write tool_use
   * event is seen, before the tool actually executes.
   *
   * Uses workspace.fs.readFile (async). If the file doesn't exist we store
   * null (Reject = delete). On read error we mark the key as failed.
   */
  async capture(
    runId: string,
    seq: number,
    fileUri: vscode.Uri
  ): Promise<void> {
    const k = this.key(runId, seq);
    try {
      const buf = await vscode.workspace.fs.readFile(fileUri);
      const text = Buffer.from(buf).toString("utf8");
      this.snapshots.set(k, text);
    } catch (err) {
      // Distinguish "not found" (FileSystemError with code FileNotFound)
      // from genuine read errors.
      if (err instanceof vscode.FileSystemError) {
        if (err.code === "FileNotFound" || err.code === "EntryNotFound") {
          this.snapshots.set(k, null);
        } else {
          this.failed.add(k);
        }
      } else {
        this.failed.add(k);
      }
    }
    this.save();
  }

  /**
   * Get the captured snapshot for a block.
   * Returns:
   *   - string: file existed, content captured
   *   - null: file did not exist before
   *   - undefined: capture failed / never captured
   */
  get(runId: string, seq: number): string | null | undefined {
    const k = this.key(runId, seq);
    if (this.failed.has(k)) return undefined;
    return this.snapshots.get(k);
  }

  /** Clear a snapshot after its block is accepted/rejected. */
  clear(runId: string, seq: number) {
    const k = this.key(runId, seq);
    this.snapshots.delete(k);
    this.failed.delete(k);
    this.save();
  }
}
