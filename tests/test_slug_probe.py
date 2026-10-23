"""Tests for slug_probe.py: slug guesses, and telling "no board" apart
from "couldn't ask". The network is mocked."""
import io
import json
import unittest
import urllib.error
from unittest import mock

import slug_probe

def answer(payload):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return resp

class SlugVariants(unittest.TestCase):
    def test_plain_name_first_and_corporate_words_only_as_words(self):
        self.assertEqual(slug_probe.slug_variants("Cisco")[0], "cisco")
        self.assertEqual(slug_probe.slug_variants("Costco")[0], "costco")
        self.assertIn("acme", slug_probe.slug_variants("Acme Technologies"))

class Probe(unittest.TestCase):
    def test_jobs_mean_a_board(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=answer({"jobs": [{"id": 1}]})):
            self.assertTrue(slug_probe.probe("acme", "greenhouse"))

    def test_no_jobs_or_a_4xx_mean_no_board(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=answer({"jobs": []})):
            self.assertFalse(slug_probe.probe("acme", "greenhouse"))
        not_found = urllib.error.HTTPError("u", 404, "Not Found", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=not_found):
            self.assertFalse(slug_probe.probe("acme", "greenhouse"))

    def test_an_outage_is_not_no_board(self):
        for error in (urllib.error.URLError("down"),
                      urllib.error.HTTPError("u", 503, "Unavailable", None,
                                             None),
                      TimeoutError()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(OSError):
                    slug_probe.probe("acme", "greenhouse")

if __name__ == "__main__":
    unittest.main()
