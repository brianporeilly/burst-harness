"""view_image (harness/images.py): the Strands engine looking at a screenshot, given to a run whose model takes images,
and the image reaching llama.cpp in a user message rather than a tool message."""

import http.server
import json
import threading

import pytest

from harness import images, transcript
from harness.config import HarnessConfig
from harness.exec_target import LocalTarget
from harness.models import build_model, images_out_of_tool_messages

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.parametrize("data, kind", [
    (PNG, "png"), (b"\xff\xd8\xff\xe0rest", "jpeg"), (b"GIF89a....", "gif"), (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
    (b"%PDF-1.7", None), (b"", None),
])
def test_the_format_is_read_from_the_files_first_bytes(data, kind):
    assert images.image_format(data) == kind


def test_an_image_is_read_from_where_the_agent_works_or_refused_saying_why(tmp_path, monkeypatch):
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "after tap.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("not a picture")
    target = LocalTarget(repo=str(tmp_path))

    seen = images.read_image(target, "shots/after tap.png")
    assert seen["status"] == "success"
    assert seen["content"][0] == {"text": "shots/after tap.png (PNG, 0 KiB):"}
    assert seen["content"][1] == {"image": {"format": "png", "source": {"bytes": PNG}}}
    assert images.read_image(target, str(tmp_path / "shots" / "after tap.png"))["status"] == "success"  # absolute

    assert images.read_image(target, "gone.png") == {"status": "error", "content": [{"text": "No file at gone.png."}]}
    assert "is not a PNG, JPEG, GIF or WebP image" in images.read_image(target, "notes.txt")["content"][0]["text"]
    monkeypatch.setattr(images, "MAX_BYTES", 16)
    too_big = images.read_image(target, "shots/after tap.png")["content"][0]["text"]
    assert too_big.startswith("shots/after tap.png is 0.0 MiB, over the 0 MiB") and "a smaller --size" in too_big


def test_the_tool_hands_strands_its_result_as_it_is(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    view = images.view_image_tool(LocalTarget(repo=str(tmp_path)))
    assert view.tool_name == "view_image" and "web-shot or window-shot" in view.tool_spec["description"]
    assert view._tool_func("a.png")["content"][1]["image"]["source"]["bytes"] == PNG


def _props_server(props):
    class Props(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(props).encode() if self.path == "/props" else b"{}"
            self.send_response(200 if self.path == "/props" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Props)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_whether_the_model_takes_images(monkeypatch):
    monkeypatch.setattr(images, "_ASKED", {})
    assert images.sees_images(HarnessConfig(provider="anthropic"))
    assert not images.sees_images(HarnessConfig(provider="openai"))
    assert images.sees_images(HarnessConfig(provider="openai", vision="yes"))
    assert not images.sees_images(HarnessConfig(provider="anthropic", vision="no"))

    with_projector = _props_server({"modalities": {"vision": True, "audio": False}})
    without = _props_server({"modalities": {"vision": False}})
    old = _props_server({"n_ctx": 4096})  # a llama-server from before modalities
    try:
        for server, sees in ((with_projector, True), (without, False), (old, False)):
            url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            assert images.sees_images(HarnessConfig(provider="llamacpp", base_url=url)) is sees
        server = with_projector
        server.shutdown()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert images.sees_images(HarnessConfig(provider="llamacpp", base_url=url))  # asked a moment ago
        monkeypatch.setattr(images, "_ASKED", {})
        assert not images.sees_images(HarnessConfig(provider="llamacpp", base_url=url))  # nothing answers
    finally:
        for server in (without, old):
            server.shutdown()


def test_llama_cpp_gets_a_tool_results_image_in_a_user_message_after_the_results():
    model = build_model(HarnessConfig(provider="llamacpp", base_url="http://llm.example:8080", model_id="m"))
    request = model._format_request([
        {"role": "user", "content": [{"text": "look"}]},
        {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "a", "name": "view_image", "input": {"path": "a.png"}}},
            {"toolUse": {"toolUseId": "b", "name": "shell", "input": {"command": "ls"}}}]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "a", "status": "success", "content": [
                {"text": "a.png (PNG, 0 KiB):"}, {"image": {"format": "png", "source": {"bytes": PNG}}}]}},
            {"toolResult": {"toolUseId": "b", "status": "success", "content": [{"text": "a.png"}]}}]},
    ])
    roles = [m["role"] for m in request["messages"]]
    assert roles == ["user", "assistant", "tool", "tool", "user"]
    first_result = request["messages"][2]["content"]
    assert [part["type"] for part in first_result] == ["text", "text"]
    assert first_result[1]["text"] == "(1 image(s): in the user message after these results.)"
    [picture] = request["messages"][4]["content"]
    assert picture["type"] == "image_url" and picture["image_url"]["url"].startswith("data:image/png;base64,")


def test_messages_without_images_are_left_as_they_are():
    messages = [{"role": "tool", "tool_call_id": "a", "content": [{"type": "text", "text": "ok"}]},
                {"role": "assistant", "content": "done"}]
    assert images_out_of_tool_messages(messages) == messages


def test_a_transcript_holds_an_images_size_not_its_bytes():
    assert transcript._unwritable(PNG) == "<72 bytes>" and transcript._unwritable(3.5) == "3.5"
