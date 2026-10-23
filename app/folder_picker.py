"""Open a native Windows folder picker from its own main thread."""
from __future__ import annotations

import tkinter as tk
import ctypes
import sys
from tkinter import filedialog


def main() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory(title="Choose a folder to organize")
    root.destroy()
    if selected:
        print(selected)


if __name__ == "__main__":
    main()
