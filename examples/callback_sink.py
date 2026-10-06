#!/usr/bin/env python3
"""示例回调接收端：接收违规事件通知，验证 X-Signature 签名并打印。

仅依赖标准库。用法：

    SECRET=demo-secret-123 PORT=9000 python3 examples/callback_sink.py

- 共享密钥须与订阅登记时的 ``secret`` 一致；
- 签名校验：``X-Signature: sha256=<HMAC-SHA256(secret, 原始请求体) 的十六进制>``；
- 验签通过返回 200（投递成功），否则返回 401（服务会按失败重试）；
- 接收方应按 ``event_id`` 去重：失败重试沿用同一事件 ID（至少一次投递语义）。
"""

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.getenv("SECRET", "demo-secret-123")
PORT = int(os.getenv("PORT", "9000"))

seen_event_ids: set[str] = set()  # 按事件 ID 去重（重试沿用同一 ID）


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        signature = self.headers.get("X-Signature", "")
        expected = "sha256=" + hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            print(f"!! 签名校验失败: {signature!r} != {expected!r}")
            self.send_response(401)
            self.end_headers()
            return
        event = json.loads(body)
        duplicate = event["event_id"] in seen_event_ids
        seen_event_ids.add(event["event_id"])
        print(
            f"收到事件 event_id={event['event_id']} 司机={event['driver_id']} "
            f"规则 {event['rules_before']} -> {event['rules_after']} "
            f"提交时刻={event['committed_at']} 重复投递={duplicate}"
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # 静默默认访问日志
        pass


if __name__ == "__main__":
    print(f"回调接收端监听 0.0.0.0:{PORT}，共享密钥已加载（{len(SECRET)} 字符）")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
