import * as vscode from "vscode";
import * as path from "path";

/**
 * A contiguous diff hunk: lines that were removed (red) and lines that were
 * added (green) at the same location. One Edit/Write event may produce
 * multiple hunks if old/new strings have interleaved changes.
 */
export interface DiffHunk {
  /** 0-based line in the *new* file where this hunk starts (green block start). */
  newStartLine: number;
  /** Original (removed) lines — shown as red ghost text. */
  redLines: string[];
  /** New (added) lines — shown as green highlight. */
  greenLines: string[];
}

/**
 * Compute diff hunks between oldStr and newStr using a simple LCS-by-line
 * algorithm. Returns hunks aligned to *new* file line numbers, so callers can
 * directly map them to decorations on the post-edit document.
 *
 * Note: this is a straightforward O(n*m) LCS, fine for typical Edit sizes
 * (hundreds of lines). For very large files we fall back to a trivial
 * "everything changed" single hunk.
 */
export function computeDiffHunks(oldStr: string, newStr: string): DiffHunk[] {
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
  const MAX_CELLS = 200_000;
  if (oldLines.length * newLines.length > MAX_CELLS) {
    return [{ newStartLine: 0, redLines: oldLines, greenLines: newLines }];
  }

  // Build LCS table
  const m = oldLines.length;
  const n = newLines.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array<number>(n + 1).fill(0)
  );
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (oldLines[i] === newLines[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  // Walk the table to emit hunks. We track runs of non-matching lines.
  const hunks: DiffHunk[] = [];
  let i = 0;
  let j = 0;
  let redBuf: string[] = [];
  let greenBuf: string[] = [];
  let greenStart = 0; // new-file line where current green buffer started

  const flush = () => {
    if (redBuf.length === 0 && greenBuf.length === 0) return;
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
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      if (greenBuf.length === 0 && redBuf.length === 0) {
        greenStart = j;
      }
      redBuf.push(oldLines[i]);
      i++;
    } else {
      if (greenBuf.length === 0 && redBuf.length === 0) {
        greenStart = j;
      }
      greenBuf.push(newLines[j]);
      j++;
    }
  }
  while (i < m) {
    if (greenBuf.length === 0 && redBuf.length === 0) greenStart = j;
    redBuf.push(oldLines[i]);
    i++;
  }
  while (j < n) {
    if (greenBuf.length === 0 && redBuf.length === 0) greenStart = j;
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
function mergeAdjacentHunks(hunks: DiffHunk[]): DiffHunk[] {
  if (hunks.length <= 1) return hunks;
  const merged: DiffHunk[] = [hunks[0]];
  for (let k = 1; k < hunks.length; k++) {
    const prev = merged[merged.length - 1];
    const cur = hunks[k];
    const prevEnd = prev.newStartLine + prev.greenLines.length;
    if (cur.newStartLine === prevEnd) {
      prev.redLines.push(...cur.redLines);
      prev.greenLines.push(...cur.greenLines);
    } else {
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
export function resolveFilePath(
  filePath: string,
  workspacePath?: string
): string {
  if (!filePath) return filePath;
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
export function translateRange(
  range: vscode.Range,
  lineOffset: number
): vscode.Range {
  if (lineOffset === 0) return range;
  return new vscode.Range(
    range.start.translate(lineOffset),
    range.end.translate(lineOffset)
  );
}

/**
 * Find the 0-based start line of `needle` inside `document`. Returns null if
 * not found (e.g. user manually edited the file and the old_string no longer
 * matches — caller should mark the block as stale).
 *
 * We search the document text directly rather than line-by-line to handle
 * multi-line old_strings correctly.
 */
export function findStartLine(
  document: vscode.TextDocument,
  needle: string
): number | null {
  if (!needle) return null;
  const fullText = document.getText();
  const idx = fullText.indexOf(needle);
  if (idx < 0) return null;
  return document.positionAt(idx).line;
}

/**
 * Compare two file paths in a platform-insensitive way (Windows) but fall
 * back to sensitive on POSIX. Normalizes both sides first.
 */
export function sameFile(a: string, b: string): boolean {
  const na = path.normalize(a).toLowerCase();
  const nb = path.normalize(b).toLowerCase();
  return na === nb;
}
