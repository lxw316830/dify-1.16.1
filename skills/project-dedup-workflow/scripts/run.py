"""Portable API-only entrypoint for the project deduplication workflow."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import workflow_client as client


class ApiClient(client.WorkflowClient):
    def __init__(self, args, extra_inputs):
        super().__init__("", args.api_base, args.api_key, args.input_name, args.user, args.timeout)
        self.extra_inputs = extra_inputs
        self.file_list = False

    def prepare(self):
        self.base_url = self.api_base.removesuffix("/v1")
        parameters = self._request_json("GET", f"{self.base_url}/v1/parameters", self._api_headers())
        fields = []
        for item in parameters.get("user_input_form") or []:
            for kind, field in item.items():
                if isinstance(field, dict):
                    fields.append((field.get("type") or kind, field))
        files = [(kind, field) for kind, field in fields if kind in ("file", "file-list")]
        if self.input_name:
            files = [(kind, field) for kind, field in files if field.get("variable") == self.input_name]
        if len(files) != 1:
            raise RuntimeError("需要唯一文件输入；请用 --input-name 指定开始节点的 file/file-list 变量")
        kind, field = files[0]
        self.input_name = field["variable"]
        self.file_list = kind == "file-list"
        if self.input_name in self.extra_inputs:
            raise RuntimeError("额外输入不能覆盖文件变量")
        missing = [field.get("variable") for _, field in fields
                   if field.get("required") and field.get("variable") != self.input_name
                   and field.get("variable") not in self.extra_inputs
                   and field.get("default") in (None, "")]
        if missing:
            raise RuntimeError(f"缺少必填输入 {missing}，请通过 --inputs-json 提供")

    def run(self, docx_path):
        file_id = self._upload(docx_path)
        file_value = {"transfer_method": "local_file", "upload_file_id": file_id, "type": "document"}
        inputs = dict(self.extra_inputs)
        inputs[self.input_name] = [file_value] if self.file_list else file_value
        body = json.dumps({"inputs": inputs, "response_mode": "streaming", "user": self.user},
                          ensure_ascii=False).encode("utf-8")
        return self._read_workflow_stream(f"{self.base_url}/v1/workflows/run",
                                         self._api_headers({"Content-Type": "application/json"}), body)


def main(argv=None):
    parser = argparse.ArgumentParser(description="通过 Dify API 对单份可研或文件夹执行项目查重")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/workflow.json")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--api-base")
    parser.add_argument("--input-name")
    parser.add_argument("--user")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--inputs-json", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8-sig"))
        for key, default in (("api_base", ""), ("input_name", ""), ("user", "project-dedup-skill"), ("timeout", 3600)):
            if getattr(args, key) is None:
                setattr(args, key, config.get(key, default))
        args.api_base = args.api_base.rstrip("/")
        if not args.api_base.startswith(("http://", "https://")):
            raise ValueError("api_base 必须是 http(s) API 地址")
        args.api_key = os.environ.get(config.get("api_key_env", "DIFY_API_KEY"), "").strip()
        if not args.api_key:
            raise ValueError(f"请设置应用密钥环境变量 {config.get('api_key_env', 'DIFY_API_KEY')}")
        extra = json.loads(args.inputs_json.read_text(encoding="utf-8-sig")) if args.inputs_json else {}
        if not isinstance(extra, dict):
            raise ValueError("--inputs-json 内容必须是 JSON 对象")
        api = ApiClient(args, extra)
        api.prepare()
        if args.check:
            print(f"API 连接正常；文件变量：{api.input_name}；类型：{'file-list' if api.file_list else 'file'}")
            return 0
        if args.input is None:
            parser.error("执行时需要 --input 文件或目录")
        forwarded = ["--input", str(args.input), "--limit", str(args.limit)]
        if args.output:
            forwarded += ["--output", str(args.output)]
        client.build_client = lambda _: api
        # The API parameters were already checked before document conversion/upload.
        api.prepare = lambda: None
        return client.main(forwarded)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"调用失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
