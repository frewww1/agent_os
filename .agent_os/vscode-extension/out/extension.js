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
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const agentOsClient_1 = require("./agentOsClient");
const diffStore_1 = require("./diffStore");
const fileSnapshot_1 = require("./fileSnapshot");
const decorations_1 = require("./decorations");
const codeLensProvider_1 = require("./codeLensProvider");
const rejectRollback_1 = require("./rejectRollback");
const utils_1 = require("./utils");
let client;
let store;
let snapshots;
let statusBarItem;
const editorDecos = new Map();
/** Track which runs we're currently subscribed to, to avoid double-subscribe. */
const subscribedRunIds = new Set();
function activate(context) {
    store = new diffStore_1.DiffStore(context);
    snapshots = new fileSnapshot_1.FileSnapshotStore(context);
    // Status bar
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    statusBarItem.command = "agentOs.connect";
    statusBarItem.text = "Agent OS: $(circle-slash)";
    statusBarItem.tooltip = "Agent OS Inline Diff — click to connect";
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);
    // CodeLens provider
    const lensProvider = new codeLensProvider_1.DiffCodeLensProvider(store);
    context.subscriptions.push(vscode.languages.registerCodeLensProvider({ scheme: "file" }, lensProvider));
    // Commands
    context.subscriptions.push(vscode.commands.registerCommand("agentOs.acceptBlock", async (args) => {
        if (!args || !store)
            return;
        handleAccept(args.fileUri, args.blockId);
    }), vscode.commands.registerCommand("agentOs.rejectBlock", async (args) => {
        if (!args || !store || !snapshots)
            return;
        await handleReject(args.fileUri, args.blockId);
    }), vscode.commands.registerCommand("agentOs.acceptAll", () => handleAcceptAll()), vscode.commands.registerCommand("agentOs.rejectAll", () => handleRejectAll()), vscode.commands.registerCommand("agentOs.connect", () => connect(context)), vscode.commands.registerCommand("agentOs.disconnect", () => disconnect()));
    // Re-render decorations when an editor becomes visible (file open / tab switch).
    context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor((editor) => {
        if (editor)
            refreshDecorationsForEditor(editor);
    }), vscode.window.onDidChangeVisibleTextEditors((editors) => {
        for (const e of editors)
            refreshDecorationsForEditor(e);
    }), vscode.workspace.onDidOpenTextDocument((doc) => {
        // If this doc has pending blocks, refresh any visible editor showing it.
        if (store && store.getBlocks(doc.uri.fsPath).length > 0) {
            for (const editor of vscode.window.visibleTextEditors) {
                if ((0, utils_1.sameFile)(editor.document.uri.fsPath, doc.uri.fsPath)) {
                    refreshDecorationsForEditor(editor);
                }
            }
        }
    }));
    // Initial decorations for any already-open editors
    for (const editor of vscode.window.visibleTextEditors) {
        refreshDecorationsForEditor(editor);
    }
    // Auto-connect on activate
    connect(context);
}
function deactivate() {
    disconnect();
}
// ---------------------------------------------------------------------------
// Connection management
// ---------------------------------------------------------------------------
function connect(context) {
    disconnect();
    const cfg = vscode.workspace.getConfiguration("agentOs");
    const host = (cfg.get("host") ?? "127.0.0.1");
    const port = cfg.get("port") ?? 8420;
    const autoReconnect = cfg.get("autoReconnect") ?? true;
    const pollInterval = cfg.get("pollInterval") ?? 5000;
    client = new agentOsClient_1.AgentOsClient(host, port, autoReconnect);
    setStatus("connecting");
    client
        .ping()
        .then((ok) => {
        if (!ok) {
            setStatus("disconnected");
            scheduleReconnect(context);
            return;
        }
        setStatus("connected", 0);
        client.startPolling(pollInterval, (activeRuns) => {
            onActiveRunsChanged(activeRuns);
        });
    })
        .catch(() => {
        setStatus("disconnected");
        scheduleReconnect(context);
    });
}
let reconnectTimer;
function scheduleReconnect(context) {
    if (reconnectTimer)
        return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = undefined;
        connect(context);
    }, 10000);
}
function disconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
    }
    if (client) {
        client.disconnect();
        client = undefined;
    }
    subscribedRunIds.clear();
    setStatus("disconnected");
}
function setStatus(state, activeCount) {
    if (!statusBarItem)
        return;
    if (state === "connected") {
        const n = activeCount ?? 0;
        statusBarItem.text = `Agent OS: $(radio-tower) ${n} active`;
        statusBarItem.tooltip = `Agent OS Inline Diff — connected (${n} active runs). Click to reconnect.`;
    }
    else if (state === "connecting") {
        statusBarItem.text = `Agent OS: $(loading~spin)`;
        statusBarItem.tooltip = "Agent OS Inline Diff — connecting…";
    }
    else {
        statusBarItem.text = `Agent OS: $(circle-slash)`;
        statusBarItem.tooltip = "Agent OS Inline Diff — disconnected. Click to connect.";
    }
}
// ---------------------------------------------------------------------------
// Run subscription lifecycle
// ---------------------------------------------------------------------------
function onActiveRunsChanged(activeRuns) {
    if (!client)
        return;
    const activeIds = new Set(activeRuns.map((r) => r.run_id));
    // Subscribe to new active runs
    for (const run of activeRuns) {
        if (subscribedRunIds.has(run.run_id))
            continue;
        subscribedRunIds.add(run.run_id);
        client.subscribeRun(run.run_id, (ev) => onRunEvent(run, ev), (_status) => {
            // Run terminated — drop subscription. Blocks remain in the store so
            // the user can still accept/reject after the run ends.
            subscribedRunIds.delete(run.run_id);
            if (client)
                client.unsubscribeRun(run.run_id);
        });
    }
    // Unsubscribe from runs that are no longer active
    for (const id of Array.from(subscribedRunIds)) {
        if (!activeIds.has(id)) {
            subscribedRunIds.delete(id);
            if (client)
                client.unsubscribeRun(id);
        }
    }
    setStatus("connected", activeRuns.length);
}
// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------
async function onRunEvent(run, ev) {
    if (!store || !snapshots)
        return;
    if (ev.kind !== "tool_use")
        return;
    if (ev.tool !== "Edit" && ev.tool !== "Write")
        return;
    if (!ev.file_path)
        return;
    const filePath = (0, utils_1.resolveFilePath)(ev.file_path, run.workspace_path);
    const fileUri = vscode.Uri.file(filePath).fsPath;
    if (ev.tool === "Edit") {
        await handleEditEvent(run, ev, fileUri);
    }
    else if (ev.tool === "Write") {
        await handleWriteEvent(run, ev, fileUri);
    }
}
async function handleEditEvent(run, ev, fileUri) {
    if (!store)
        return;
    const oldStr = ev.old_string ?? "";
    const newStr = ev.new_string ?? "";
    // Try to open the document so we can locate where old_string was replaced
    // by new_string. The tool has already executed by the time we get here in
    // many cases (SSE is near-real-time), so the file on disk should contain
    // newStr. We use findStartLine(newStr) to locate the green block's start.
    let doc;
    try {
        doc = await vscode.workspace.openTextDocument(vscode.Uri.file(fileUri));
    }
    catch {
        // File gone — nothing to decorate.
        return;
    }
    const hunks = (0, utils_1.computeDiffHunks)(oldStr, newStr);
    if (hunks.length === 0)
        return;
    // Locate where newStr currently lives in the document.
    const startLine = (0, utils_1.findStartLine)(doc, newStr);
    if (startLine === null) {
        // The new_string isn't in the file anymore — user probably edited it.
        // Add a stale block so the user is aware (Accept disabled, Reject disabled).
        const hunk = hunks[0];
        const block = store.addBlock(fileUri, {
            runId: run.run_id,
            seq: ev.seq,
            kind: "edit",
            fileUri,
            startLine: hunk.newStartLine,
            greenLines: hunk.greenLines,
            redLines: hunk.redLines,
        });
        store.markStale(fileUri, block.id);
        return;
    }
    // For each hunk, compute its absolute start line in the document. The hunk's
    // newStartLine is relative to newStr (0-based within newStr), so we add the
    // document start line.
    let lineCursor = startLine;
    for (const hunk of hunks) {
        // Find where this hunk's greenLines start within newStr by counting
        // preceding lines up to hunk.newStartLine.
        const absStart = startLine + hunk.newStartLine;
        const block = store.addBlock(fileUri, {
            runId: run.run_id,
            seq: ev.seq,
            kind: "edit",
            fileUri,
            startLine: absStart,
            greenLines: hunk.greenLines,
            redLines: hunk.redLines,
        });
        // Shift later blocks (added by previous edits in the same file) that are
        // below this hunk. Net line delta = greenLines.length - redLines.length.
        const delta = hunk.greenLines.length - hunk.redLines.length;
        if (delta !== 0) {
            store.shiftBlocksAfterLine(fileUri, absStart + hunk.greenLines.length, delta);
        }
        lineCursor = absStart + hunk.greenLines.length;
        void block;
    }
    refreshDecorationsForFile(fileUri);
}
async function handleWriteEvent(run, ev, fileUri) {
    if (!store || !snapshots)
        return;
    const content = ev.content ?? "";
    // Capture the pre-Write snapshot. tool_use fires *before* the tool executes,
    // so the file on disk should still be in its pre-Write state right now.
    // We must race to read it before the CLI's Write tool lands.
    await snapshots.capture(run.run_id, ev.seq, vscode.Uri.file(fileUri));
    const snapshot = snapshots.get(run.run_id, ev.seq);
    // Open the (now possibly post-Write) document. We decorate from line 0
    // because Write replaces the whole file — green block = entire new content.
    let doc;
    try {
        doc = await vscode.workspace.openTextDocument(vscode.Uri.file(fileUri));
    }
    catch {
        return;
    }
    // If the snapshot we captured equals the current content, the Write
    // hasn't landed yet (or it was a no-op). We still add the block; the
    // greenLines come from ev.content.
    const startLine = 0;
    store.addWriteBlock(fileUri, run.run_id, ev.seq, content, startLine, snapshot);
    void doc;
    refreshDecorationsForFile(fileUri);
}
// ---------------------------------------------------------------------------
// Accept / Reject handlers
// ---------------------------------------------------------------------------
function handleAccept(fileUri, blockId) {
    if (!store)
        return;
    const block = store.getBlock(fileUri, blockId);
    if (!block)
        return;
    store.removeBlock(fileUri, blockId);
    if (snapshots)
        snapshots.clear(block.runId, block.seq);
    refreshDecorationsForFile(fileUri);
}
async function handleReject(fileUri, blockId) {
    if (!store || !snapshots)
        return;
    const block = store.getBlock(fileUri, blockId);
    if (!block)
        return;
    if (block.stale) {
        vscode.window.showWarningMessage("Agent OS: this block is stale and cannot be rejected");
        return;
    }
    const ok = await (0, rejectRollback_1.rejectBlock)(block);
    if (!ok)
        return;
    store.removeBlock(fileUri, blockId);
    if (snapshots)
        snapshots.clear(block.runId, block.seq);
    refreshDecorationsForFile(fileUri);
}
async function handleAcceptAll() {
    if (!store || !snapshots)
        return;
    const editor = vscode.window.activeTextEditor;
    if (!editor)
        return;
    const fileUri = editor.document.uri.fsPath;
    const blocks = store.getBlocks(fileUri);
    for (const b of blocks) {
        if (snapshots)
            snapshots.clear(b.runId, b.seq);
    }
    store.removeAllBlocks(fileUri);
    refreshDecorationsForFile(fileUri);
}
async function handleRejectAll() {
    if (!store || !snapshots)
        return;
    const editor = vscode.window.activeTextEditor;
    if (!editor)
        return;
    const fileUri = editor.document.uri.fsPath;
    const blocks = store.getBlocks(fileUri);
    // Reject in reverse order (bottom-up) so earlier line numbers stay valid.
    for (let i = blocks.length - 1; i >= 0; i--) {
        const b = blocks[i];
        if (b.stale)
            continue;
        const ok = await (0, rejectRollback_1.rejectBlock)(b);
        if (!ok)
            continue;
        store.removeBlock(fileUri, b.id);
        if (snapshots)
            snapshots.clear(b.runId, b.seq);
    }
    refreshDecorationsForFile(fileUri);
}
// ---------------------------------------------------------------------------
// Decoration refresh
// ---------------------------------------------------------------------------
function refreshDecorationsForFile(fileUri) {
    for (const editor of vscode.window.visibleTextEditors) {
        if ((0, utils_1.sameFile)(editor.document.uri.fsPath, fileUri)) {
            refreshDecorationsForEditor(editor);
        }
    }
}
function refreshDecorationsForEditor(editor) {
    if (!store)
        return;
    const fileUri = editor.document.uri.fsPath;
    const blocks = store.getBlocks(fileUri);
    let decos = editorDecos.get(fileUri);
    if (!decos) {
        decos = {
            added: new decorations_1.AddedLineDecorationManager(editor),
            removed: new decorations_1.RemovedLineDecorationManager(editor),
        };
        editorDecos.set(fileUri, decos);
    }
    else {
        decos.added.applyToNewEditor(editor);
        decos.removed.applyToNewEditor(editor);
    }
    if (blocks.length === 0) {
        decos.added.clear();
        decos.removed.clear();
        return;
    }
    // Rebuild decoration ranges from the current block list.
    decos.added.clear();
    decos.removed.clear();
    for (const block of blocks) {
        // Green (added) lines: block.startLine .. startLine + greenLines.length
        if (block.greenLines.length > 0) {
            decos.added.addLines(block.startLine, block.greenLines.length);
        }
        // Red (removed) lines: sit at the same start line, ghost-text style.
        // For an Edit, the red lines logically precede the green block; we place
        // them at block.startLine (they'll render as hidden ghost text with the
        // old content shown via `after`). For a Write there are no red lines.
        if (block.redLines.length > 0) {
            decos.removed.addLines(block.startLine, block.redLines);
        }
    }
}
//# sourceMappingURL=extension.js.map