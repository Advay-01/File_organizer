export interface HistoryMove {
  src?: string;
  dest?: string;
  from_name: string;
  to_name: string;
  folder: string;
  renamed: boolean;
  restored: boolean;
}

export interface HistoryTree {
  name: string;
  folders: HistoryTree[];
  files: HistoryMove[];
  count: number;
}

/** A snapshot of this run's recorded destinations, not the current filesystem. */
export function buildHistoryTree(moves: HistoryMove[], root: string): HistoryTree {
  const tree: HistoryTree = { name: root || "Organized folder", folders: [], files: [], count: 0 };
  for (const move of moves) {
    let node = tree;
    node.count += 1;
    // Use the journal's relative folder, including for cross-root moves and old records.
    for (const name of move.folder.split(/[\\/]/).filter((part) => part && part !== ".")) {
      let child = node.folders.find((folder) => folder.name === name);
      if (!child) {
        child = { name, folders: [], files: [], count: 0 };
        node.folders.push(child);
      }
      node = child;
      node.count += 1;
    }
    node.files.push(move);
  }
  const sort = (node: HistoryTree) => {
    node.folders.sort((a, b) => a.name.localeCompare(b.name));
    node.files.sort((a, b) => a.to_name.localeCompare(b.to_name));
    node.folders.forEach(sort);
  };
  sort(tree);
  return tree;
}
