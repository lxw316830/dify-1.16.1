"""Offline contract checks; no live credentials or remote requests required."""
import argparse
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import run
import workflow_client


class Stream(io.BytesIO):
    headers = {"Content-Type": "text/event-stream"}


class WorkflowTests(unittest.TestCase):
    def api(self, name="", extra=None):
        return run.ApiClient(argparse.Namespace(api_base="http://localhost/v1", api_key="test-key",
                             input_name=name, user="test-user", timeout=10), extra or {})

    def parameters(self, kind="file"):
        return {"user_input_form": [{kind: {"variable": "document", "required": True}}]}

    def test_single_and_list_upload_stream_and_saved_report(self):
        for kind in ("file", "file-list"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "input.docx"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("word/document.xml", "<document/>")
                    archive.writestr("[Content_Types].xml", "<Types/>")
                api = self.api(extra={"region": "test"})
                with patch.object(api, "_request_json", return_value=self.parameters(kind)):
                    api.prepare()
                event = {"event": "workflow_finished", "workflow_run_id": "run-1", "data": {
                    "status": "succeeded", "outputs": {"report": "original workflow report"}}}
                stream = Stream(("data: " + json.dumps(event) + "\n\n").encode())
                with patch.object(api, "prepare"), patch.object(api, "_upload", return_value="uploaded-1"), \
                     patch.object(workflow_client, "build_client", return_value=api), \
                     patch("urllib.request.urlopen", return_value=stream) as request:
                    code = workflow_client.main(["--input", str(source), "--output", str(root / "reports")])
                self.assertEqual(code, 0)
                self.assertEqual((root / "reports/input.md").read_text(encoding="utf-8"), "original workflow report")
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, "http://localhost/v1/workflows/run")
                payload = json.loads(sent.data)
                value = payload["inputs"]["document"]
                self.assertEqual(isinstance(value, list), kind == "file-list")
                self.assertEqual((value[0] if isinstance(value, list) else value)["upload_file_id"], "uploaded-1")
                self.assertEqual(payload["inputs"]["region"], "test")
                self.assertEqual(payload["user"], "test-user")

    def test_ambiguous_and_missing_required_inputs_fail_before_upload(self):
        api = self.api()
        parameters = self.parameters()
        parameters["user_input_form"].append({"file": {"variable": "other"}})
        with patch.object(api, "_request_json", return_value=parameters):
            with self.assertRaisesRegex(RuntimeError, "唯一文件"):
                api.prepare()
        parameters = self.parameters()
        parameters["user_input_form"].append({"text-input": {"variable": "region", "required": True}})
        with patch.object(api, "_request_json", return_value=parameters):
            with self.assertRaisesRegex(RuntimeError, "region"):
                api.prepare()

    def test_incomplete_stream_is_not_success(self):
        api = self.api()
        with patch("urllib.request.urlopen", return_value=Stream(b'data: {"event":"ping"}\n\n')):
            with self.assertRaisesRegex(RuntimeError, "没有收到完成事件"):
                api._read_workflow_stream("http://localhost/v1/workflows/run", {}, b"{}")


if __name__ == "__main__":
    unittest.main()
