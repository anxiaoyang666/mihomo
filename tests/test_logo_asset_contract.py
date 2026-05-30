from pathlib import Path
import struct
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
LOGO = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "static" / "logo.png"


def text(path):
    return path.read_text(encoding="utf-8")


def png_size(path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG file")
    return struct.unpack(">II", data[16:24])


class MihomoLogoAssetContractTest(unittest.TestCase):
    def test_panel_version_marks_logo_refresh_release(self):
        self.assertIn('PANEL_VERSION = "0.1.12"', text(APP))

    def test_logo_is_square_app_icon_png(self):
        width, height = png_size(LOGO)

        self.assertEqual(width, height)
        self.assertGreaterEqual(width, 512)
        self.assertLessEqual(width, 1024)
        self.assertLess(LOGO.stat().st_size, 700_000)


if __name__ == "__main__":
    unittest.main()
