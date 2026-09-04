#!/usr/bin/env python3
"""マネーフォワードME「資産内訳」ページを定期的にPDF保存するスタンドアロンツール。

Asset Summary のサーバ機能からは独立して動く（このリポジトリの他モジュールは
import しない）。Playwright のヘッドレス Chromium でログイン済みプロファイルを
使い回し、ブラウザの「印刷 → PDFに保存」と同等のPDFを出力する。出力PDFは
そのまま Asset Summary の「マネーフォワードME PDF取込」に使える。

使い方:
    初回のみ: python mf_pdf_autosave.py --login
    以後:     python mf_pdf_autosave.py --out-dir <保存先>

認証情報（メールアドレス・パスワード）は保存しない。--login でブラウザ画面を
開いて手動ログインし、セッションCookieを含むブラウザプロファイル
（既定: ~/.asset-summary/mf-profile）だけを使い回す。

終了コード:
    0 = 成功
    2 = 未ログイン（セッション切れ。--login で再ログインが必要）
    1 = その他のエラー（ページ表示待ちのタイムアウト等）
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

PORTFOLIO_URL = "https://moneyforward.com/bs/portfolio"
ACCOUNTS_URL = "https://moneyforward.com/accounts"

# 一括更新まわりの文言（MFのUI文言に合わせる）
REFRESH_BUTTON_TEXT = "一括更新"
UPDATING_TEXT = "更新中"
REFRESH_FIRST_WAIT_SECONDS = 10.0   # クリック後、キュー投入と表示反映を待つ
REFRESH_POLL_SECONDS = 15.0         # 「更新中」消滅のポーリング間隔
REFRESH_HEARTBEAT_SECONDS = 60.0    # 件数に変化が無くても経過をログに出す間隔

# ログインページ判定（この文字列をURLに含むならセッション切れ）
_SIGNIN_URL_MARKERS = ("id.moneyforward.com", "sign_in")

_TOOL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOL_DIR.parent.parent
DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "mf_pdf"
DEFAULT_PROFILE_DIR = Path.home() / ".asset-summary" / "mf-profile"
DEFAULT_FILENAME = "マネーフォワードME_資産内訳_{date}.pdf"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOGIN_REQUIRED = 2

# Chrome の印刷ダイアログ既定と同等のヘッダ/フッタ（左上に印刷日時、右上にタイトル、
# 左下にURL、右下にページ番号）。印刷日は取込の基準日の参考になる（README のヒント参照）。
_HEADER_TEMPLATE = (
    '<div style="width:100%; font-size:8px; padding:0 12mm;'
    ' display:flex; justify-content:space-between; color:#555;">'
    '<span class="date"></span><span class="title"></span></div>'
)
_FOOTER_TEMPLATE = (
    '<div style="width:100%; font-size:8px; padding:0 12mm;'
    ' display:flex; justify-content:space-between; color:#555;">'
    '<span class="url"></span>'
    '<span><span class="pageNumber"></span>/<span class="totalPages"></span></span></div>'
)

_log_path: Path | None = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)  # pythonw.exe では sys.stdout が None のため print は何もしない
    if _log_path is not None:
        try:
            with _log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _is_signin_url(url: str) -> bool:
    return any(marker in url for marker in _SIGNIN_URL_MARKERS)


def _launch(pw, args, *, headless: bool):
    kwargs: dict = dict(
        user_data_dir=str(Path(args.profile_dir).expanduser()),
        headless=headless,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 900},
        # navigator.webdriver=true を立てない（自動化検知でheadful時と表示が変わるのを防ぐ）
        args=["--disable-blink-features=AutomationControlled"],
    )
    if args.browser_path:
        kwargs["executable_path"] = args.browser_path
    elif headless:
        # ヘッドレスでも headless shell ではなく通常 Chromium の新ヘッドレスモードを使い、
        # ログイン時（headful）と同じ実体・同じ描画にする。
        # 古い Playwright は channel="chromium" を知らないため、失敗したら既定に戻す。
        try:
            return pw.chromium.launch_persistent_context(channel="chromium", **kwargs)
        except Exception:
            pass
    return pw.chromium.launch_persistent_context(**kwargs)


def _mask_headless_ua(ctx, page) -> None:
    """User-Agent の "HeadlessChrome" を "Chrome" に直す。

    ヘッドレスChromiumはUAで自らヘッドレスと名乗るため、サイトによっては
    headful時と違うページ（チャレンジ画面等）が返る。CDPで上書きして
    ログイン時と同じ見え方にする。
    """
    try:
        ua = page.evaluate("() => navigator.userAgent")
        if "HeadlessChrome" in ua:
            cdp = ctx.new_cdp_session(page)
            cdp.send(
                "Network.setUserAgentOverride",
                {
                    "userAgent": ua.replace("HeadlessChrome", "Chrome"),
                    "acceptLanguage": "ja,en-US;q=0.9,en;q=0.8",
                },
            )
    except Exception as exc:
        log(f"User-Agent調整をスキップしました（{exc}）")


def _dump_debug(page, out_dir: Path) -> None:
    """失敗時の診断用に、いま見えている画面とHTMLを保存する（毎回上書き）。"""
    try:
        log(f"ページタイトル: {page.title()!r}")
    except Exception:
        pass
    for name, save in (
        ("debug_last.png", lambda p: page.screenshot(path=str(p), full_page=True)),
        ("debug_last.html", lambda p: p.write_text(page.content(), encoding="utf-8")),
    ):
        path = out_dir / name
        try:
            save(path)
            log(f"診断情報を保存しました: {path}")
        except Exception as exc:
            log(f"診断情報の保存に失敗: {name}（{exc}）")


def _find_ready_page(ctx, ready_text: str):
    """ログイン済みでready_textが見えているタブを探す（無ければ None）。"""
    for page in ctx.pages:
        try:
            url = page.url
            if "moneyforward.com" not in url or _is_signin_url(url):
                continue
            if page.get_by_text(ready_text).count() > 0:
                return page
        except Exception:
            continue  # ナビゲーション中のタブは読めないことがある
    return None


def do_login(args) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _launch(pw, args, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass  # 遅くても手動操作で進められるので致命的ではない
            log("開いたブラウザでマネーフォワードMEにログインしてください。")
            log(f"ログイン後「{args.ready_text}」が表示されるページ（資産 → 資産内訳）が")
            log(f"開けば自動的に完了します（最大 {int(args.login_timeout / 60)} 分待ちます）。")
            deadline = time.monotonic() + args.login_timeout
            ok = False
            while time.monotonic() < deadline:
                try:
                    if not ctx.pages:  # 全タブが閉じられた
                        break
                    if _find_ready_page(ctx, args.ready_text) is not None:
                        ok = True
                        break
                    ctx.pages[0].wait_for_timeout(1500)
                except Exception:
                    # ブラウザごと閉じられた等。閉了済みなら抜ける。
                    time.sleep(1.5)
                    try:
                        if not ctx.pages:
                            break
                    except Exception:
                        break
        finally:
            try:
                ctx.close()  # プロファイル（Cookie）をディスクへ確定させる
            except Exception:
                pass

    if not ok:
        log("ログインを確認できませんでした。もう一度 --login を実行してください。")
        return EXIT_ERROR

    log("ログインを確認しました。続けて動作確認のためPDFを1回保存します"
        "（動作確認のため一括更新は省略します）。")
    args.refresh = False
    return do_fetch(args)


# 最終取得日の読み取り（絶対表記と相対表記の両方に対応）
_ABS_YMD_RE = re.compile(
    r"(\d{4})[/\-年](\d{1,2})[/\-月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?"
)
_ABS_MD_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])(?:\s*(\d{1,2}):(\d{2}))?")
_REL_RES = (
    (re.compile(r"(\d+)\s*分前"), lambda n: timedelta(minutes=n)),
    (re.compile(r"(\d+)\s*時間前"), lambda n: timedelta(hours=n)),
    (re.compile(r"(\d+)\s*日前"), lambda n: timedelta(days=n)),
)


def _parse_last_acquired(text: str) -> datetime | None:
    """テキストから最終取得日らしき日時を読み取る（複数見つかれば最新を返す）。"""
    now = datetime.now()
    found: list[datetime] = []
    spans: list[tuple[int, int]] = []
    for m in _ABS_YMD_RE.finditer(text):
        try:
            found.append(
                datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0))
            )
            spans.append(m.span())
        except ValueError:
            pass
    for m in _ABS_MD_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue  # 年付き表記の一部を年なしとして二重に読まない
        try:
            dt = datetime(now.year, int(m[1]), int(m[2]), int(m[3] or 0), int(m[4] or 0))
        except ValueError:
            continue
        if dt > now + timedelta(days=1):  # 年なし表記の年跨ぎ（12月の日付を翌年に読んだ等）
            dt = dt.replace(year=now.year - 1)
        found.append(dt)
    for regex, delta in _REL_RES:
        for m in regex.finditer(text):
            found.append(now - delta(int(m[1])))
    m = re.search(r"今日\s*(\d{1,2}):(\d{2})", text)
    if m:
        found.append(now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0))
    m = re.search(r"昨日\s*(\d{1,2}):(\d{2})", text)
    if m:
        found.append(
            (now - timedelta(days=1)).replace(
                hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0
            )
        )
    return max(found) if found else None


def _fmt_last(last: datetime | None) -> str:
    return f"{last:%Y-%m-%d %H:%M}" if last else "不明"


def _row_account_name(row_text: str) -> str:
    """行テキストから口座名（最初の意味のあるセル）を取り出す。"""
    for part in re.split(r"[\t\n]+", row_text):
        part = part.strip()
        if not part or UPDATING_TEXT in part:
            continue
        if re.search(r"\d", part) and _parse_last_acquired(part) is not None:
            continue  # 日付セル
        return part[:40]
    return "(名称不明)"


def _updating_rows(page) -> list[dict]:
    """可視の「更新中」ラベルごとに、その行の口座名と最終取得日を読み取る。

    返値: [{"name": str, "last": datetime|None}]。行（tr）が辿れないラベル
    （一括更新ボタン自身が「一括更新中」になっている等）は名称不明・日時不明の
    項目として返し、完了扱いにはしない。
    """
    rows: list[dict] = []
    labels = page.locator(f"text={UPDATING_TEXT} >> visible=true")
    for i in range(labels.count()):
        item = {"name": "(名称不明)", "last": None}
        try:
            row = labels.nth(i).locator("xpath=ancestor::tr[1]")
            if row.count() > 0:
                text = row.first.inner_text()
                item["name"] = _row_account_name(text)
                item["last"] = _parse_last_acquired(text)
        except Exception:
            pass
        rows.append(item)
    return rows


def _is_fresh(item: dict, cutoff: datetime | None) -> bool:
    return cutoff is not None and item["last"] is not None and item["last"] >= cutoff


def _bulk_refresh(page, args, out_dir: Path) -> int | None:
    """登録済み金融機関ページで一括更新を実行し、全口座の「更新中」消滅を待つ。

    最終取得日が --fresh-hours 時間以内の口座は、「更新中」表示のままでも
    更新完了とみなす（MFには更新中表示のまま終わらない口座があるため）。

    返値: セッション切れなら EXIT_LOGIN_REQUIRED、それ以外は None（続行）。
    ボタンが見つからない・時間内に終わらない場合は、その旨をログに残して
    現在の状態のままPDF保存へ進む（定期実行を止めないことを優先する）。
    """
    log("登録済み金融機関の一括更新を実行します...")
    page.goto(args.accounts_url, wait_until="domcontentloaded")
    if _is_signin_url(page.url):
        log("セッションが切れています。--login で再ログインしてください。")
        return EXIT_LOGIN_REQUIRED

    clicked = False
    for candidate in (
        page.get_by_role("button", name=REFRESH_BUTTON_TEXT),
        page.get_by_role("link", name=REFRESH_BUTTON_TEXT),
        page.get_by_text(REFRESH_BUTTON_TEXT),
    ):
        try:
            if candidate.count() > 0:
                candidate.first.click(timeout=10_000)
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        log(f"「{REFRESH_BUTTON_TEXT}」ボタンが見つかりませんでした。更新せずにPDF保存へ進みます。")
        _dump_debug(page, out_dir)
        return None

    log(
        f"一括更新を開始しました。全口座の「{UPDATING_TEXT}」が消えるまで待ちます"
        f"（最大 {args.refresh_timeout / 60:.0f} 分）..."
    )
    # クリックがフォーム送信なら /accounts へのリダイレクトが遅れて発火する。
    # 落ち着くまで待たないと、この後の goto がそれに割り込まれて失敗する。
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(int(REFRESH_FIRST_WAIT_SECONDS * 1000))
    start = time.monotonic()
    deadline = start + args.refresh_timeout
    last_reported = None
    last_logged_at = start
    rows: list[dict] = []
    empty_streak = 0
    while time.monotonic() < deadline:
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            pass  # 一時的な失敗は次の周回で拾う
        try:
            # 「見えている」ラベルだけを行単位で読む。MFの一覧は非表示の状態
            # ラベルもDOMに持つため、存在で数えると全行ぶんが常にヒットして
            # いつまでも0にならない（実測: 画面上の「更新中」7件に対し25件）。
            rows = _updating_rows(page)
        except Exception:
            page.wait_for_timeout(int(REFRESH_POLL_SECONDS * 1000))
            continue
        cutoff = (
            datetime.now() - timedelta(hours=args.fresh_hours)
            if args.fresh_hours > 0
            else None
        )
        stale = [r for r in rows if not _is_fresh(r, cutoff)]
        elapsed = int(time.monotonic() - start)
        if not rows:
            # 一覧の描画前に読むと0件に見えるため、続けて2回確認してから完了とする
            # （実測: 開始直後に「0分00秒で完了」と誤判定していた）。
            empty_streak += 1
            if empty_streak >= 2:
                log(f"全口座の更新が完了しました（{elapsed // 60}分{elapsed % 60:02d}秒）。")
                return None
            page.wait_for_timeout(int(REFRESH_POLL_SECONDS * 1000))
            continue
        empty_streak = 0
        if not stale:
            log(
                f"残り {len(rows)} 口座は「{UPDATING_TEXT}」表示のままですが、最終取得日が"
                f"{args.fresh_hours:g}時間以内のため更新完了とみなします"
                f"（{elapsed // 60}分{elapsed % 60:02d}秒）:"
            )
            for r in rows:
                log(f"  - {r['name']}（最終取得日: {_fmt_last(r['last'])}）")
            return None
        now = time.monotonic()
        # 件数が変わったとき、または変化が無くても一定間隔で経過を出す
        # （金融機関側が遅いと数分単位で件数が動かず、止まって見えるため）
        key = (len(stale), len(rows))
        if key != last_reported or now - last_logged_at >= REFRESH_HEARTBEAT_SECONDS:
            fresh_note = (
                f"（うち {len(rows) - len(stale)} 件は最終取得日が新しく完了扱い）"
                if len(rows) != len(stale)
                else ""
            )
            log(
                f"{UPDATING_TEXT}の口座: {len(rows)} 件{fresh_note}"
                f"（経過 {elapsed // 60}分{elapsed % 60:02d}秒）"
            )
            last_reported = key
            last_logged_at = now
        page.wait_for_timeout(int(REFRESH_POLL_SECONDS * 1000))
    log("時間内に一括更新が終わりませんでした。現在の状態でPDF保存へ進みます。")
    cutoff = (
        datetime.now() - timedelta(hours=args.fresh_hours) if args.fresh_hours > 0 else None
    )
    stale = [r for r in rows if not _is_fresh(r, cutoff)]
    if stale:
        log("未完了の口座:")
        for r in stale:
            log(f"  - {r['name']}（最終取得日: {_fmt_last(r['last'])}）")
    return None


def _goto(page, url: str, attempts: int = 3) -> None:
    """遅れて発火した別ナビゲーションに割り込まれたら開き直す。

    一括更新のクリックがフォーム送信のとき、MFは少し遅れて /accounts へ
    リダイレクトする。そこへ資産内訳への goto が重なると Playwright が
    "interrupted by another navigation" で失敗するため、落ち着かせて再試行する。
    """
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as exc:
            if "interrupted by another navigation" not in str(exc) or i == attempts - 1:
                raise
            log(f"別のページ遷移と重なったため開き直します（{i + 1}回目）")
            page.wait_for_timeout(3000)


def _wait_ready(page, args) -> str:
    """ready_text の出現を待つ。返値: "ok" / "signin" / "timeout"。

    可視状態ではなく存在で判定する（同じ文言の隠れ要素が先にマッチしても
    失敗しないように）。
    """
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if _is_signin_url(page.url):
            return "signin"
        try:
            if (
                page.get_by_text(args.ready_text).count() > 0
                or args.ready_text in page.content()
            ):
                return "ok"
        except Exception:
            pass  # ナビゲーション中は評価に失敗しうる
        page.wait_for_timeout(1000)
    return "timeout"


def _headful_preview(args, out_dir: Path) -> int | None:
    """ブラウザ画面を表示して、一括更新〜資産内訳の表示までを実行する（動作確認用）。

    ChromiumのPDF生成はヘッドレス専用のため、確認が済んだらブラウザを閉じて
    ヘッドレス側で保存し直す。返値: 中断すべきときの終了コード / 続行なら None。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _launch(pw, args, headless=False)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(args.timeout * 1000)
            if args.refresh:
                code = _bulk_refresh(page, args, out_dir)
                if code is not None:
                    return code
            _goto(page, args.url)
            state = _wait_ready(page, args)
            if state == "signin":
                log("セッションが切れています。--login で再ログインしてください。")
                return EXIT_LOGIN_REQUIRED
            if state == "timeout":
                log(
                    f"ページに「{args.ready_text}」が表示されませんでした"
                    f"（{args.timeout:.0f}秒待機、URL: {page.url}）。"
                )
                _dump_debug(page, out_dir)
                return EXIT_ERROR
            log("資産内訳を確認しました。5秒後にこのブラウザを閉じ、ヘッドレスでPDF保存を行います。")
            page.wait_for_timeout(5_000)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return None


