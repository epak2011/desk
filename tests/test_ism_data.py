import unittest
from datetime import date
from unittest import mock

import ism_data


REPORT = """
<h1>Manufacturing PMI<sup>&reg;</sup> at 54.6%</h1>
<h1>August 2026 ISM<sup>&reg;</sup> Manufacturing PMI<sup>&reg;</sup> Report</h1>
<p>The Manufacturing PMI<sup>&reg;</sup> registered 54.6 percent in August,
1 percentage point below the July figure of 55.6 percent.</p>
"""


class IsmDataTests(unittest.TestCase):
    def test_parses_current_and_previous_official_readings(self):
        self.assertEqual(
            ism_data.parse_manufacturing_pmi(REPORT),
            [
                {"date": "2026-07-01", "value": 55.6},
                {"date": "2026-08-01", "value": 54.6},
            ],
        )

    @mock.patch("ism_data.urllib.request.urlopen")
    def test_fetches_previous_month_report_first(self, urlopen):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = REPORT.encode()
        urlopen.return_value = response

        rows = ism_data.manufacturing_pmi_rows(today=date(2026, 9, 21))

        self.assertEqual(rows[-1]["value"], 54.6)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/august/"))


if __name__ == "__main__":
    unittest.main()
