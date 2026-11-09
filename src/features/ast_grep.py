"""
AST-Grep Structural Code Search (OMP pi-lens & wire Standout)
Searches codebases by syntactic Abstract Syntax Tree (AST) structure
rather than raw regex, matching functions, classes, and calls regardless of formatting.
"""

import ast
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


class ASTGrepSearcher:
    """Structural Python AST code pattern searcher."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or r"D:\Harness\omni").resolve()

    def search_functions(self, name_pattern: str = "", path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Finds function and async function definitions matching name or docstring."""
        results = []
        target_dir = Path(path).resolve() if path else self.workspace_root

        for root, dirs, files in os.walk(target_dir):
            if any(skip in root for skip in [".git", "__pycache__", "node_modules", "tests"]):
                continue
            for f in files:
                if f.endswith(".py"):
                    full_p = os.path.join(root, f)
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="replace") as fp:
                            content = fp.read()
                        tree = ast.parse(content, filename=full_p)
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if not name_pattern or name_pattern.lower() in node.name.lower():
                                    args_list = [a.arg for a in node.args.args]
                                    results.append({
                                        "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                                        "name": node.name,
                                        "args": args_list,
                                        "file": str(Path(full_p).relative_to(self.workspace_root)),
                                        "line": node.lineno,
                                        "docstring": ast.get_docstring(node) or "",
                                    })
                    except Exception:
                        pass
        return results

    def search_classes(self, name_pattern: str = "", path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Finds class definitions matching name."""
        results = []
        target_dir = Path(path).resolve() if path else self.workspace_root

        for root, dirs, files in os.walk(target_dir):
            if any(skip in root for skip in [".git", "__pycache__", "node_modules", "tests"]):
                continue
            for f in files:
                if f.endswith(".py"):
                    full_p = os.path.join(root, f)
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="replace") as fp:
                            content = fp.read()
                        tree = ast.parse(content, filename=full_p)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.ClassDef):
                                if not name_pattern or name_pattern.lower() in node.name.lower():
                                    methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                                    results.append({
                                        "kind": "class",
                                        "name": node.name,
                                        "methods": methods,
                                        "file": str(Path(full_p).relative_to(self.workspace_root)),
                                        "line": node.lineno,
                                        "docstring": ast.get_docstring(node) or "",
                                    })
                    except Exception:
                        pass
        return results

    def search_calls(self, callee_name: str, path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Finds calls to a specific function or attribute across the codebase."""
        results = []
        target_dir = Path(path).resolve() if path else self.workspace_root

        for root, dirs, files in os.walk(target_dir):
            if any(skip in root for skip in [".git", "__pycache__", "node_modules", "tests"]):
                continue
            for f in files:
                if f.endswith(".py"):
                    full_p = os.path.join(root, f)
                    try:
                        with open(full_p, "r", encoding="utf-8", errors="replace") as fp:
                            content = fp.read()
                        tree = ast.parse(content, filename=full_p)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Call):
                                called_id = None
                                if isinstance(node.func, ast.Name):
                                    called_id = node.func.id
                                elif isinstance(node.func, ast.Attribute):
                                    called_id = node.func.attr
                                if called_id and callee_name.lower() in called_id.lower():
                                    results.append({
                                        "callee": called_id,
                                        "file": str(Path(full_p).relative_to(self.workspace_root)),
                                        "line": node.lineno,
                                    })
                    except Exception:
                        pass
        return results
