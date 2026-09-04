"""Google OAuth 2.0 によるログインゲート（任意有効化）。

Crypto-Summary の web/auth.py を踏襲。ただし AS は単一 DB のまま —
認証は「メール許可リストによる入場ゲート」であってマルチテナントではない。
ログインした利用者の Google sub は、Crypto-Summary 連携の X-CS-User に使う。

有効化条件: 環境変数 AS_ALLOWED_EMAILS が設定されていること。
未設定なら SessionMiddleware もゲートも登録されず、従来どおり認証なしで動く。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


def auth_enabled() -> bool:
    return bool(os.environ.get("AS_ALLOWED_EMAILS", "").strip())


def allowed_emails() -> set[str]:
    raw = os.environ.get("AS_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def email_allowed(email: str | None) -> bool:
    if not email:
        return False
    return email.strip().lower() in allowed_emails()


DEFAULT_BASE_URL = "http://localhost:8010"


def root_path() -> str:
    """サブパス配信の接頭辞（"/asset" 形式。ルート直下なら空文字）。

    AS_ROOT_PATH が優先。未設定なら AS_BASE_URL の先頭エントリのパス部分を
    使う（https://例.com/asset と書けば接頭辞も決まる、という一本化）。
    """
    raw = os.environ.get("AS_ROOT_PATH")
    if raw is None or not raw.strip():
        urls = allowed_base_urls()
        raw = urlsplit(urls[0]).path if urls else ""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    return raw if raw.startswith("/") else "/" + raw


def allowed_base_urls() -> list[str]:
    """AS_BASE_URL に列挙された公開 URL（カンマ区切り・空なら制限なし）。

    同じアプリを複数の入口で開けるようにするための一覧。例:
      AS_BASE_URL=http://localhost:8010,http://example.net:1000
    OAuth のリダイレクト URI はリクエストごとに「今開いている入口」から
    組み立てるが、Host ヘッダは送信側が自由に名乗れるため、ここに挙げた
    URL 以外は採用しない。
    """
    raw = os.environ.get("AS_BASE_URL") or ""
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def _base_url() -> str:
    """設定上の正規 URL（リクエストが無い文脈での既定）。"""
    urls = allowed_base_urls()
    return urls[0] if urls else DEFAULT_BASE_URL


def resolve_base_url(request: Request | None = None) -> str:
    """このリクエストが名乗っている入口の URL を返す。

    ブラウザが実際に開いた URL でリダイレクト URI を組み立てるので、
    localhost でもポート転送先のホスト名でも、設定を変えずに同じように
    ログインできる（どちらも Google に登録してあることが前提）。

    Host ヘッダは詐称できるため、AS_BASE_URL を設定してあるときはその
    一覧に無いものを採用しない。TLS 終端プロキシの内側ではスキームが
    http に見えるので、ホスト:ポートが一致する登録済み URL があれば
    そちらのスキームを優先する。
    """
    allowed = allowed_base_urls()
    if request is None:
        return allowed[0] if allowed else DEFAULT_BASE_URL

    derived = str(request.base_url).rstrip("/")
    if not allowed:
        return derived
    if derived in allowed:
        return derived
    netloc = urlsplit(derived).netloc
    for url in allowed:
        if urlsplit(url).netloc == netloc:
            return url
    return allowed[0]


def https_only() -> bool:
    """セッション Cookie に Secure を付けるか。

    Cookie の設定はミドルウェア登録時に1度だけ決まるためリクエストごとに
    切り替えられない。入口が1つでも http なら、Secure を付けると
    そちらでログインできなくなるので付けない。
    """
    urls = allowed_base_urls()
    if not urls:
        return False
    return all(u.startswith("https://") for u in urls)


_SESSION_KEY_FILE = "_session_key"


def session_secret(base_dir: str) -> str:
    """セッション署名鍵。env(AS_SECRET_KEY) が無ければ生成して base_dir に保存。

    固定の既定値にフォールバックすると、公開した瞬間に誰でもセッション Cookie を
    偽造できてしまう。env が無い場合はランダムに生成し、再起動でログインが
    切れないようファイルへ残す（0600・DB と同じディレクトリ）。
    """
    env_key = os.environ.get("AS_SECRET_KEY", "").strip()
    if env_key:
        return env_key

    path = Path(base_dir) / _SESSION_KEY_FILE
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    except OSError:
        pass

    key = secrets.token_hex(32)
    try:
        path.write_text(key, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # 書けない場合もその場限りの鍵で動かす（再起動でログインは切れる）。
        pass
    return key


def _get_oauth():
    """authlib OAuth クライアントを返す（遅延初期化）。"""
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


_oauth = None


def _oauth_client():
    global _oauth
    if _oauth is None:
        _oauth = _get_oauth()
    return _oauth.google


def reset_oauth_client() -> None:
    """キャッシュ済み OAuth クライアントを破棄する（テスト・鍵変更後用）。"""
    global _oauth
    _oauth = None


@router.get("/auth/login")
async def login(request: Request):
    # 開いている入口に応じたリダイレクト URI（authlib が state と一緒に
    # セッションへ保存し、コールバックのトークン交換でも同じ値を使う）。
    redirect_uri = resolve_base_url(request) + "/auth/callback"
    return await _oauth_client().authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request):
    from authlib.integrations.starlette_client import OAuthError

    try:
        token = await _oauth_client().authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = token.get("userinfo")
    if not user:
        raise HTTPException(status_code=400, detail="userinfo not found")
    if not email_allowed(user.get("email")):
        # 許可リスト外 — セッションを残さずログイン画面へ戻す
        request.session.clear()
        return RedirectResponse(
            url=(request.scope.get("root_path") or "") + "/?login=denied"
        )
    request.session["user"] = {
        "sub": user["sub"],
        "email": user["email"],
        "name": user.get("name", ""),
        "picture": user.get("picture", ""),
    }
    return RedirectResponse(url=(request.scope.get("root_path") or "") + "/")


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url=(request.scope.get("root_path") or "") + "/")


@router.get("/auth/me")
async def me(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        return {"authenticated": False, "enabled": True}
    return {"authenticated": True, "enabled": True, **user}
