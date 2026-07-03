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
exports.DiffCodeLensProvider = void 0;
const vscode = __importStar(require("vscode"));
/**
 * CodeLens provider that renders `Accept | Reject` above each pending diff
 * block. Lenses are disabled (greyed out via `title`) when the block is
 * stale or when Reject is unavailable (Write with no snapshot).
 *
 * Commands invoked: `agentOs.acceptBlock` / `agentOs.rejectBlock` with
 * arguments `{ fileUri, blockId }`.
 */
class DiffCodeLensProvider {
    constructor(store) {
        this.store = store;
        this._onDidChange = new vscode.EventEmitter();
        this.onDidChangeCodeLenses = this._onDidChange.event;
        store.onDidChange(() => this._onDidChange.fire());
    }
    provideCodeLenses(document, _token) {
        const fileUri = document.uri.fsPath;
        const blocks = this.store.getBlocks(fileUri);
        if (blocks.length === 0)
            return [];
        const lenses = [];
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
            lenses.push(new vscode.CodeLens(range, {
                title: acceptTitle,
                command: "agentOs.acceptBlock",
                arguments: [{ fileUri, blockId: block.id }],
            }));
            lenses.push(new vscode.CodeLens(range, {
                title: rejectTitle,
                command: block.stale || !isRejectable(block.kind, block.snapshotBefore)
                    ? ""
                    : "agentOs.rejectBlock",
                arguments: block.stale || !isRejectable(block.kind, block.snapshotBefore)
                    ? undefined
                    : [{ fileUri, blockId: block.id }],
            }));
            lenses.push(new vscode.CodeLens(range, {
                title: `$(info) ${block.kind} · ${block.runId.slice(0, 8)}`,
                command: "",
            }));
        }
        return lenses;
    }
}
exports.DiffCodeLensProvider = DiffCodeLensProvider;
/**
 * A block is rejectable iff:
 *   - edit: always (we can reverse-patch with redLines)
 *   - write: only when snapshotBefore is string (restore) or null (delete file)
 *     — undefined means capture failed, Reject is disabled.
 */
function isRejectable(kind, snapshotBefore) {
    if (kind === "edit")
        return true;
    // write
    return snapshotBefore !== undefined;
}
//# sourceMappingURL=codeLensProvider.js.map