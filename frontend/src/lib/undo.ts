export type UndoSkip = {
  file: string;
  reason: string;
};

export type UndoResult = {
  status: string;
  restored: number;
  skipped: UndoSkip[];
};

export type UndoFeedback = {
  message: string;
  tone: "success" | "warning";
  recoveryAvailable: boolean;
};

export function describeUndoResult(result: UndoResult): UndoFeedback {
  const count = result.skipped.length;
  if (result.restored > 0 && count === 0) {
    return {
      message: `Restored ${result.restored} ${result.restored === 1 ? "file" : "files"}.`,
      tone: "success",
      recoveryAvailable: false,
    };
  }
  if (result.restored > 0) {
    return {
      message: `Restored ${result.restored} ${result.restored === 1 ? "file" : "files"}. ${count} could not be restored. Review History to retry remaining files.`,
      tone: "warning",
      recoveryAvailable: true,
    };
  }
  if (count > 0) {
    return {
      message: `Undo could not restore any files because ${count} ${count === 1 ? "path conflicted or disappeared" : "paths conflicted or disappeared"}. Review History for recovery details.`,
      tone: "warning",
      recoveryAvailable: true,
    };
  }
  return {
    message: "There was no organization run to undo.",
    tone: "success",
    recoveryAvailable: false,
  };
}
