"""Beginner-friendly desktop interface for the AI file organizer.

Run with ``python -m app.gui``.  Previewing only scans and plans; files are
never moved until the user clicks Organize and accepts the confirmation dialog.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, List, Optional

from dotenv import load_dotenv

from app.core.classifier import LLMClassifier
from app.core.executor import ExecutionResult, execute_plan
from app.core.extractor import attach_snippets
from app.core.planner import Move, build_plan
from app.core.scanner import scan_directory
from app.main import resolve_models


class OrganizerApp(tk.Tk):
    """Window that keeps planning and moving as two deliberately separate steps."""

    def __init__(self) -> None:
        super().__init__()
        self.title("AI File Organizer")
        self.minsize(850, 550)
        self.geometry("1050x650")

        self.source_var = tk.StringVar()
        self.models_var = tk.StringVar(value=os.getenv("ORGANIZER_MODELS", ""))
        self.recursive_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a folder, then preview the organization plan.")
        self.plan: List[Move] = []
        self.plan_root: Optional[Path] = None
        self.target_selection: dict[str, bool] = {}
        self.target_items: dict[str, list[str]] = {}
        self.item_targets: dict[str, str] = {}
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.source_var.trace_add("write", lambda *_args: self._clear_plan())
        self.models_var.trace_add("write", lambda *_args: self._clear_plan())

        self._build_window()
        self.after(100, self._check_events)

    def _build_window(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(outer, text="AI File Organizer", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            outer,
            text="Preview suggestions first. Toggle off a target to keep its files in the selected folder.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 14))

        ttk.Label(outer, text="Folder to organize:").grid(row=2, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.source_var).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Button(outer, text="Browse…", command=self._choose_folder).grid(row=2, column=2)

        options = ttk.Frame(outer)
        options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 10))
        options.columnconfigure(2, weight=1)
        ttk.Checkbutton(
            options, text="Include subfolders", variable=self.recursive_var, command=self._clear_plan
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(options, text="Models (optional):").grid(row=0, column=1, sticky="w", padx=(22, 6))
        ttk.Entry(options, textvariable=self.models_var).grid(row=0, column=2, sticky="ew")

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=4, column=0, columnspan=3, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.preview = ttk.Treeview(
            table_frame,
            columns=("selected", "file", "destination", "reason"),
            show="headings",
            selectmode="browse",
        )
        self.preview.heading("selected", text="Organize?")
        self.preview.heading("file", text="File")
        self.preview.heading("destination", text="Will move to")
        self.preview.heading("reason", text="Why")
        self.preview.column("selected", width=115, anchor="center", stretch=False)
        self.preview.column("file", width=240, anchor="w")
        self.preview.column("destination", width=390, anchor="w")
        self.preview.column("reason", width=260, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=scrollbar.set)
        self.preview.grid(row=0, column=0, sticky="nsew")
        self.preview.bind("<Double-1>", self._toggle_current_target)
        scrollbar.grid(row=0, column=1, sticky="ns")

        footer = ttk.Frame(outer)
        footer.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.toggle_button = ttk.Button(footer, text="Toggle Target", command=self._toggle_current_target, state="disabled")
        self.toggle_button.grid(row=0, column=1, padx=(8, 0))
        self.preview_button = ttk.Button(footer, text="Preview Plan", command=self._preview_plan)
        self.preview_button.grid(row=0, column=2, padx=(8, 0))
        self.organize_button = ttk.Button(footer, text="Organize", command=self._confirm_organize, state="disabled")
        self.organize_button.grid(row=0, column=3, padx=(8, 0))

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose a folder to organize")
        if selected:
            self.source_var.set(selected)
            self._clear_plan()

    def _clear_plan(self) -> None:
        self.plan = []
        self.plan_root = None
        self.target_selection = {}
        self.target_items = {}
        self.item_targets = {}
        self.toggle_button.config(state="disabled")
        self.organize_button.config(state="disabled")

    def _models(self) -> List[str]:
        entered = self.models_var.get().strip()
        return resolve_models([model.strip() for model in entered.split(",") if model.strip()]) if entered else resolve_models(None)

    def _preview_plan(self) -> None:
        source = Path(self.source_var.get().strip()).expanduser()
        if not source.is_dir():
            messagebox.showerror("Choose a folder", "Please choose an existing folder to organize.")
            return
        recursive = self.recursive_var.get()
        models = self._models()
        self._clear_plan()
        self._set_busy(True, "Scanning files and preparing a safe preview…")
        self._run_in_background(lambda: self._make_plan(source, recursive, models))

    def _make_plan(self, source: Path, recursive: bool, models: List[str]) -> List[Move]:
        files = scan_directory(str(source), recursive=recursive)
        if not files:
            return []
        attach_snippets(files)
        existing = sorted(
            item.name for item in source.iterdir() if item.is_dir() and not item.name.startswith(".")
        )
        return build_plan(files, str(source), LLMClassifier(models), known_folders=existing)

    def _confirm_organize(self) -> None:
        if not self.plan or self.plan_root is None:
            return
        selected_moves = [move for move in self.plan if move.selected]
        if not selected_moves:
            self.status_var.set("No target subfolders are selected. Files will stay in the selected folder.")
            return
        missing = [Path(move.src).name for move in selected_moves if not Path(move.src).is_file()]
        if missing:
            self._clear_plan()
            messagebox.showwarning(
                "Preview needs refreshing",
                "Some files changed after the preview. Create a new preview before organizing.",
            )
            return
        answer = messagebox.askyesno(
            "Confirm file moves",
            f"Move {len(selected_moves)} selected file(s) into topic folders inside:\n{self.plan_root}\n\n"
            "Files in unselected target subfolders will stay where they are.\n"
            "An undo log will be saved there.\n\n"
            "Do you want to organize these files now?",
            icon="warning",
        )
        if not answer:
            self.status_var.set("Organization cancelled. The preview is still available to review.")
            return
        self._set_busy(True, "Organizing files…")
        self._run_in_background(
            lambda: execute_plan(
                self.plan,
                str(self.plan_root),
                dry_run=False,
                source_root=str(self.plan_root),
            )
        )

    def _run_in_background(self, work: Callable[[], object]) -> None:
        def runner() -> None:
            try:
                self.events.put(("success", work()))
            except Exception as error:
                self.events.put(("error", error))

        threading.Thread(target=runner, daemon=True).start()

    def _check_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                self._set_busy(False)
                if event == "error":
                    self.status_var.set("Could not complete the request.")
                    messagebox.showerror("Could not organize files", str(value))
                    continue
                if isinstance(value, ExecutionResult):
                    self._clear_plan()
                    summary = f"Organized {value.moved} file(s)."
                    if value.failures or value.log_error:
                        details = "\n".join(
                            f"Skipped {item['file']}: {item['reason']}" for item in value.failures
                        )
                        self.status_var.set(summary + " Create a new preview for skipped files.")
                        messagebox.showwarning("Organization incomplete", "\n".join(
                            part for part in (summary, details, value.log_error) if part
                        ))
                    else:
                        self.status_var.set(summary + " Use the CLI undo command if needed.")
                        messagebox.showinfo("Finished", summary)
                elif isinstance(value, list) and (not value or isinstance(value[0], Move)):
                    self._show_plan(value)
                else:
                    moved = len(value)
                    self._clear_plan()
                    self.status_var.set(f"Organized {moved} file(s). Use the CLI undo command if needed.")
                    messagebox.showinfo("Finished", f"Organized {moved} file(s).")
        except queue.Empty:
            pass
        self.after(100, self._check_events)

    def _show_plan(self, moves: List[Move]) -> None:
        for item in self.preview.get_children():
            self.preview.delete(item)
        self.plan = moves
        self.plan_root = Path(self.source_var.get().strip()).expanduser()
        self.target_selection = {}
        self.target_items = {}
        self.item_targets = {}
        for move in moves:
            target = self._target_name(move)
            self.target_selection.setdefault(target, move.selected)
            destination = str(Path(target) / Path(move.dest).name)
            item = self.preview.insert(
                "", "end", values=(self._selection_label(move.selected), Path(move.src).name, destination, move.reason or "—")
            )
            self.target_items.setdefault(target, []).append(item)
            self.item_targets[item] = target
        if moves:
            self.status_var.set(
                f"Preview ready: {len(moves)} file(s). Select a row and toggle its target if you want to keep it in place."
            )
            self.toggle_button.config(state="normal")
            self.organize_button.config(state="normal")
        else:
            self.status_var.set("No eligible files were found. Nothing will be moved.")

    def _set_busy(self, busy: bool, status: Optional[str] = None) -> None:
        self.busy = busy
        button_state = "disabled" if busy else "normal"
        self.preview_button.config(state=button_state)
        self.toggle_button.config(state="disabled" if busy or not self.plan else "normal")
        self.organize_button.config(state="disabled" if busy or not self.plan else "normal")
        if status:
            self.status_var.set(status)

    @staticmethod
    def _target_name(move: Move) -> str:
        return str(Path(move.folder) / move.subfolder) if move.subfolder else move.folder

    @staticmethod
    def _selection_label(selected: bool) -> str:
        return "☑ Selected" if selected else "☐ Keep in root"

    def _toggle_current_target(self, _event=None) -> None:
        selected_items = self.preview.selection()
        if not selected_items:
            self.status_var.set("Select a file row to toggle its target subfolder.")
            return
        target = self.item_targets[selected_items[0]]
        selected = not self.target_selection[target]
        self.target_selection[target] = selected
        for move in self.plan:
            if self._target_name(move) == target:
                move.selected = selected
        for item in self.target_items[target]:
            values = list(self.preview.item(item, "values"))
            values[0] = self._selection_label(selected)
            self.preview.item(item, values=values)
        action = "will be organized" if selected else "will stay in the selected folder"
        self.status_var.set(f"{target}: {action}.")


def main() -> None:
    load_dotenv()
    app = OrganizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
