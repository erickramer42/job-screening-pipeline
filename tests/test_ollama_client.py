"""Tests for ollama_client.py: repairing and validating the model's
answers, retries, error handling, and context counting. Ollama is never
called: urlopen or ollama_chat is mocked. Run from the repo root with:
python -m unittest
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

import ollama_client as client

class Responses(unittest.TestCase):
    def test_zero_based_indices_are_kept(self):
        rows = [(0, 70, "a"), (1, 40, "b")]
        self.assertEqual(client.align_indices(rows, 2),
                         {0: {"score": 70, "reason": "a"},
                          1: {"score": 40, "reason": "b"}})

    def test_one_based_indices_are_shifted(self):
        self.assertEqual(sorted(client.align_indices([(1, 70, "a"), (2, 40, "b")],
                                                 2)), [0, 1])

    def test_ambiguous_numbering_is_rejected(self):
        with self.assertRaises(ValueError):
            client.align_indices([(1, 70, "a")], 3)

    def test_out_of_range_indices_are_dropped(self):
        self.assertEqual(list(client.align_indices([(0, 1, ""), (5, 2, "")], 2)),
                         [0])

    def test_truncated_response_is_salvaged(self):
        text = ('{"scores": [{"index": 0, "score": 70, "reason": "ok"}, '
                '{"index": 1, "score": 6')
        with mock.patch("builtins.print"):
            self.assertEqual(client.parse_scores(text, 2, 1),
                             {0: {"score": 70, "reason": "ok"}})

    def test_salvage_never_invents_an_entry(self):
        with self.assertRaises(ValueError):
            client.salvage_scores('{"scores": [{"index": 0, "reason": "a } b')

    def test_unusable_responses(self):
        for text in ("", "   ", "[1, 2]", '{"scores": null}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                client.parse_scores(text, 1, 1)

    def test_scores_are_clamped_and_bad_entries_dropped(self):
        text = ('{"scores": [{"index": 0, "score": 150, "reason": "x"}, '
                '{"index": 1, "score": "high"}, "junk"]}')
        self.assertEqual(client.parse_scores(text, 2, 1),
                         {0: {"score": 100, "reason": "x"}})

    def test_a_failed_retry_keeps_the_earlier_partial_result(self):
        answers = iter([json.dumps({"scores": [
            {"index": 0, "score": 70, "reason": "a"}]}), TimeoutError()])
        def chat(system, user, attempt=1):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(client, "ollama_chat", chat), \
                mock.patch("builtins.print"):
            self.assertEqual(client.request_scores("s", "u", [{}, {}]),
                             {0: {"score": 70, "reason": "a", "attempt": 1}})

    def test_the_answering_attempt_is_recorded(self):
        answers = iter(["not json", json.dumps({"scores": [
            {"index": 0, "score": 55, "reason": "b"}]})])
        with mock.patch.object(client, "ollama_chat",
                               lambda system, user, attempt=1: next(answers)), \
                mock.patch("builtins.print"):
            self.assertEqual(client.request_scores("s", "u", [{}]),
                             {0: {"score": 55, "reason": "b", "attempt": 2}})

    def test_a_failed_first_attempt_fails_the_batch(self):
        with mock.patch.object(client, "ollama_chat", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                client.request_scores("s", "u", [{}])

    def http_error(self, code, reason, body):
        return urllib.error.HTTPError("http://localhost:11434/api/chat",
                                      code, reason, {}, io.BytesIO(body))

    def test_a_4xx_is_rejected_with_ollamas_reason(self):
        error = self.http_error(
            400, "Bad Request",
            b'{"error": "\\"gemma\\" does not support thinking"}')
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(client.RequestRejected) as cm:
            client.ollama_chat("s", "u")
        self.assertEqual(str(cm.exception), 'HTTP 400 Bad Request: "gemma" '
                                            'does not support thinking')

    def test_a_5xx_is_a_transport_error_with_ollamas_reason(self):
        error = self.http_error(500, "Internal Server Error",
                                b'{"error": "model requires more memory"}')
        with mock.patch("urllib.request.urlopen", side_effect=error), \
                self.assertRaises(client.TRANSPORT_ERRORS) as cm:
            client.ollama_chat("s", "u")
        self.assertNotIsInstance(cm.exception, client.RequestRejected)
        self.assertIn("model requires more memory", str(cm.exception))

    def test_model_available(self):
        tags = {"models": [{"name": "gemma4:12b"}, {"name": "other:latest"}]}
        self.assertTrue(client.model_available(tags, "gemma4:12b"))
        self.assertTrue(client.model_available(tags, "other"))
        self.assertFalse(client.model_available(tags, "gemma4"))
        self.assertFalse(client.model_available({}, "gemma4:12b"))


class ContextCount(unittest.TestCase):
    def test_a_request_records_its_prompt_and_response_tokens(self):
        body = json.dumps({"message": {"content": "{}"},
                           "prompt_eval_count": 2118, "eval_count": 46})
        with mock.patch.object(client, "CONTEXT_USED", 0), \
                mock.patch("urllib.request.urlopen",
                           return_value=io.BytesIO(body.encode())):
            client.ollama_chat("s", "u")
            self.assertEqual(client.CONTEXT_USED, 2164)

    def test_a_response_without_counts_records_nothing(self):
        body = json.dumps({"message": {"content": "{}"}})
        with mock.patch.object(client, "CONTEXT_USED", 0), \
                mock.patch("urllib.request.urlopen",
                           return_value=io.BytesIO(body.encode())):
            client.ollama_chat("s", "u")
            self.assertEqual(client.CONTEXT_USED, 0)

if __name__ == "__main__":
    unittest.main()