def do_fetch(args) -> int:
    from playwright.sync_api import TimeoutError as PWTimeoutError
    from playwright.sync_api import sync_playwright

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.headful:
        code = _headful_preview(args, out_dir)
        if code is not None:
            return code
        args.refresh = False  # 一括更新は画面表示側で済ませた
    now = datetime.now()
    filename = args.filename.format(
        date=now.strftime("%Y-%m-%d"), datetime=now.strftime("%Y-%m-%d_%H%M")
    )
    out_path = out_dir / filename
    tmp_path = out_path.with_name(out_path.name + ".part")

    with sync_playwright() as pw:
        ctx = _launch(pw, args, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(args.timeout * 1000)
            _mask_headless_ua(ctx, page)
            if args.refresh:
                code = _bulk_refresh(page, args, out_dir)
                if code is not None:
                    return code
            _goto(page, args.url)
            state = _wait_ready(page, args)
            if state == "signin":
                log("セッションが切れています。--login で再ログインしてください。")
                return EXIT_LOGIN_REQUIRED
            if state == "timeout":
                log(
                    f"ページに「{args.ready_text}」が表示されませんでした"
                    f"（{args.timeout:.0f}秒待機、URL: {page.url}）。"
                )
                _dump_debug(page, out_dir)
                log("MF側の障害・レイアウト変更・ログイン途中状態の可能性があります。"
                    "debug_last.png を開いて実際の画面を確認してください。")
                return EXIT_ERROR
            # 明細・グラフの遅延読み込みが落ち着くのを待つ（ベストエフォート）
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeoutError:
                pass
            page.wait_for_timeout(int(args.settle * 1000))
            page.pdf(
                path=str(tmp_path),
                format="A4",
                print_background=False,
                display_header_footer=True,
                header_template=_HEADER_TEMPLATE,
                footer_template=_FOOTER_TEMPLATE,
                margin={"top": "20mm", "bottom": "16mm", "left": "10mm", "right": "10mm"},
            )
            os.replace(tmp_path, out_path)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                ctx.close()
            except Exception:
                pass

    log(f"保存しました: {out_path}（{out_path.stat().st_size:,} バイト）")
    if args.keep > 0:
        _prune(out_dir, args.filename, args.keep)
    return EXIT_OK


class _Wildcard(dict):
    def __missing__(self, key: str) -> str:
        return "*"


def _prune(out_dir: Path, filename_pattern: str, keep: int) -> None:
    """ファイル名パターンに一致するPDFを新しい順に keep 件だけ残す。"""
    glob_pattern = filename_pattern.format_map(_Wildcard())
    if not glob_pattern.strip("*"):
        # 固定部分の無いパターン（例: "{date}"）は保存先の全ファイルに一致してしまう
        log("--filename パターンに固定部分が無いため世代管理をスキップしました")
        return
    protected = {_log_path.resolve()} if _log_path is not None else set()
    files = sorted(
        (
            p
            for p in out_dir.glob(glob_pattern)
            if p.is_file()
            and not p.name.endswith(".part")
            and p.resolve() not in protected
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            log(f"世代管理により削除: {old.name}")
        except OSError as exc:
            log(f"削除に失敗: {old.name}（{exc}）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="マネーフォワードME「資産内訳」ページをPDF保存する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--login", action="store_true",
                   help="ブラウザ画面を開いて手動ログインし、プロファイルを保存する（初回・セッション切れ時）")
    p.add_argument("--headful", action="store_true",
                   help="ブラウザ画面を表示して一括更新〜資産内訳の表示までを確認する"
                        "（動作確認用。PDF保存自体は確認後にヘッドレスで行う）")
    p.add_argument("--out-dir", default=os.environ.get("MF_PDF_OUT_DIR") or str(DEFAULT_OUT_DIR),
                   help="PDFの保存先フォルダ（環境変数 MF_PDF_OUT_DIR でも指定可）")
    p.add_argument("--filename", default=DEFAULT_FILENAME,
                   help="ファイル名パターン。{date}=YYYY-MM-DD, {datetime}=YYYY-MM-DD_HHMM")
    p.add_argument("--profile-dir",
                   default=os.environ.get("MF_PDF_PROFILE_DIR") or str(DEFAULT_PROFILE_DIR),
                   help="ログイン状態を保持するブラウザプロファイルの場所（MF_PDF_PROFILE_DIR でも指定可）")
    p.add_argument("--url", default=PORTFOLIO_URL, help="取得するページのURL")
    p.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True,
                   help="保存前に登録済み金融機関の一括更新を実行し、全口座の「更新中」消滅を待つ")
    p.add_argument("--refresh-timeout", type=float, default=900.0,
                   help="一括更新の完了を待つ最大秒数（超えたら現状のまま保存へ進む）")
    p.add_argument("--fresh-hours", type=float, default=12.0,
                   help="最終取得日がこの時間以内の口座は「更新中」表示のままでも"
                        "更新完了とみなす（0で無効）")
    p.add_argument("--accounts-url", default=ACCOUNTS_URL,
                   help="一括更新を行う登録済み金融機関ページのURL")
    p.add_argument("--ready-text", default="資産総額",
                   help="このテキストが表示されたらページ準備完了とみなす")
    p.add_argument("--timeout", type=float, default=90.0, help="ページ表示待ちの秒数")
    p.add_argument("--settle", type=float, default=2.0, help="表示後にさらに待つ描画安定秒数")
    p.add_argument("--login-timeout", type=float, default=900.0,
                   help="--login で手動ログインを待つ秒数")
    p.add_argument("--keep", type=int, default=0,
                   help="保存先に残すPDFの世代数（0=無制限。古いものから削除）")
    p.add_argument("--log-file", default=None,
                   help="ログの追記先（既定: 保存先/mf_pdf_autosave.log、'-' で無効）")
    p.add_argument("--browser-path", default=os.environ.get("MF_PDF_CHROMIUM_PATH") or None,
                   help="Chromium実行ファイルを明示指定（MF_PDF_CHROMIUM_PATH でも指定可）")
    return p


def main(argv: list[str] | None = None) -> int:
    # cp932 コンソールでの文字化け対策（pythonw.exe では stdout が None）
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    args = build_parser().parse_args(argv)

    global _log_path
    if args.log_file != "-":
        _log_path = (
            Path(args.log_file).expanduser()
            if args.log_file
            else Path(args.out_dir).expanduser() / "mf_pdf_autosave.log"
        )
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            _log_path = None

    try:
        import playwright  # noqa: F401
    except ImportError:
        log("Playwright が見つかりません。次の2コマンドでセットアップしてください:")
        log("  pip install -r tools/mf_pdf_autosave/requirements.txt")
        log("  python -m playwright install chromium")
        return EXIT_ERROR

    try:
        if args.login:
            return do_login(args)
        return do_fetch(args)
    except Exception:
        log("予期しないエラーが発生しました:")
        log(traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
