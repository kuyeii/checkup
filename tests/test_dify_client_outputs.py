import unittest

from src.dify_client import DifyWorkflowError, extract_blocking_outputs


class DifyClientOutputsTests(unittest.TestCase):
    def test_extract_outputs_raises_on_failed_status(self):
        payload = {
            "data": {
                "status": "failed",
                "error": "upstream failure",
            }
        }
        with self.assertRaises(DifyWorkflowError):
            extract_blocking_outputs(payload)

    def test_extract_outputs_success(self):
        payload = {"data": {"status": "succeeded", "outputs": {"risk_items": []}}}
        out = extract_blocking_outputs(payload)
        self.assertEqual(out, {"risk_items": []})


if __name__ == "__main__":
    unittest.main()
