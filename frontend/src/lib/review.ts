export type ReviewFilter = "all" | "attention" | "security";
export type ReviewBatchMode = "small" | "medium" | "large";
export type ReviewFileStatus = "ready" | "attention" | "security" | "excluded";

export type ReviewFile = {
  id: string;
  name: string;
  target_path: string;
  destination_name: string;
  category: string;
  confidence: string;
  evidence_sufficient: boolean;
  extraction_status: string;
  filename_conflict: string;
  warnings: string[];
  sensitive: boolean;
  selected: boolean;
};

export type ReviewEdit = {
  accepted: boolean;
  target_path: string;
  destination_name: string;
};

export type ReviewStats = {
  total: number;
  selected: number;
  excluded: number;
  ready: number;
  attention: number;
  security: number;
};

const SECURITY_WARNING = /\b(?:sensitive|credential|private key|unsafe|malicious|security|injection)\b/i;
const DIAGNOSTIC_WARNING = /\b(?:parser|extension|metadata|extraction|grounded|evidence phrase)\b/i;

function currentEdit(file: ReviewFile, edit?: ReviewEdit): ReviewEdit {
  return edit ?? {
    accepted: file.selected,
    target_path: file.target_path,
    destination_name: file.destination_name,
  };
}

export function getReviewBatchMode(fileCount: number): ReviewBatchMode {
  if (fileCount > 1000) return "large";
  if (fileCount > 100) return "medium";
  return "small";
}

export function getInitialGroupLimit(fileCount: number): number {
  const mode = getReviewBatchMode(fileCount);
  if (mode === "large") return 40;
  if (mode === "medium") return 80;
  return Number.POSITIVE_INFINITY;
}

export function shouldExpandGroupsInitially(fileCount: number): boolean {
  return getReviewBatchMode(fileCount) === "small";
}

export function shouldShowReviewTools(fileCount: number): boolean {
  return fileCount > 50;
}

export function fileHasSecurityConcern(file: ReviewFile): boolean {
  return file.sensitive || file.warnings.some((warning) => SECURITY_WARNING.test(warning));
}

export function fileNeedsAttention(file: ReviewFile, edit?: ReviewEdit): boolean {
  const current = currentEdit(file, edit);
  return (
    !file.evidence_sufficient
    || file.confidence === "low"
    || file.confidence === "insufficient"
    || file.extraction_status !== "sufficient"
    || file.filename_conflict !== "none"
    || (current.accepted && (!current.target_path.trim() || !current.destination_name.trim()))
    || file.warnings.some((warning) => !DIAGNOSTIC_WARNING.test(warning))
  );
}

export function getFileReviewStatus(file: ReviewFile, edit?: ReviewEdit): ReviewFileStatus {
  const current = currentEdit(file, edit);
  if (fileHasSecurityConcern(file)) return "security";
  if (fileNeedsAttention(file, current)) return "attention";
  if (!current.accepted) return "excluded";
  return "ready";
}

export function summarizeReview(files: ReviewFile[], edits: Record<string, ReviewEdit>): ReviewStats {
  const stats: ReviewStats = {
    total: files.length,
    selected: 0,
    excluded: 0,
    ready: 0,
    attention: 0,
    security: 0,
  };
  for (const file of files) {
    const edit = edits[file.id];
    if (edit?.accepted ?? file.selected) stats.selected += 1;
    const status = getFileReviewStatus(file, edit);
    stats[status] += 1;
  }
  return stats;
}

export function matchesReviewQuery(file: ReviewFile, edit: ReviewEdit | undefined, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  const current = currentEdit(file, edit);
  return [
    file.name,
    current.destination_name,
    current.target_path,
    file.category,
  ].some((value) => value.toLowerCase().includes(normalized));
}

export function fileMatchesReviewFilter(
  file: ReviewFile,
  edit: ReviewEdit | undefined,
  filter: ReviewFilter,
): boolean {
  if (filter === "security") return fileHasSecurityConcern(file);
  if (filter === "attention") return getFileReviewStatus(file, edit) === "attention";
  return true;
}

export function filterReviewGroups<T extends ReviewFile>(
  groups: Array<[string, T[]]>,
  edits: Record<string, ReviewEdit>,
  query: string,
  filter: ReviewFilter,
): Array<[string, T[]]> {
  return groups
    .map(([target, files]) => [
      target,
      files.filter((file) => (
        matchesReviewQuery(file, edits[file.id], query)
        && fileMatchesReviewFilter(file, edits[file.id], filter)
      )),
    ] as [string, T[]])
    .filter(([, files]) => files.length > 0);
}

export function summarizeFileWarning(file: ReviewFile, edit?: ReviewEdit): string {
  const current = currentEdit(file, edit);
  if (fileHasSecurityConcern(file)) {
    return "Sensitive content needs your review before this file can be included.";
  }
  if (current.accepted && (!current.target_path.trim() || !current.destination_name.trim())) {
    return "A destination or filename needs attention.";
  }
  if (file.extraction_status !== "sufficient") {
    return "Sortly could not fully confirm what this file contains.";
  }
  if (file.confidence === "low" || file.confidence === "insufficient") {
    return "This suggestion is uncertain. Please check the destination and filename.";
  }
  if (file.filename_conflict === "misleading" || file.filename_conflict === "extension_mismatch") {
    return "The original file type may be misleading. Review the proposed name.";
  }
  if (file.filename_conflict === "generic") {
    return "A clearer filename is suggested.";
  }
  const actionable = file.warnings.find((warning) => !DIAGNOSTIC_WARNING.test(warning));
  return actionable ?? "";
}

export function summarizeGroup(files: ReviewFile[], edits: Record<string, ReviewEdit>) {
  return files.reduce(
    (summary, file) => {
      const status = getFileReviewStatus(file, edits[file.id]);
      if (status === "attention") summary.attention += 1;
      if (status === "security") summary.security += 1;
      return summary;
    },
    { attention: 0, security: 0 },
  );
}
