"""
AST Constraint Verifier & Safety Guard (Hermes Architecture Standout)
Parses Python Abstract Syntax Trees (AST) to verify that generated or mutated skills
contain no dangerous calls, syntax errors, or infinite loops before deployment.
"""

import ast
from typing import List, Tuple


class ASTConstraintChecker:
    def __init__(self):
        # Disallowed function calls and modules
        self.disallowed_calls = {
            "eval", "exec", "__import__", "compile"
        }
        self.disallowed_attributes = {
            ("os", "system"),
            ("os", "popen"),
            ("shutil", "rmtree"),
        }

    def validate_code(self, code_str: str) -> Tuple[bool, List[str]]:
        """
        Validates Python source code against safety and syntax constraints.
        Returns (is_valid, list_of_violations).
        """
        violations = []

        # 1. Syntax validation
        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, [f"SyntaxError on line {e.lineno}: {e.msg}"]
        except Exception as e:
            return False, [f"Failed to parse AST: {e}"]

        # 2. Node visitor checks
        for node in ast.walk(tree):
            # Check function calls
            if isinstance(node, ast.Call):
                # Direct call e.g. eval()
                if isinstance(node.func, ast.Name):
                    if node.func.id in self.disallowed_calls:
                        violations.append(f"Forbidden call to '{node.func.id}()' at line {node.lineno}")

                # Attribute call e.g. os.system()
                elif isinstance(node.func, ast.Attribute):
                    if isinstance(node.func.value, ast.Name):
                        target = (node.func.value.id, node.func.attr)
                        if target in self.disallowed_attributes:
                            violations.append(f"Forbidden call to '{target[0]}.{target[1]}()' at line {node.lineno}")

            # Check for unbounded while loops (while True: with no break)
            elif isinstance(node, ast.While):
                has_break = any(isinstance(child, ast.Break) for child in ast.walk(node))
                if isinstance(node.test, ast.Constant) and node.test.value is True and not has_break:
                    violations.append(f"Unbounded while True loop with no break detected at line {node.lineno}")

        is_valid = len(violations) == 0
        return is_valid, violations
