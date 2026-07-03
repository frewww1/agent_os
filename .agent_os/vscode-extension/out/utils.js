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
exports.computeDiffHunks = computeDiffHunks;
exports.resolveFilePath = resolveFilePath;
exports.translateRange = translateRange;
exports.findStartLine = findStartLine;
exports.sameFile = sameFile;
const vscode = __importStar(require("vscode"));
const path = __importStar(require("path"));
/**
 * Compute diff hunks between oldStr and newStr using a simple LCS-by-line
 * algorithm. Returns hunks aligned to *new* file line numbers, so callers can
 * directly map them to decorations on the post-edit document.
 *
 * Note: this is a straightforward O(n*m) LCS, fine for typical Edit sizes
 * (hundreds of lines). For very large files we fall back to a trivial
 * "everything changed" single hunk.
 */
function computeDiffHunks(oldStr, newStr) {
    const oldLines = oldStr.length === 0 ? [] : oldStr.split(/\r?\n/);
    const newLines = newStr.length === 0 ? [] : newStr.split(/\r?\n/);
    // Trivial cases
    if (oldLines.length === 0 && newLines.length === 0) {
        return [];
    }
    if (oldLines.length === 0) {
        // Pure insertion
        return [{ newStartLine: 0, redLines: [], greenLines: newLines }];
    }
    if (newLines.length === 0) {
        // Pure deletion — no new lines to decorate; represent as empty hunk at line 0
        return [{ newStartLine: 0, redLines: oldLines, greenLines: [] }];
    }
    // Cap LCS table size to avoid pathological blowups
    const MAX_CELLS = 200000;
    if (oldLines.length * newLines.length > MAX_CELLS) {
        return [{ newStartLine: 0, redLines: oldLines, greenLines: newLines }];
    }
    // Build LCS table
    const m = oldLines.length;
    const n = newLines.length;
    const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
    for (let i = m - 1; i >= 0; i--) {
        for (let j = n - 1; j >= 0; j--) {
            if (oldLines[i] === newLines[j]) {
                dp[i][j] = dp[i + 1][j + 1] + 1;
            }
            else {
                dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
    }
    // Walk the table to emit hunks. We track runs of non-matching lines.
    const hunks = [];
    let i = 0;
    let j = 0;
    let redBuf = [];
    let greenBuf = [];
    let greenStart = 0; // new-file line where current green buffer started
    const flush = () => {
        if (redBuf.length === 0 && greenBuf.length === 0)
            return;
        hunks.push({
            newStartLine: greenStart,
            redLines: redBuf,
            greenLines: greenBuf,
        });
        redBuf = [];
        greenBuf = [];
    };
    while (i < m && j < n) {
        if (oldLines[i] === newLines[j]) {
            flush();
            i++;
            j++;
            greenStart = j;
        }
        else if (dp[i + 1][j] >= dp[i][j + 1]) {
            if (greenBuf.length === 0 && redBuf.length === 0) {
                greenStart = j;
            }
            redBuf.push(oldLines[i]);
            i++;
        }
        else {
            if (greenBuf.length === 0 && redBuf.length === 0) {
                greenStart = j;
            }
            greenBuf.push(newLines[j]);
            j++;
        }
    }
    while (i < m) {
        if (greenBuf.length === 0 && redBuf.length === 0)
            greenStart = j;
        redBuf.push(oldLines[i]);
        i++;
    }
    while (j < n) {
        if (greenBuf.length === 0 && redBuf.length === 0)
            greenStart = j;
        greenBuf.push(newLines[j]);
        j++;
    }
    flush();
    return mergeAdjacentHunks(hunks);
}
/**
 * Merge hunks that are directly adjacent (no matching line between them),
 * since they should render as one continuous diff block.
 */
function mergeAdjacentHunks(hunks) {
    if (hunks.length <= 1)
        return hunks;
    const merged = [hunks[0]];
    for (let k = 1; k < hunks.length; k++) {
        const prev = merged[merged.length - 1];
        const cur = hunks[k];
        const prevEnd = prev.newStartLine + prev.greenLines.length;
        if (cur.newStartLine === prevEnd) {
            prev.redLines.push(...cur.redLines);
            prev.greenLines.push(...cur.greenLines);
        }
        else {
            merged.push(cur);
        }
    }
    return merged;
}
/**
 * Resolve a file_path from a tool_use event to an absolute fs path.
 * - If already absolute, return as-is (normalized).
 * - If relative, resolve against workspacePath.
 * - If workspacePath is empty, try against each VS Code workspace folder.
 */
function resolveFilePath(filePath, workspacePath) {
    if (!filePath)
        return filePath;
    if (path.isAbsolute(filePath)) {
        return path.normalize(filePath);
    }
    if (workspacePath) {
        return path.normalize(path.join(workspacePath, filePath));
    }
    const folders = vscode.workspace.workspaceFolders;
    if (folders && folders.length > 0) {
        return path.normalize(path.join(folders[0].uri.fsPath, filePath));
    }
    return path.normalize(filePath);
}
/**
 * Translate a Range by a line offset (used when shifting decorations after
 * an insert/delete elsewhere in the file).
 */
function translateRange(range, lineOffset) {
    if (lineOffset === 0)
        return range;
    return new vscode.Range(range.start.translate(lineOffset), range.end.translate(lineOffset));
}
/**
 * Find the 0-based start line of `needle` inside `document`. Returns null if
 * not found (e.g. user manually edited the file and the old_string no longer
 * matches — caller should mark the block as stale).
 *
 * We search the document text directly rather than line-by-line to handle
 * multi-line old_strings correctly.
 */
function findStartLine(document, needle) {
    if (!needle)
        return null;
    const fullText = document.getText();
    const idx = fullText.indexOf(needle);
    if (idx < 0)
        return null;
    return document.positionAt(idx).line;
}
/**
 * Compare two file paths in a platform-insensitive way (Windows) but fall
 * back to sensitive on POSIX. Normalizes both sides first.
 */
function sameFile(a, b) {
    const na = path.normalize(a).toLowerCase();
    const nb = path.normalize(b).toLowerCase();
    return na === nb;
}
//# sourceMappingURL=utils.js.map