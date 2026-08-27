import ast
import unittest
from pathlib import Path


class PublicDemoPrivacyTests(unittest.TestCase):
    def test_public_demo_uses_allowlist_not_source_copy(self):
        source = Path("app.py").read_text()
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_public_demo_store"
        )
        text = ast.get_source_segment(source, function)
        self.assertIn("clean = _store_default()", text)
        self.assertNotIn("clean = dict(store", text)
        self.assertIn("Everything", text)


if __name__ == "__main__":
    unittest.main()
