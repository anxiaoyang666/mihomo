"""订阅与任务页：定时任务的时间按浏览器本地时间填写/显示，保存时换算成服务器时间（容器是 UTC）。"""
import unittest

from test_auto_update_card import extract, index_text, run_node

FUNCS = (
    r"(?ms)^    function pad2\(.*?^    \}\n",
    r"(?ms)^    function shiftHHMM\(.*?^    \}\n",
    r"(?ms)^    function serverTzDelta\(.*?^    \}\n",
    r"(?m)^    function serverToLocalHHMM\(.*$",
    r"(?m)^    function localToServerHHMM\(.*$",
    r"(?ms)^    function scheduleLabel\(.*?^    \}\n",
)
# 浏览器固定成北京时间（UTC+8），服务器是 UTC
PRELUDE = "Date.prototype.getTimezoneOffset = () => -480;\nlet serverTz = { offset_minutes: 0 };\n"


class ScheduleLocalTimeTest(unittest.TestCase):
    def js(self):
        return PRELUDE + extract(index_text(), *FUNCS)

    def test_conversions(self):
        out = run_node(self, self.js(), "[serverToLocalHHMM(c), localToServerHHMM(c)]", ["05:00", "20:30", "00:00", "bad"])
        self.assertEqual(out, [["13:00", "21:00"], ["04:30", "12:30"], ["08:00", "16:00"], ["bad", "bad"]])

    def test_labels_are_local(self):
        out = run_node(self, self.js(), "scheduleLabel(c[0], c[1], '订阅')",
                       [["daily", "13:00"], ["every6h", "08:15"], ["every12h", "08:00"], ["advanced", ""]])
        self.assertEqual(out[0], "每天 13:00 自动更新订阅")
        self.assertEqual(out[1], "每 6 小时自动更新订阅（02:15、08:15、14:15、20:15）")
        self.assertEqual(out[2], "每 12 小时自动更新订阅（08:00、20:00）")
        self.assertIn("服务器时间", out[3])

    def test_wiring(self):
        html = index_text()
        self.assertIn("cron_sub_time: document.getElementById('cron_sub_time') ? localToServerHHMM(", html)
        self.assertIn("cron_geo_time: document.getElementById('cron_geo_time') ? localToServerHHMM(", html)
        self.assertIn("document.getElementById('cron_sub_time').value = serverToLocalHHMM(", html)
        self.assertIn("schedInput.value = scheduleToCron(mode, localToServerHHMM(timeInput.value)", html)
        self.assertIn("if (res.timezone) serverTz = res.timezone;", html)


if __name__ == "__main__":
    unittest.main()
