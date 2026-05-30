from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "app.py"
INDEX = ROOT / "remote-root" / "etc" / "mihomo" / "manager" / "templates" / "index.html"


def app_source():
    return APP.read_text(encoding="utf-8")


def index_source():
    return INDEX.read_text(encoding="utf-8")


class MihomoOverviewDashboardContractTest(unittest.TestCase):
    def test_backend_overview_contract_exists(self):
        text = app_source()

        self.assertIn("def mihomo_api_get", text)
        self.assertIn("def mihomo_controller_settings", text)
        self.assertIn("def collect_overview", text)
        self.assertIn("@app.route('/api/overview')", text)
        self.assertIn("connections_count", text)
        self.assertIn("download_total", text)
        self.assertIn("upload_total", text)
        self.assertIn("proxy_groups", text)
        self.assertIn("log_levels", text)

    def test_dashboard_ui_contract_exists(self):
        text = index_source()

        self.assertIn("ops-shell", text)
        self.assertIn("metricServiceState", text)
        self.assertIn("metricConnections", text)
        self.assertIn("metricDownload", text)
        self.assertIn("metricUpload", text)
        self.assertIn("proxyHealthList", text)
        self.assertIn("logLevelStrip", text)
        self.assertIn("loadOverview", text)
        self.assertIn("renderOverview", text)
        self.assertIn("overviewInterval", text)


if __name__ == "__main__":
    unittest.main()
