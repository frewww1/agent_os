import * as vscode from "vscode";
import { DiffHunk } from "./utils";

export type BlockKind = "edit" | "write";

export interface DiffBlock {
  /** Stable id for this block (runId + ":" + seq). */
  id: string;
  runId: string;
  /** SSE seq of the tool_use event that produced this block. */
  seq: number;
  kind: BlockKind;
  /** File URI (fsPath form) this block belongs to. */
  fileUri: string;
  /** 0-based line where the green (added) portion starts in the current document. */
  startLine: number;
  /** Lines added (green). */
  greenLines: string[];
  /** Lines removed (red). Empty for Write. */
  redLines: string[];
  /**
   * For Write blocks: the file content captured *before* the tool executed.
   * null = file did not exist before (new file). undefined = snapshot failed
   * (Reject will be disabled).
   */
  snapshotBefore?: string | null;
  /** True if the block's old_string can no longer be located in the file. */
  stale: boolean;
}

/**
 * Per-file decoration state needed to re-render after close/reopen.
 * We persist this so reopening a file restores the decorations.
 */
interface PersistedBlock {
  id: string;
  runId: string;
  seq: number;
  kind: BlockKind;
  fileUri: string;
  startLine: number;
  greenLines: string[];
  redLines: string[];
  snapshotBefore?: string | null;
  stale: boolean;
}

const STATE_KEY = "agentOs.diffBlocks";

/**
 * Global pending-diff state. Maps fileUri → DiffBlock[].
 *
 * Notifies listeners (CodeLensProvider, decoration refresh) via
 * onDidChange. Persists to workspace.state so decorations survive
 * file close/reopen and editor restart.
 */
export class DiffStore {
  private blocks = new Map<string, DiffBlock[]>();
  private _onDidChange = new vscode.EventEmitter<string | undefined>();
  readonly onDidChange = this._onDidChange.event;

  constructor(private context: vscode.ExtensionContext) {
    this.load();
  }

  private load() {
    try {
      const raw = this.context.workspaceState.get<string>(STATE_KEY);
      if (!raw) return;
      const persisted = JSON.parse(raw) as PersistedBlock[];
      for (const p of persisted) {
        const existing = this.blocks.get(p.fileUri) ?? [];
        existing.push(this.fromPersisted(p));
        this.blocks.set(p.fileUri, existing);
      }
    } catch {
      // Corrupt state — start fresh.
    }
  }

  private save() {
    const all: PersistedBlock[] = [];
    for (const list of this.blocks.values()) {
      for (const b of list) {
        all.push(this.toPersisted(b));
      }
    }
    this.context.workspaceState.update(STATE_KEY, JSON.stringify(all));
  }

  private toPersisted(b: DiffBlock): PersistedBlock {
    return {
      id: b.id,
      runId: b.runId,
      seq: b.seq,
      kind: b.kind,
      fileUri: b.fileUri,
      startLine: b.startLine,
      greenLines: b.greenLines,
      redLines: b.redLines,
      snapshotBefore: b.snapshotBefore,
      stale: b.stale,
    };
  }

  private fromPersisted(p: PersistedBlock): DiffBlock {
    return {
      id: p.id,
      runId: p.runId,
      seq: p.seq,
      kind: p.kind,
      fileUri: p.fileUri,
      startLine: p.startLine,
      greenLines: p.greenLines,
      redLines: p.redLines,
      snapshotBefore: p.snapshotBefore,
      stale: p.stale,
    };
  }

  /**
   * Add a new diff block for a file. The `startLine` is the line in the
   * *current* document where the green portion begins. Caller is responsible
   * for translating hunks to current line numbers before calling.
   *
   * Returns the created block.
   */
  addBlock(
    fileUri: string,
    block: Omit<DiffBlock, "id" | "stale">
  ): DiffBlock {
    const list = this.blocks.get(fileUri) ?? [];
    const full: DiffBlock = {
      ...block,
      id: `${block.runId}:${block.seq}`,
      stale: false,
    };
    list.push(full);
    this.blocks.set(fileUri, list);
    this.save();
    this._onDidChange.fire(fileUri);
    return full;
  }

