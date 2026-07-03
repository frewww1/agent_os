import * as vscode from "vscode";
import { translateRange } from "./utils";

/**
 * Manages green (added) line decorations for a single editor.
 * Mirrors Continue.dev's AddedLineDecorationManager — one shared
 * decorationType, a list of ranges, shift/delete helpers.
 */
export class AddedLineDecorationManager {
  ranges: vscode.Range[] = [];
  private decorationType = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: { id: "diffEditor.insertedLineBackground" },
    outlineWidth: "1px",
    outlineStyle: "solid",
    outlineColor: { id: "diffEditor.insertedTextBorder" },
    rangeBehavior: vscode.DecorationRangeBehavior.ClosedClosed,
  });

  constructor(private editor: vscode.TextEditor) {}

  applyToNewEditor(newEditor: vscode.TextEditor) {
    this.editor = newEditor;
    this.editor.setDecorations(this.decorationType, this.ranges);
  }

  /**
   * Add `numLines` green-highlighted lines starting at `startIndex` (0-based).
   * Merges with the previous range if contiguous.
   */
  addLines(startIndex: number, numLines: number) {
    const lastRange = this.ranges[this.ranges.length - 1];
    if (lastRange && lastRange.end.line === startIndex - 1) {
      this.ranges[this.ranges.length - 1] = lastRange.with(
        undefined,
        lastRange.end.translate(numLines)
      );
    } else {
      this.ranges.push(
        new vscode.Range(
          startIndex,
          0,
          startIndex + numLines - 1,
          Number.MAX_SAFE_INTEGER
        )
      );
    }
    this.editor.setDecorations(this.decorationType, this.ranges);
  }

  /**
   * Add an explicit list of ranges (used when restoring from persisted state
   * or applying a precomputed hunk). Does not merge.
   */
  setRanges(ranges: vscode.Range[]) {
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
  shiftDownAfterLine(afterLine: number, offset: number) {
    if (offset === 0) return;
    for (let i = 0; i < this.ranges.length; i++) {
      if (this.ranges[i].start.line >= afterLine) {
        this.ranges[i] = translateRange(this.ranges[i], offset);
      }
    }
    this.editor.setDecorations(this.decorationType, this.ranges);
  }

  /**
   * Remove the range that starts exactly at `line`. Returns the removed
   * range's line count, or 0 if none matched.
   */
  deleteRangeStartingAt(line: number): number {
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

/**
 * Manages red (removed) line decorations. Each removed line gets its own
 * decorationType because the `after.contentText` (ghost text showing the
 * deleted content) differs per line.
 *
 * Implementation note: the `textDecoration: "none; display: none"` trick
 * hides the user's caret/text on the red line so the ghost text is the
 * only thing visible — same as Continue.dev.
 */
export class RemovedLineDecorationManager {
  ranges: {
    line: string;
    range: vscode.Range;
    decoration: vscode.TextEditorDecorationType;
  }[] = [];

  constructor(private editor: vscode.TextEditor) {}

  applyToNewEditor(newEditor: vscode.TextEditor) {
    this.editor = newEditor;
    this.applyDecorations();
  }

  private makeDecorationType(line: string): vscode.TextEditorDecorationType {
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

  addLines(startIndex: number, lines: string[]) {
    let i = 0;
    for (const line of lines) {
      this.ranges.push({
        line,
        range: new vscode.Range(
          startIndex + i,
          0,
          startIndex + i,
          Number.MAX_SAFE_INTEGER
        ),
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
  setEntries(
    entries: { line: string; range: vscode.Range }[]
  ) {
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

  shiftDownAfterLine(afterLine: number, offset: number) {
    if (offset === 0) return;
    for (let i = 0; i < this.ranges.length; i++) {
      if (this.ranges[i].range.start.line >= afterLine) {
        this.ranges[i].range = translateRange(this.ranges[i].range, offset);
      }
    }
    this.applyDecorations();
  }

  /**
   * Remove all red ranges starting at `line` (sequential run). Returns the
   * removed entries (so caller can recover the original text for reject).
   */
  deleteRangesStartingAt(
    line: number
  ): { line: string; range: vscode.Range }[] {
    for (let i = 0; i < this.ranges.length; i++) {
      if (this.ranges[i].range.start.line === line) {
        let sequential = 0;
        while (
          i + sequential < this.ranges.length &&
          this.ranges[i + sequential].range.start.line === line + sequential
        ) {
          this.ranges[i + sequential].decoration.dispose();
          sequential++;
        }
        return this.ranges.splice(i, sequential);
      }
    }
    return [];
  }
}
