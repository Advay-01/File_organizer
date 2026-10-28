import { ChevronDown, FileText, Folder, FolderOpen } from "lucide-react";
import { useState } from "react";
import type { HistoryTree as Tree } from "../lib/history";

function Branch({ node, root = false }: { node: Tree; root?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = root ? FolderOpen : Folder;
  return <details className="py-1" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary className={`flex cursor-pointer list-none items-center gap-2 ${root ? "font-medium text-ink" : "text-ink-soft"}`}>
        <ChevronDown size={14} className={`shrink-0 transition ${expanded ? "" : "-rotate-90"}`} />
        <Icon size={root ? 16 : 15} className="shrink-0 text-accent-text" />
        <span className="break-all">{node.name}</span>
        <span className="history-chip">{node.count}</span>
      </summary>
      <div className="ml-3 mt-1 border-l border-line pl-3">
        {node.folders.map((folder) => <Branch key={folder.name} node={folder} />)}
        {node.files.map((file, index) => <div
          key={`${file.src ?? file.from_name}-${file.dest ?? file.to_name}-${index}`}
          className="flex flex-wrap items-center gap-2 py-1 text-ink-soft"
          title={file.restored ? `Restored to ${file.src ?? file.from_name}` : file.renamed ? `Originally ${file.from_name}` : file.dest}
        >
          <FileText size={14} className="shrink-0 text-ink-faint" />
          <span className="break-all">{file.to_name}</span>
          {file.renamed && <span className="history-chip">renamed</span>}
          {file.restored && <span className="history-chip" data-tone="muted">restored</span>}
        </div>)}
      </div>
    </details>;
}

export default function HistoryTree({ tree }: { tree: Tree }) {
  return <div className="my-2 max-h-80 overflow-y-auto rounded-lg border border-line bg-surface-inset p-3 text-sm" aria-label="Folder tree">
    <Branch node={tree} root />
  </div>;
}
