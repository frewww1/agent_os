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
exports.rejectBlock = rejectBlock;
const vscode = __importStar(require("vscode"));
/**
 * Reverse-apply a diff block to the file:
 *   - Edit: replace the greenLines (new) back with the redLines (old) at the
 *     block's start line.
 *   - Write: restore snapshotBefore (or delete file if snapshot is null).
 *
 * Returns true if the rollback succeeded. The caller is responsible for
 * removing the block from the store and refreshing decorations on success.
 *
 * Strategy: open the document (may already be open), perform a single
 * `editor.edit()` with `undoStopAfter: false` so the user can Ctrl+Z to
 * undo the rollback.
 */
async function rejectBlock(block) {
    const uri = vscode.Uri.file(block.fileUri);
    let document;
    try {
        document = await vscode.workspace.openTextDocument(uri);
    }
    catch (err) {
        vscode.window.showErrorMessage(`Agent OS: cannot open ${block.fileUri} for reject — ${err}`);
        return false;
    }
    // Show the editor so the edit is visible (and so the edit applies to a
    // visible editor, which is required for editor.edit).
    const editor = await vscode.window.showTextDocument(document, {
        preview: false,
        preserveFocus: false,
    });
    if (block.kind === "edit") {
        return rejectEditBlock(editor, block);
    }
    else {
        return rejectWriteBlock(editor, block, uri);
    }
}
/**
 * Edit rollback: the block's `startLine` points at the first green (added)
 * line in the current document. The green block occupies
 * `[startLine, startLine + greenLines.length)`. We replace that range with
 * `redLines` (the original content).
 *
 * If the document no longer contains the greenLines at that location (user
 * edited the file), the block should have been marked stale upstream; here
 * we still attempt the line-range replace, which may produce a slightly
 * wrong result — but stale blocks have Reject disabled in the CodeLens, so
 * this path is only reached for non-stale blocks.
 */
async function rejectEditBlock(editor, block) {
    const doc = editor.document;
    const startLine = block.startLine;
    const endLine = startLine + block.greenLines.length; // exclusive
    // Clamp to document bounds.
    const safeEndLine = Math.min(endLine, doc.lineCount);
    const range = new vscode.Range(startLine, 0, safeEndLine, safeEndLine < doc.lineCount ? 0 : 0);
    const replacement = block.redLines.join("\n");
    const ok = await editor.edit((b) => {
        b.replace(range, replacement);
    }, { undoStopAfter: false, undoStopBefore: false });
    if (!ok) {
        vscode.window.showErrorMessage(`Agent OS: reject edit failed — edit was canceled`);
        return false;
    }
    await doc.save();
    return true;
}
/**
 * Write rollback:
 *   - snapshotBefore === string → overwrite file with the old content
 *   - snapshotBefore === null   → file was newly created, delete it
 *   - snapshotBefore === undefined → should never reach here (Reject disabled
 *     in CodeLens); bail out.
 *
 * For the restore case we replace the entire document content in one edit.
 */
async function rejectWriteBlock(editor, block, uri) {
    if (block.snapshotBefore === undefined) {
        vscode.window.showWarningMessage(`Agent OS: reject unavailable for this Write — old content was not captured`);
        return false;
    }
    if (block.snapshotBefore === null) {
        // File was new — delete it. We have to close the editor first or VS Code
        // will keep the document in a "deleted" dirty state.
        await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
        try {
            await vscode.workspace.fs.delete(uri, { useTrash: false });
        }
        catch (err) {
            vscode.window.showErrorMessage(`Agent OS: failed to delete new file on reject — ${err}`);
            return false;
        }
        return true;
    }
    // Restore old content: replace whole document.
    const doc = editor.document;
    const fullRange = new vscode.Range(0, 0, Math.max(0, doc.lineCount - 1), doc.lineAt(Math.max(0, doc.lineCount - 1)).text.length);
    const ok = await editor.edit((b) => {
        b.replace(fullRange, block.snapshotBefore);
    }, { undoStopAfter: false, undoStopBefore: false });
    if (!ok) {
        vscode.window.showErrorMessage(`Agent OS: reject write failed — edit was canceled`);
        return false;
    }
    await doc.save();
    return true;
}
//# sourceMappingURL=rejectRollback.js.map