  /**
   * Append a hunk (from an Edit event) to an existing file's block list,
   * shifting later blocks as needed. `hunk.newStartLine` is relative to the
   * document state *after* this hunk's edit was applied — callers should pass
   * the line in the current on-disk document where the green lines now live.
   */
  addEditHunk(
    fileUri: string,
    runId: string,
    seq: number,
    hunk: DiffHunk,
    startLineInDoc: number
  ): DiffBlock {
    return this.addBlock(fileUri, {
      runId,
      seq,
      kind: "edit",
      fileUri,
      startLine: startLineInDoc,
      greenLines: hunk.greenLines,
      redLines: hunk.redLines,
    });
  }

  /**
   * Add a Write block. The entire written content is "green" (new). Red is
   * empty because we don't show removed lines for Write — instead Reject
   * restores from snapshotBefore.
   */
  addWriteBlock(
    fileUri: string,
    runId: string,
    seq: number,
    content: string,
    startLineInDoc: number,
    snapshotBefore?: string | null
  ): DiffBlock {
    const greenLines = content.length === 0 ? [] : content.split(/\r?\n/);
    return this.addBlock(fileUri, {
      runId,
      seq,
      kind: "write",
      fileUri,
      startLine: startLineInDoc,
      greenLines,
      redLines: [],
      snapshotBefore,
    });
  }

  getBlocks(fileUri: string): DiffBlock[] {
    return this.blocks.get(fileUri) ?? [];
  }

  getBlock(fileUri: string, blockId: string): DiffBlock | undefined {
    return this.blocks.get(fileUri)?.find((b) => b.id === blockId);
  }

  /**
   * Remove a single block by id. Returns the removed block or undefined.
   */
  removeBlock(fileUri: string, blockId: string): DiffBlock | undefined {
    const list = this.blocks.get(fileUri);
    if (!list) return undefined;
    const idx = list.findIndex((b) => b.id === blockId);
    if (idx < 0) return undefined;
    const [removed] = list.splice(idx, 1);
    if (list.length === 0) {
      this.blocks.delete(fileUri);
    }
    this.save();
    this._onDidChange.fire(fileUri);
    return removed;
  }

  /**
   * Remove all blocks for a file. Returns the removed blocks (for reject-all
   * to iterate in reverse order).
   */
  removeAllBlocks(fileUri: string): DiffBlock[] {
    const list = this.blocks.get(fileUri);
    if (!list) return [];
    this.blocks.delete(fileUri);
    this.save();
    this._onDidChange.fire(fileUri);
    return list;
  }

  /**
   * Shift all blocks at or below `afterLine` by `offset` in the given file.
   * Used when a new edit inserts/deletes lines and existing blocks need their
   * startLine updated.
   */
  shiftBlocksAfterLine(fileUri: string, afterLine: number, offset: number) {
    if (offset === 0) return;
    const list = this.blocks.get(fileUri);
    if (!list) return;
    for (const b of list) {
      if (b.startLine >= afterLine) {
        b.startLine = Math.max(0, b.startLine + offset);
      }
    }
    this.save();
    this._onDidChange.fire(fileUri);
  }

  /**
   * Mark a block stale (e.g. old_string no longer matches the file). Stale
   * blocks render greyed-out and have disabled Accept/Reject.
   */
  markStale(fileUri: string, blockId: string) {
    const list = this.blocks.get(fileUri);
    if (!list) return;
    const b = list.find((x) => x.id === blockId);
    if (b && !b.stale) {
      b.stale = true;
      this.save();
      this._onDidChange.fire(fileUri);
    }
  }

  /** All file URIs that currently have pending blocks. */
  filesWithBlocks(): string[] {
    return Array.from(this.blocks.keys());
  }

  /** Clear everything (used on disconnect). */
  clearAll() {
    this.blocks.clear();
    this.save();
    this._onDidChange.fire(undefined);
  }
}
