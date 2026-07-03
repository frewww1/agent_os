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
exports.FileSnapshotStore = void 0;
const vscode = __importStar(require("vscode"));
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
class FileSnapshotStore {
    constructor(context) {
        this.context = context;
        this.snapshots = new Map();
        this.failed = new Set();
        this.load();
    }
    load() {
        try {
            const raw = this.context.workspaceState.get(FileSnapshotStore.STATE_KEY);
            if (!raw)
                return;
            const obj = JSON.parse(raw);
            for (const [k, v] of Object.entries(obj.ok ?? {})) {
                this.snapshots.set(k, v);
            }
            for (const k of obj.failed ?? []) {
                this.failed.add(k);
            }
        }
        catch {
            // corrupt — start fresh
        }
    }
    save() {
        const ok = {};
        for (const [k, v] of this.snapshots) {
            ok[k] = v;
        }
        const payload = JSON.stringify({
            ok,
            failed: Array.from(this.failed),
        });
        this.context.workspaceState.update(FileSnapshotStore.STATE_KEY, payload);
    }
    key(runId, seq) {
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
    async capture(runId, seq, fileUri) {
        const k = this.key(runId, seq);
        try {
            const buf = await vscode.workspace.fs.readFile(fileUri);
            const text = Buffer.from(buf).toString("utf8");
            this.snapshots.set(k, text);
        }
        catch (err) {
            // Distinguish "not found" (FileSystemError with code FileNotFound)
            // from genuine read errors.
            if (err instanceof vscode.FileSystemError) {
                if (err.code === "FileNotFound" || err.code === "EntryNotFound") {
                    this.snapshots.set(k, null);
                }
                else {
                    this.failed.add(k);
                }
            }
            else {
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
    get(runId, seq) {
        const k = this.key(runId, seq);
        if (this.failed.has(k))
            return undefined;
        return this.snapshots.get(k);
    }
    /** Clear a snapshot after its block is accepted/rejected. */
    clear(runId, seq) {
        const k = this.key(runId, seq);
        this.snapshots.delete(k);
        this.failed.delete(k);
        this.save();
    }
}
exports.FileSnapshotStore = FileSnapshotStore;
FileSnapshotStore.STATE_KEY = "agentOs.snapshots";
//# sourceMappingURL=fileSnapshot.js.map