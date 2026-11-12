import json
from pathlib import Path
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RemoteError(Exception):
    def __init__(self, service, status="network_or_invalid_response", uncertain=False):
        self.uncertain = uncertain
        super().__init__(f"{service}: {status}")


def post_json(url, payload, headers=None, timeout=60, service="remote", sending=False):
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        raise RemoteError(service, f"HTTP {error.code}", sending and error.code >= 500) from None
    except (URLError, OSError, ValueError):
        # Exception strings may contain a Telegram token or authentication headers.
        raise RemoteError(service, uncertain=sending) from None


class Telegram:
    def __init__(self, token, owner, expected_username):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.owner = owner
        self.expected_username = expected_username

    def call(self, method, payload=None, sending=False):
        data = post_json(self.base + method, payload or {}, service="Telegram", sending=sending)
        if not isinstance(data, dict) or not data.get("ok"):
            raise RemoteError("Telegram", "rejected_or_invalid", uncertain=sending)
        return data["result"]

    def verify(self):
        if not self.expected_username or self.call("getMe").get("username", "").lower() != self.expected_username.lower():
            raise RemoteError("Telegram", "unexpected_bot")
        chat = self.call("getChat", {"chat_id": self.owner})
        if chat.get("id") != self.owner or chat.get("type") != "private":
            raise RemoteError("Telegram", "unexpected_recipient")
        if self.call("getWebhookInfo").get("url"):
            raise RemoteError("Telegram", "webhook_exists_no_changes_made")

    def updates(self, offset):
        return self.call("getUpdates", {"offset": offset, "timeout": 10, "allowed_updates": ["message"]})

    def send(self, text):
        return self.call("sendMessage", {"chat_id": self.owner, "text": text}, sending=True)["message_id"]

    def send_file(self, file_path, caption):
        path = Path(file_path)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise RemoteError("Telegram", "file_too_large")
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("chat_id", str(self.owner)), ("caption", caption)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        # Strip characters that could alter the multipart headers.
        filename = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_')
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        parts.extend([path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
        request = Request(self.base + "sendDocument", data=b"".join(parts),
                          headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
        try:
            with urlopen(request, timeout=90) as response:
                data = json.load(response)
            if not data.get("ok"):
                raise RemoteError("Telegram", "file_rejected")
            return data["result"]["message_id"]
        except HTTPError as error:
            raise RemoteError("Telegram", f"HTTP {error.code}", error.code >= 500) from None
        except (URLError, OSError, ValueError, KeyError, TypeError):
            raise RemoteError("Telegram", "file_delivery_unknown", uncertain=True) from None


class OpenRouter:
    def __init__(self, key, model):
        self.key, self.model = key, model

    def chat(self, messages, tools=None):
        payload = {"model": self.model, "messages": messages, "max_tokens": 1800}
        if tools:
            payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        data = post_json("https://openrouter.ai/api/v1/chat/completions", payload,
                         {"Authorization": "Bearer " + self.key}, timeout=90, service="OpenRouter")
        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError
            return message
        except (KeyError, IndexError, TypeError, ValueError):
            raise RemoteError("OpenRouter", "invalid_completion") from None
