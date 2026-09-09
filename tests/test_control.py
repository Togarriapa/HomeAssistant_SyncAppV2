import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from ha_syncapp.control import Control, make_server


@contextmanager
def server(control: Control, allowed: str = "127.0.0.1") -> Iterator[str]:
    http = make_server(control, ("127.0.0.1", 0), allowed_peer=allowed)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_ingress_peer_and_csrf_are_required_before_enqueuing_actions() -> None:
    control = Control()
    with server(control, allowed="172.30.32.2") as url:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url)
        assert error.value.code == 403
    with server(control) as url:
        status = json.load(urllib.request.urlopen(url + "/status"))
        assert "csrf" in status
        req = urllib.request.Request(
            url + "/action",
            data=b'{"action":"generate"}',
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        assert error.value.code == 403
        assert control.actions.empty()
        req.add_header("X-SyncApp-CSRF", status["csrf"])
        response = urllib.request.urlopen(req)
        assert response.status == 202
        assert control.actions.get_nowait()["action"] == "generate"


def test_status_never_exposes_internal_payloads_or_script_markup() -> None:
    control = Control()
    control.publish({"initialized": False, "repository": "owner/repo"})
    with server(control) as url:
        response = urllib.request.urlopen(url)
        page = response.read().decode()
        assert "Initialize Repo" in page
        assert "Refresh Key" in page
        assert "Generate Key" in page
        assert "private_key" not in page
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert urllib.request.urlopen(url + "/app.js").status == 200
