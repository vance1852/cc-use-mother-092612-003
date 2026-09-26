"""提供体验安全记录模块的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from night_market_foundation.api import route as foundation_route
from night_market_foundation.errors import DomainError, ValidationError
from night_market_foundation.storage import Database

from .service import ExperienceSafetyService


def route(service: ExperienceSafetyService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把安全记录请求分派到领域服务，未命中时回退到基础路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and parsed.path == "/safety/protocols":
            receipt = service.register_protocol(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/practitioners":
            receipt = service.register_practitioner(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/participants":
            receipt = service.register_participant(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/screenings":
            receipt = service.register_screening(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/equipment":
            receipt = service.register_equipment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/checkouts":
            receipt = service.issue_equipment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/checkouts/return":
            receipt = service.return_equipment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/sessions":
            receipt = service.start_session(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/sessions/facts":
            receipt = service.append_fact(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/sessions/completion":
            receipt = service.complete_session(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/incidents":
            result = service.report_incident(actor_id=actor_id, **body)
            return (200 if result["replayed"] else 201), result
        if method == "POST" and parsed.path == "/safety/reviews/resolutions":
            receipt = service.resolve_review_item(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/safety/suspensions/lift":
            receipt = service.lift_suspension(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/safety/sessions":
            query = parse_qs(parsed.query)
            session_id = query.get("session_id", [""])[0]
            if not session_id:
                raise ValidationError("session_id 不能为空")
            return 200, service.get_session(session_id).__dict__
        if method == "GET" and parsed.path == "/safety/sessions/facts":
            query = parse_qs(parsed.query)
            session_id = query.get("session_id", [""])[0]
            if not session_id:
                raise ValidationError("session_id 不能为空")
            return 200, {"items": [item.__dict__ for item in service.list_facts(session_id)]}
        if method == "GET" and parsed.path == "/safety/sessions/trace":
            query = parse_qs(parsed.query)
            session_id = query.get("session_id", [""])[0]
            if not session_id:
                raise ValidationError("session_id 不能为空")
            return 200, service.trace_session(session_id)
        if method == "GET" and parsed.path == "/safety/suspensions":
            query = parse_qs(parsed.query)
            status = query.get("status", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_suspensions(status)]}
        if method == "GET" and parsed.path == "/safety/review-items":
            query = parse_qs(parsed.query)
            status = query.get("status", [None])[0]
            suspension_id = query.get("suspension_id", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_review_items(status, suspension_id)]}
        return foundation_route(service, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为安全记录路由调用。"""

    service: ExperienceSafetyService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动体验安全记录 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动适宜技术体验安全记录服务")
    parser.add_argument("--database", default="safety.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = ExperienceSafetyService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
