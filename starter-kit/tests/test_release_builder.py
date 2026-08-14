from __future__ import annotations
import importlib.util, tempfile, unittest, zipfile
from pathlib import Path
SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build-release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT); assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)
class ReleaseBuilderTests(unittest.TestCase):
    def test_allowlist_excludes_internal_development_assets(self) -> None:
        paths = [relative.as_posix() for _path, relative in MODULE.release_files()]
        self.assertIn("bb", paths); self.assertIn("agents/example-agent/agent.yaml", paths)
        self.assertIn("AGENTS.md", paths); self.assertIn("CLAUDE.md", paths)
        self.assertTrue(any(path.startswith("example-cases/") for path in paths))
        for path in paths:
            self.assertFalse(path.startswith(("docs/", "tests/", "tools/", "runs/", "dist/")))
            self.assertNotIn("milestone", path.lower())
    def test_zip_is_deterministic(self) -> None:
        files = MODULE.release_files(); MODULE.validate_source(files)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"; second = Path(directory) / "second.zip"
            MODULE.write_zip(first, "starter-kit", files); MODULE.write_zip(second, "starter-kit", files)
            self.assertEqual(MODULE.sha256(first), MODULE.sha256(second))
            with zipfile.ZipFile(first) as archive: names = archive.namelist()
            self.assertFalse(any("milestone" in name.lower() for name in names))
if __name__ == "__main__": unittest.main()
