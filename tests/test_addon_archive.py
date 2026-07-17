from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.build_addon_zip import build_archive, validate_package


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "libsm64_studio"


def expected_archive_files():
    return {
        source.relative_to(ROOT).as_posix(): source
        for source in PACKAGE.rglob("*")
        if source.is_file()
        and "__pycache__" not in source.parts
        and source.suffix not in {".pyc", ".pyo"}
    }


class AddonArchiveTests(unittest.TestCase):
    def assert_archive_matches_package(self, archive_path):
        expected = expected_archive_files()
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, set(expected))
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertFalse(any(name.endswith((".pyc", ".pyo")) for name in names))
            for name, source in expected.items():
                self.assertEqual(
                    archive.read(name), source.read_bytes(),
                    "Archive contains a stale {}".format(name),
                )

    def test_package_validation_checks_runtime_mirrors_and_exports(self):
        validate_package(ROOT)

    def test_fresh_archive_matches_package_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "libsm64_studio.zip"
            build_archive(ROOT, archive_path)
            self.assert_archive_matches_package(archive_path)

if __name__ == "__main__":
    unittest.main()
