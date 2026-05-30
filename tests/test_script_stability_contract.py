from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "remote-root" / "etc" / "mihomo" / "scripts"


def source(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


class MihomoScriptStabilityContractTest(unittest.TestCase):
    def test_geo_update_downloads_to_private_temp_dir_before_replace(self):
        text = source("update_geo.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn('GEOIP_TMP="${TMP_DIR}/geoip.dat"', text)
        self.assertIn('GEOSITE_TMP="${TMP_DIR}/geosite.dat"', text)
        self.assertIn('wget --no-check-certificate -O "$GEOIP_TMP"', text)
        self.assertIn('wget --no-check-certificate -O "$GEOSITE_TMP"', text)
        self.assertIn('mv "$GEOIP_TMP" "${GEO_DIR}/geoip.dat"', text)
        self.assertIn('mv "$GEOSITE_TMP" "${GEO_DIR}/geosite.dat"', text)
        self.assertNotIn('${GEO_DIR}/geoip.dat.new', text)
        self.assertNotIn('${GEO_DIR}/geosite.dat.new', text)

    def test_cron_manager_uses_fixed_string_filtering_and_temp_files(self):
        text = source("cron_manager.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("remove_matching_cron()", text)
        self.assertIn('grep -F -v -- "$pattern"', text)
        self.assertIn('crontab "$TMP_CRON"', text)
        self.assertNotIn('grep -v "$comment"', text)
        self.assertNotIn('(crontab -l 2>/dev/null; echo', text)

    def test_gateway_init_writes_crontab_through_temp_file(self):
        text = source("gateway_init.sh")

        self.assertIn('TMP_DIR="$(mktemp -d)"', text)
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn('TMP_CRON="${TMP_DIR}/gateway_crontab"', text)
        self.assertIn('grep -F -v -- "gateway_init.sh check"', text)
        self.assertIn('crontab "$TMP_CRON"', text)
        self.assertNotIn('(crontab -l 2>/dev/null; echo', text)


if __name__ == "__main__":
    unittest.main()
