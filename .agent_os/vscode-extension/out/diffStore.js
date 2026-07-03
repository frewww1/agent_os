"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.DiffStore = void 0;
const vscode = __importStar(require("vscode"));
const STATE_KEY = "agentOs.diffBlocks";
/**
 * Global pending-diff state. Maps fileUri → DiffBlock[].
 *
 * Notifies listeners (CodeLensProvider, decoration refresh) via
 * onDidChange. Persists to workspace.state so decorations survive
 * file close/reopen and editor restart.
 */
class DiffStore {
    constructor(context) {
        this.context = context;
        this.blocks = new Map();
        this._onDidChange = new vscode.EventEmitter();
        this.onDidChange = this._onDidChange.event;
        this.load();
    }
    load() {
        try {
            const raw = this.context.workspaceState.get(STATE_KEY);
            if (!raw)
                return;
            const persisted = JSON.parse(raw);
            for (const p of persisted) {
                const existing = this.blocks.get(p.fileUri) ?? [];
                existing.push(this.fromPersisted(p));
                this.blocks.set(p.fileUri, existing);
            }
        }
        catch {
            // Corrupt state — start fresh.
        }
    }
    save() {
        const all = [];
        for (const list of this.blocks.values()) {
            for (const b of list) {
                all.push(this.toPersisted(b));
            }
        }
        this.context.workspaceState.update(STATE_KEY, JSON.stringify(all));
    }
    toPersisted(b) {
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
    fromPersisted(p) {
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
    addBlock(fileUri, block) {
        const list = this.blocks.get(fileUri) ?? [];
        const full = {
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
    addEditHunk(fileUri, runId, seq, hunk, startLineInDoc) {
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
    addWriteBlock(fileUri, runId, seq, content, startLineInDoc, snapshotBefore) {
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
    getBlocks(fileUri) {
        return this.blocks.get(fileUri) ?? [];
    }
    getBlock(fileUri, blockId) {
        return this.blocks.get(fileUri)?.find((b) => b.id === blockId);
    }
    /**
     * Remove a single block by id. Returns the removed block or undefined.
     */
    removeBlock(fileUri, blockId) {
        const list = this.blocks.get(fileUri);
        if (!list)
            return undefined;
        const idx = list.findIndex((b) => b.id === blockId);
        if (idx < 0)
            return undefined;
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
    removeAllBlocks(fileUri) {
        const list = this.blocks.get(fileUri);
        if (!list)
            return [];
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
    shiftBlocksAfterLine(fileUri, afterLine, offset) {
        if (offset === 0)
            return;
        const list = this.blocks.get(fileUri);
        if (!list)
            return;
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
    markStale(fileUri, blockId) {
        const list = this.blocks.get(fileUri);
        if (!list)
            return;
        const b = list.find((x) => x.id === blockId);
        if (b && !b.stale) {
            b.stale = true;
            this.save();
            this._onDidChange.fire(fileUri);
        }
    }
    /** All file URIs that currently have pending blocks. */
    filesWithBlocks() {
        return Array.from(this.blocks.keys());
    }
    /** Clear everything (used on disconnect). */
    clearAll() {
        this.blocks.clear();
        this.save();
        this._onDidChange.fire(undefined);
    }
}
exports.DiffStore = DiffStore;
//# sourceMappingURL=diffStore.js.map