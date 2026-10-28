import test from "node:test";
import assert from "node:assert/strict";
import { buildHistoryTree, type HistoryMove } from "../src/lib/history.ts";

function move(folder: string, name: string, extra: Partial<HistoryMove> = {}): HistoryMove {
  return { folder, from_name: name, to_name: name, renamed: false, restored: false, ...extra };
}

test("history nests recorded folders and keeps every file beyond the old 100-move limit", () => {
  const files = Array.from({ length: 300 }, (_, i) => move(i % 2 ? "Code/Python" : "Documents/Notes", `${i}.txt`));
  const tree = buildHistoryTree(files, "C:\\Output");
  assert.equal(tree.name, "C:\\Output");
  assert.equal(tree.count, 300);
  assert.deepEqual(tree.folders.map((folder) => folder.name), ["Code", "Documents"]);
  assert.equal(tree.folders[0].folders[0].files.length, 150);
  assert.equal(tree.folders[1].count, 150);
});

test("cross-root, renamed and partly restored files retain their recorded details", () => {
  const renamed = move("Code\\CSS", "new.css", {
    src: "C:\\Input\\old.txt", dest: "D:\\Output\\Code\\CSS\\new.css",
    from_name: "old.txt", renamed: true, restored: true,
  });
  const tree = buildHistoryTree([renamed, move("Code/HTML", "new.css"), move(".", "root.txt")], "D:\\Output");
  assert.equal(tree.files[0].to_name, "root.txt");
  assert.deepEqual(tree.folders[0].folders.map((folder) => folder.name), ["CSS", "HTML"]);
  assert.equal(tree.folders[0].folders[0].files[0], renamed);
  assert.equal(tree.folders[0].folders[1].files[0].restored, false);
  assert.equal(renamed.from_name, "old.txt");
});

test("empty and legacy histories do not require absolute paths", () => {
  assert.equal(buildHistoryTree([], "").count, 0);
  const tree = buildHistoryTree([move("./Notes/", "one.md")], "");
  assert.equal(tree.name, "Organized folder");
  assert.equal(tree.folders[0].files[0].to_name, "one.md");
});
