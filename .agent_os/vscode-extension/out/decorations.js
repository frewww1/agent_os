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
exports.RemovedLineDecorationManager = exports.AddedLineDecorationManager = void 0;
const vscode = __importStar(require("vscode"));
const utils_1 = require("./utils");
/**
 * Manages green (added) line decorations for a single editor.
 * Mirrors Continue.dev's AddedLineDecorationManager — one shared
 * decorationType, a list of ranges, shift/delete helpers.
 */
class AddedLineDecorationManager {
    constructor(editor) {
        this.editor = editor;
        this.ranges = [];
        this.decorationType = vscode.window.createTextEditorDecorationType({
            isWholeLine: true,
            backgroundColor: { id: "diffEditor.insertedLineBackground" },
            outlineWidth: "1px",
            outlineStyle: "solid",
            outlineColor: { id: "diffEditor.insertedTextBorder" },
            rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
        });
    }
    applyToNewEditor(newEditor) {
        this.editor = newEditor;
        this.editor.setDecorations(this.decorationType, this.ranges);
    }
    /**
     * Add `numLines` green-highlighted lines starting at `startIndex` (0-based).
     * Merges with the previous range if contiguous.
     */
    addLines(startIndex, numLines) {
        const lastRange = this.ranges[this.ranges.length - 1];
        if (lastRange && lastRange.end.line === startIndex - 1) {
            this.ranges[this.ranges.length - 1] = lastRange.with(undefined, lastRange.end.translate(numLines));
        }
        else {
            this.ranges.push(new vscode.Range(startIndex, 0, startIndex + numLines - 1, Number.MAX_SAFE_INTEGER));
        }
        this.editor.setDecorations(this.decorationType, this.ranges);
    }
    /**
     * Add an explicit list of ranges (used when restoring from persisted state
     * or applying a precomputed hunk). Does not merge.
     */
    setRanges(ranges) {
        this.ranges = ranges.slice();
        this.editor.setDecorations(this.decorationType, this.ranges);
    }
    clear() {
        this.ranges = [];
        this.editor.setDecorations(this.decorationType, this.ranges);
    }
    /**
     * Shift all ranges at or below `afterLine` by `offset` (may be negative).
     */
    shiftDownAfterLine(afterLine, offset) {
        if (offset === 0)
            return;
        for (let i = 0; i < this.ranges.length; i++) {
            if (this.ranges[i].start.line >= afterLine) {
                this.ranges[i] = (0, utils_1.translateRange)(this.ranges[i], offset);
            }
        }
        this.editor.setDecorations(this.decorationType, this.ranges);
    }
    /**
     * Remove the range that starts exactly at `line`. Returns the removed
     * range's line count, or 0 if none matched.
     */
    deleteRangeStartingAt(line) {
        for (let i = 0; i < this.ranges.length; i++) {
            if (this.ranges[i].start.line === line) {
                const removed = this.ranges.splice(i, 1)[0];
                this.editor.setDecorations(this.decorationType, this.ranges);
                return removed.end.line - removed.start.line + 1;
            }
        }
        this.editor.setDecorations(this.decorationType, this.ranges);
        return 0;
    }
    dispose() {
        this.decorationType.dispose();
    }
}
exports.AddedLineDecorationManager = AddedLineDecorationManager;
/**
 * Manages red (removed) line decorations. Each removed line gets its own
 * decorationType because the `after.contentText` (ghost text showing the
 * deleted content) differs per line.
 *
 * Implementation note: the `textDecoration: "none; display: none"` trick
 * hides the user's caret/text on the red line so the ghost text is the
 * only thing visible — same as Continue.dev.
 */
class RemovedLineDecorationManager {
    constructor(editor) {
        this.editor = editor;
        this.ranges = [];
    }
    applyToNewEditor(newEditor) {
        this.editor = newEditor;
        this.applyDecorations();
    }
    makeDecorationType(line) {
        return vscode.window.createTextEditorDecorationType({
            isWholeLine: true,
            backgroundColor: { id: "diffEditor.removedLineBackground" },
            outlineWidth: "1px",
            outlineStyle: "solid",
            outlineColor: { id: "diffEditor.removedTextBorder" },
            rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
            after: {
                contentText: line,
                color: "#808080",
                textDecoration: "none; white-space: pre",
            },
            // Hide any text the user might type into the red line — it will be
            // deleted on accept/reject anyway. (Mirrors Continue.dev behavior.)
            textDecoration: "none; display: none",
        });
    }
    addLines(startIndex, lines) {
        let i = 0;
        for (const line of lines) {
            this.ranges.push({
                line,
                range: new vscode.Range(startIndex + i, 0, startIndex + i, Number.MAX_SAFE_INTEGER),
                decoration: this.makeDecorationType(line),
            });
            i++;
        }
        this.applyDecorations();
    }
    /**
     * Set explicit (line, range) entries — used when restoring from persisted
     * state. Ranges must be pre-translated to the current document layout.
     */
    setEntries(entries) {
        this.clear();
        for (const e of entries) {
            this.ranges.push({
                line: e.line,
                range: e.range,
                decoration: this.makeDecorationType(e.line),
            });
        }
        this.applyDecorations();
    }
    applyDecorations() {
        for (const r of this.ranges) {
            this.editor.setDecorations(r.decoration, [r.range]);
        }
    }
    clear() {
        for (const r of this.ranges) {
            r.decoration.dispose();
        }
        this.ranges = [];
    }
    shiftDownAfterLine(afterLine, offset) {
        if (offset === 0)
            return;
        for (let i = 0; i < this.ranges.length; i++) {
            if (this.ranges[i].range.start.line >= afterLine) {
                this.ranges[i].range = (0, utils_1.translateRange)(this.ranges[i].range, offset);
            }
        }
        this.applyDecorations();
    }
    /**
     * Remove all red ranges starting at `line` (sequential run). Returns the
     * removed entries (so caller can recover the original text for reject).
     */
    deleteRangesStartingAt(line) {
        for (let i = 0; i < this.ranges.length; i++) {
            if (this.ranges[i].range.start.line === line) {
                let sequential = 0;
                while (i + sequential < this.ranges.length &&
                    this.ranges[i + sequential].range.start.line === line + sequential) {
                    this.ranges[i + sequential].decoration.dispose();
                    sequential++;
                }
                return this.ranges.splice(i, sequential);
            }
        }
        return [];
    }
}
exports.RemovedLineDecorationManager = RemovedLineDecorationManager;
//# sourceMappingURL=decorations.js.map