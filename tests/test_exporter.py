import json
import subprocess
import unittest
from unittest.mock import patch

import mega_raid_exporter as exporter


def response(status="Success", description="", data=None):
    return {
        "Controllers": [
            {
                "Command Status": {"Status": status, "Description": description},
                "Response Data": data or {},
            }
        ]
    }


class Completed:
    returncode = 0
    stderr = ""

    def __init__(self, payload):
        self.stdout = json.dumps(payload)


class RunStorcliTests(unittest.TestCase):
    @patch("mega_raid_exporter.subprocess.run")
    def test_rejects_failure_inside_successful_process(self, run):
        run.return_value = Completed(response("Failure", "no controller"))

        with self.assertRaisesRegex(exporter.ScrapeError, "status is Failure") as raised:
            exporter.run_storcli(["show"])

        self.assertEqual(raised.exception.reason, "storcli_status")

    @patch("mega_raid_exporter.subprocess.run")
    def test_classifies_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired("storcli", 20)

        with self.assertRaises(exporter.ScrapeError) as raised:
            exporter.run_storcli(["show"])

        self.assertEqual(raised.exception.reason, "timeout")


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.overview = response(
            data={
                "System Overview": [
                    {"Ctl": 0, "Hlth": "Opt"},
                    {"Ctl": 1, "Hlth": "Opt"},
                ]
            }
        )
        self.details = response(
            data={
                "Basics": {"Product Name": "MegaRAID", "Serial Number": "serial"},
                "VD LIST": [{"DG/VD": "0/0", "State": "Optl"}],
                "PD LIST": [{"EID:Slt": "1:0", "State": "Onln"}],
            }
        )

    def collect(self, command):
        with patch.object(exporter, "ensure_ioctl_node", return_value=(True, "")), patch.object(
            exporter, "run_storcli", side_effect=command
        ):
            return exporter.collect_metrics_locked()

    def test_success_has_one_unlabelled_up_metric(self):
        body, success = self.collect(lambda args: (self.overview if args == ["show"] else self.details, 0.01))

        self.assertTrue(success)
        self.assertEqual([line for line in body.splitlines() if line.startswith("megaraid_exporter_up")], ["megaraid_exporter_up 1"])

    def test_partial_failure_keeps_other_controller_and_reports_only_failure_up(self):
        def command(args):
            if args == ["show"]:
                return self.overview, 0.01
            if args[0] == "/c0":
                raise exporter.ScrapeError("command_failed", "failed")
            return self.details, 0.01

        body, success = self.collect(command)

        self.assertFalse(success)
        self.assertEqual([line for line in body.splitlines() if line.startswith("megaraid_exporter_up")], ["megaraid_exporter_up 0"])
        self.assertIn('megaraid_controller_scrape_success{controller="0"} 0', body)
        self.assertIn('megaraid_controller_scrape_success{controller="1"} 1', body)
        self.assertEqual(body.count('megaraid_exporter_scrape_error{reason="command_failed"} 1'), 1)

    def test_device_error_does_not_expose_message_as_label(self):
        with patch.object(exporter, "ensure_ioctl_node", return_value=(False, "/secret/path missing")):
            body, success = exporter.collect_metrics_locked()

        self.assertFalse(success)
        self.assertNotIn("/secret/path", body)
        self.assertIn('megaraid_exporter_scrape_error{reason="device_unavailable"} 1', body)

    def test_no_controllers_is_a_failed_scrape(self):
        empty_overview = response(data={"Number of Controllers": 0})

        body, success = self.collect(lambda args: (empty_overview, 0.01))

        self.assertFalse(success)
        self.assertIn('megaraid_exporter_scrape_error{reason="no_controllers"} 1', body)
        self.assertIn("megaraid_exporter_up 0", body)


if __name__ == "__main__":
    unittest.main()
