"""ログインゲート（AS_ALLOWED_EMAILS で任意有効化）のテスト。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AS_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="asset-summary-test-"), "t.db")
)

import pytest
from fastapi.testclient import TestClient

import asset_summary.web.app as web_app
from asset_summary.web import auth as as_auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AS_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("AS_SECRET_KEY", raising=False)
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    monkeypatch.delenv("CS_BASE_URL", raising=False)
    yield


def _auth_env(monkeypatch):
    monkeypatch.setenv("AS_ALLOWED_EMAILS", "me@example.com, other@example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")


# ---- 無効時（回帰ガード: 従来どおり全ルート開放） ----


def test_no_env_everything_open(tmp_path):
    client = TestClient(web_app.create_app(str(tmp_path / "t.db")))
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    # スタブが「認証オフ」を知らせる
    assert client.get("/auth/me").json() == {"authenticated": True, "enabled": False}


# ---- 有効時 ----


def test_gate_blocks_api_but_leaves_shell_open(tmp_path, monkeypatch):
    _auth_env(monkeypatch)
    client = TestClient(web_app.create_app(str(tmp_path / "t.db")))
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/meta").status_code == 401
    assert client.put("/api/settings", json={}).status_code == 401
    # 画面の骨組み・静的・死活監視・認証フローは通る
    assert client.get("/").status_code == 200
    assert client.get("/api/health").json() == {"status": "ok"}
    me = client.get("/auth/me").json()
    assert me == {"authenticated": False, "enabled": True}


def test_gate_missing_google_env_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("AS_ALLOWED_EMAILS", "me@example.com")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        web_app.create_app(str(tmp_path / "t.db"))


# ---- 純関数 ----


def test_email_allowed(monkeypatch):
    monkeypatch.setenv("AS_ALLOWED_EMAILS", " Me@Example.com , other@example.com ")
    assert as_auth.email_allowed("me@example.com") is True
    assert as_auth.email_allowed("ME@EXAMPLE.COM") is True
    assert as_auth.email_allowed("other@example.com") is True
    assert as_auth.email_allowed("evil@example.com") is False
    assert as_auth.email_allowed("") is False
    assert as_auth.email_allowed(None) is False


def test_https_only_follows_base_url(monkeypatch):
    monkeypatch.setenv("AS_BASE_URL", "https://as.example.com")
    assert as_auth.https_only() is True
    monkeypatch.setenv("AS_BASE_URL", "http://localhost:8010")
    assert as_auth.https_only() is False


def test_session_secret_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AS_SECRET_KEY", "fixed-key")
    assert as_auth.session_secret(str(tmp_path)) == "fixed-key"
    assert not (tmp_path / "_session_key").exists()


def test_session_secret_generated_and_reused(tmp_path):
    k1 = as_auth.session_secret(str(tmp_path))
    assert (tmp_path / "_session_key").read_text(encoding="utf-8").strip() == k1
    assert as_auth.session_secret(str(tmp_path)) == k1  # 再起動相当でも同じ鍵


# ---- 公開 URL をリクエストから決める ----


def _req(host: str, scheme: str = "http"):
    """指定した Host ヘッダで来たリクエスト。"""
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": "/auth/login",
        "scheme": scheme, "query_string": b"", "root_path": "",
        "headers": [(b"host", host.encode())],
        "server": (host.split(":")[0], int(host.split(":")[1]) if ":" in host else 80),
    })


def test_base_url_derived_from_host_when_unrestricted(monkeypatch):
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    assert as_auth.resolve_base_url(_req("localhost:8010")) == "http://localhost:8010"
    assert as_auth.resolve_base_url(_req("example.net:1000")) == "http://example.net:1000"


def test_base_url_accepts_any_listed_entry(monkeypatch):
    monkeypatch.setenv(
        "AS_BASE_URL", "http://localhost:8010, http://example.net:1000/"
    )
    # 同じ設定のまま、開いた入口ごとに違うリダイレクト URI になる
    assert as_auth.resolve_base_url(_req("localhost:8010")) == "http://localhost:8010"
    assert as_auth.resolve_base_url(_req("example.net:1000")) == "http://example.net:1000"


def test_spoofed_host_falls_back_to_first_entry(monkeypatch):
    monkeypatch.setenv("AS_BASE_URL", "http://localhost:8010,http://example.net:1000")
    assert as_auth.resolve_base_url(_req("evil.example:80")) == "http://localhost:8010"


def test_tls_terminating_proxy_keeps_registered_scheme(monkeypatch):
    """プロキシ内側は http に見えるが、登録済みの https を使う。"""
    monkeypatch.setenv("AS_BASE_URL", "https://as.example.com")
    assert as_auth.resolve_base_url(_req("as.example.com")) == "https://as.example.com"


def test_base_url_without_request(monkeypatch):
    monkeypatch.setenv("AS_BASE_URL", "http://example.net:1000,http://localhost:8010")
    assert as_auth.resolve_base_url(None) == "http://example.net:1000"
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    assert as_auth.resolve_base_url(None) == as_auth.DEFAULT_BASE_URL


def test_https_only_requires_every_entry_https(monkeypatch):
    monkeypatch.setenv("AS_BASE_URL", "https://a.example.com,https://b.example.com")
    assert as_auth.https_only() is True
    # 1つでも http があれば Secure を付けない（そちらで入れなくなるため）
    monkeypatch.setenv("AS_BASE_URL", "https://a.example.com,http://localhost:8010")
    assert as_auth.https_only() is False
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    assert as_auth.https_only() is False


# ---- サブパス配信（リバースプロキシの配下に階層を持つ） ----


def _subpath_client(tmp_path, monkeypatch, prefix="/asset", **env):
    monkeypatch.setenv("AS_ROOT_PATH", prefix)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(web_app.create_app(str(tmp_path / "t.db")))


def test_root_path_derived_from_base_url(monkeypatch):
    monkeypatch.delenv("AS_ROOT_PATH", raising=False)
    monkeypatch.setenv("AS_BASE_URL", "https://example.com/asset")
    assert as_auth.root_path() == "/asset"
    # 明示指定が優先。スラッシュの有無は正規化する
    monkeypatch.setenv("AS_ROOT_PATH", "sub/")
    assert as_auth.root_path() == "/sub"
    monkeypatch.setenv("AS_ROOT_PATH", "/")
    assert as_auth.root_path() == ""


def test_root_path_empty_by_default(monkeypatch):
    monkeypatch.delenv("AS_ROOT_PATH", raising=False)
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    assert as_auth.root_path() == ""


def test_subpath_serves_app_and_api(tmp_path, monkeypatch):
    client = _subpath_client(tmp_path, monkeypatch)
    assert client.get("/asset/api/health").json() == {"status": "ok"}
    r = client.get("/asset/")
    assert r.status_code == 200
    # 相対 URL の基準を差し込む（スキーム・ホストは書かない）
    assert '<base href="/asset/">' in r.text
    assert 'src="static/app.js"' in r.text
    # 末尾スラッシュ無しは付けてリダイレクト
    r = client.get("/asset", follow_redirects=False)
    assert r.status_code in (301, 307, 308)
    assert r.headers["location"].endswith("/asset/")


def test_subpath_static_files(tmp_path, monkeypatch):
    client = _subpath_client(tmp_path, monkeypatch)
    assert client.get("/asset/static/app.js").status_code == 200


def test_root_serving_unaffected(tmp_path, monkeypatch):
    """接頭辞なし（従来どおり）でも base はルートになる。"""
    monkeypatch.delenv("AS_ROOT_PATH", raising=False)
    monkeypatch.delenv("AS_BASE_URL", raising=False)
    client = TestClient(web_app.create_app(str(tmp_path / "t.db")))
    r = client.get("/")
    assert '<base href="/">' in r.text
    assert client.get("/api/health").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_subpath_login_gate_paths(tmp_path, monkeypatch):
    """ゲートの素通し判定が接頭辞付きでも効くこと。"""
    client = _subpath_client(
        tmp_path, monkeypatch,
        AS_ALLOWED_EMAILS="me@example.com",
        GOOGLE_CLIENT_ID="cid", GOOGLE_CLIENT_SECRET="csec",
    )
    # 未ログインでも画面の骨組みと静的ファイルは読める（でないと何も表示できない）
    assert client.get("/asset/").status_code == 200
    assert client.get("/asset/static/app.js").status_code == 200
    assert client.get("/asset/api/health").status_code == 200
    assert client.get("/asset/auth/me").json()["authenticated"] is False
    # API は閉じる
    assert client.get("/asset/api/summary").status_code == 401


# ---- 静的アセットのキャッシュ制御 ----


def test_no_store_on_static_and_index(tmp_path, monkeypatch):
    """/ と /static/* に no-store が付くこと（接頭辞の有無どちらでも）。

    StaticFiles の Mount は一致時に scope の root_path を書き換えるので、
    ミドルウェアが call_next の後にパスを読むと判定が外れて静かに消える。
    """
    for prefix in ("", "/asset"):
        monkeypatch.setenv("AS_ROOT_PATH", prefix)
        client = TestClient(web_app.create_app(str(tmp_path / f"t{len(prefix)}.db")))
        for path in (f"{prefix}/", f"{prefix}/static/app.js"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers.get("cache-control") == "no-store", path


def test_app_path_does_not_strip_similar_prefix(tmp_path, monkeypatch):
    """接頭辞に前方一致するだけの別パスを剥がさないこと（/asset vs /assetx）。"""
    from starlette.requests import Request

    def req(path):
        return Request({
            "type": "http", "method": "GET", "path": path, "scheme": "http",
            "query_string": b"", "root_path": "/asset", "headers": [(b"host", b"h")],
            "server": ("h", 80),
        })

    assert web_app._app_path(req("/asset/api/x")) == "/api/x"
    assert web_app._app_path(req("/asset")) == "/"
    assert web_app._app_path(req("/assetx/api/x")) == "/assetx/api/x"
