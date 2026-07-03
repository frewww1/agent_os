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
exports.AgentOsClient = void 0;
const http = __importStar(require("http"));
/**
 * HTTP + SSE client for the Agent OS backend.
 *
 * Uses Node's http module directly (no external deps) because the VS Code
 * extension host doesn't ship a global EventSource.
 */
class AgentOsClient {
    constructor(host, port, autoReconnect) {
        this.host = host;
        this.port = port;
        this.autoReconnect = autoReconnect;
        this.subscribedRuns = new Map();
        /** Per-run max seq seen, for dedup (SSE has no cursor — it replays from 0). */
        this.maxSeq = new Map();
        this.reconnectTimers = new Map();
        this.connected = false;
        this.reconnectAttempts = 0;
        this.baseUrl = `http://${host}:${port}`;
    }
    /** Test connectivity by hitting /api/runs. Resolves true if reachable. */
    async ping() {
        try {
            await this.getJson("/api/runs");
            return true;
        }
        catch {
            return false;
        }
    }
    /** Generic JSON GET. */
    getJson(path) {
        return new Promise((resolve, reject) => {
            const req = http.get(`${this.baseUrl}${path}`, { timeout: 5000 }, (res) => {
                if (res.statusCode !== 200) {
                    res.resume();
                    reject(new Error(`HTTP ${res.statusCode}`));
                    return;
                }
                const chunks = [];
                res.on("data", (c) => chunks.push(c));
                res.on("end", () => {
                    try {
                        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
                    }
                    catch (e) {
                        reject(e);
                    }
                });
            });
            req.on("error", reject);
            req.on("timeout", () => {
                req.destroy();
                reject(new Error("timeout"));
            });
        });
    }
    /**
     * Fetch /api/tree and flatten to a list of runs (root + all descendants).
     */
    async listAllRuns() {
        const tree = await this.getJson("/api/tree");
        const out = [];
        const walk = (node) => {
            if (!node)
                return;
            out.push({
                run_id: node.run_id,
                status: node.status,
                workspace_path: node.workspace_path,
                prompt: node.prompt,
                agent_name: node.agent_name,
                model: node.model,
                parent_run_id: node.parent_run_id ?? null,
                children_run_ids: node.children_run_ids ?? [],
            });
            for (const child of node.children ?? []) {
                walk(child);
            }
        };
        if (Array.isArray(tree)) {
            for (const root of tree)
                walk(root);
        }
        else if (tree) {
            walk(tree);
        }
        return out;
    }
    /**
     * Return runs with status running or waiting — these are the ones whose
     * SSE streams are worth subscribing to.
     */
    async listActiveRuns() {
        const all = await this.listAllRuns();
        return all.filter((r) => r.status === "running" || r.status === "waiting");
    }
    /**
     * Subscribe to a run's SSE stream. Calls onEvent for each event (deduped
     * by seq), onDone when the stream closes (status terminal).
     *
     * Reconnects with exponential backoff if autoReconnect is true and the
     * stream drops without a clean `event: done` whose data is a terminal
     * status (completed/failed/stopped).
     */
    subscribeRun(runId, onEvent, onDone) {
        this.connectStream(runId, onEvent, onDone);
    }
    connectStream(runId, onEvent, onDone) {
        // Abort any existing request for this run.
        const existing = this.subscribedRuns.get(runId);
        if (existing) {
            existing.destroy();
        }
        const url = `${this.baseUrl}/api/run/${runId}/stream`;
        const req = http.get(url, { timeout: 0 }, (res) => {
            if (res.statusCode !== 200) {
                res.resume();
                this.handleDrop(runId, onEvent, onDone, `HTTP ${res.statusCode}`);
                return;
            }
            this.connected = true;
            this.reconnectAttempts = 0;
            let buffer = "";
            let currentEvent = "message";
            res.setEncoding("utf8");
            res.on("data", (chunk) => {
                buffer += chunk;
                const lines = buffer.split("\n");
                // Keep the last partial line in the buffer.
                buffer = lines.pop() ?? "";
                for (const raw of lines) {
                    const line = raw.replace(/\r$/, "");
                    if (line === "") {
                        // Empty line = event dispatch boundary (SSE spec)
                        continue;
                    }
                    if (line.startsWith(":")) {
                        // comment / heartbeat
                        continue;
                    }
                    if (line.startsWith("event:")) {
                        currentEvent = line.slice(6).trim();
                        continue;
                    }
                    if (line.startsWith("data:")) {
                        const data = line.slice(5).trim();
                        if (currentEvent === "done") {
                            // Stream is closing. data is the terminal status.
                            const status = data.replace(/^"|"$/g, "");
                            this.cleanupRun(runId);
                            // Only treat as terminal if status is really done.
                            // waiting means the run will resume — we should resubscribe.
                            if (status === "waiting") {
                                this.scheduleReconnect(runId, onEvent, onDone);
                            }
                            else {
                                onDone(status);
                            }
                            return;
                        }
                        // default event: a JSON event payload
                        try {
                            const ev = JSON.parse(data);
                            if (typeof ev.seq === "number") {
                                const seen = this.maxSeq.get(runId) ?? 0;
                                if (ev.seq <= seen) {
                                    // duplicate from replay — skip
                                    continue;
                                }
                                this.maxSeq.set(runId, ev.seq);
                            }
                            onEvent(ev);
                        }
                        catch {
                            // Not JSON — ignore (could be a plain-text status line).
                        }
                        currentEvent = "message";
                    }
                }
            });
            res.on("end", () => {
                // Stream ended without an explicit `event: done` — treat as drop.
                this.handleDrop(runId, onEvent, onDone, "stream ended");
            });
            res.on("error", (err) => {
                this.handleDrop(runId, onEvent, onDone, err.message);
            });
        });
        req.on("error", (err) => {
            this.handleDrop(runId, onEvent, onDone, err.message);
        });
        this.subscribedRuns.set(runId, req);
    }
    handleDrop(runId, onEvent, onDone, reason) {
        this.cleanupRun(runId);
        if (!this.autoReconnect) {
            return;
        }
        // Verify the run is still active before resubscribing — if not, treat as done.
        this.getJson(`/api/run/${runId}`)
            .then((info) => {
            const status = info?.status ?? info?.run?.status;
            if (status &&
                status !== "running" &&
                status !== "waiting") {
                onDone(status);
            }
            else {
                this.scheduleReconnect(runId, onEvent, onDone);
            }
        })
            .catch(() => {
            // Backend unreachable — retry with backoff.
            this.scheduleReconnect(runId, onEvent, onDone);
        });
        void reason;
    }
    scheduleReconnect(runId, onEvent, onDone) {
        if (this.reconnectTimers.has(runId))
            return;
        this.reconnectAttempts = Math.min(this.reconnectAttempts + 1, 6);
        const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30000);
        const t = setTimeout(() => {
            this.reconnectTimers.delete(runId);
            this.connectStream(runId, onEvent, onDone);
        }, delay);
        this.reconnectTimers.set(runId, t);
    }
    cleanupRun(runId) {
        const req = this.subscribedRuns.get(runId);
        if (req) {
            req.destroy();
            this.subscribedRuns.delete(runId);
        }
    }
    /** Unsubscribe from a specific run. */
    unsubscribeRun(runId) {
        const t = this.reconnectTimers.get(runId);
        if (t) {
            clearTimeout(t);
            this.reconnectTimers.delete(runId);
        }
        this.cleanupRun(runId);
        this.maxSeq.delete(runId);
    }
    /** Disconnect everything. */
    disconnect() {
        for (const runId of this.subscribedRuns.keys()) {
            this.cleanupRun(runId);
        }
        for (const t of this.reconnectTimers.values()) {
            clearTimeout(t);
        }
        this.reconnectTimers.clear();
        this.maxSeq.clear();
        this.connected = false;
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = undefined;
        }
    }
    isConnected() {
        return this.connected;
    }
    /**
     * Start polling /api/tree every `intervalMs` to discover newly-active runs
     * and drop runs that are no longer active. Calls `onActiveRunsChanged` with
     * the latest active run list each tick.
     */
    startPolling(intervalMs, onActiveRunsChanged) {
        if (this.pollTimer)
            clearInterval(this.pollTimer);
        const tick = async () => {
            try {
                const active = await this.listActiveRuns();
                this.connected = true;
                onActiveRunsChanged(active);
            }
            catch {
                this.connected = false;
                onActiveRunsChanged([]);
            }
        };
        tick();
        this.pollTimer = setInterval(tick, intervalMs);
    }
}
exports.AgentOsClient = AgentOsClient;
//# sourceMappingURL=agentOsClient.js.map