"""Click CLI for Asset Summary (mirrors Crypto-Summary's cli conventions)."""

from __future__ import annotations

import os
import socket
from decimal import Decimal
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

console = Console()

DEFAULT_DB = "data/assets.db"


@click.group()
@click.option("--db", "db_path", default=None, help=f"SQLite DBパス（既定: {DEFAULT_DB}）")
@click.pass_context
def cli(ctx: click.Context, db_path: str | None) -> None:
    """Asset Summary — 個人資産サマリー（Crypto-Summaryの兄弟アプリ）"""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path or os.environ.get("AS_DB_PATH", DEFAULT_DB)


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8010, show_default=True, type=int)
@click.option("--lan", is_flag=True, help="0.0.0.0 にバインドしLAN内からアクセス可能にする")
@click.pass_context
def web(ctx: click.Context, host: str, port: int, lan: bool) -> None:
    """Webサーバを起動する。"""
    import uvicorn

    from .web.app import create_app

    if lan:
        host = "0.0.0.0"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
            console.print(f"[green]LANアクセス: http://{lan_ip}:{port}[/green]")
        except OSError:
            pass
    console.print(f"[bold]Asset Summary[/bold] → http://{'127.0.0.1' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
    app = create_app(db_path=ctx.obj["db_path"])
    # loop="asyncio": Windows の ProactorEventLoop による WinError 10054 対策
    # （Crypto-Summary cli.py と同じ措置）
    uvicorn.run(app, host=host, port=port, log_level="warning", loop="asyncio")


@cli.command("import")
@click.argument("pdf_path", type=click.Path(exists=True, path_type=Path))
@click.option("--as-of", "as_of", default=None, help="基準日 YYYY-MM-DD（既定: PDFの更新日）")
@click.option("--yes", is_flag=True, help="確認プロンプトを省略して確定する")
@click.option("--include-crypto", is_flag=True, help="暗号資産セクションも取り込む")
@click.pass_context
def import_cmd(
    ctx: click.Context, pdf_path: Path, as_of: str | None, yes: bool, include_crypto: bool
) -> None:
    """マネーフォワードME PDF を取り込む。"""
    from datetime import date, datetime

    from .core.store import Store
    from .importers.service import build_preview, commit_batch

    store = Store(ctx.obj["db_path"])
    if as_of is None:
        as_of = date.fromtimestamp(pdf_path.stat().st_mtime).isoformat()
    preview = build_preview(store, pdf_path.read_bytes(), pdf_path.name)
    report = preview["report"]
    console.print(f"[bold]解析結果[/bold]: {pdf_path.name}")
    for sec in report.get("sections", []):
        mark = "[green]OK[/green]" if sec["ok"] else "[red]NG[/red]"
        console.print(
            f"  {sec['name']}: {sec['row_count']}行 合計照合 {mark}"
        )
    diff_counts: dict[str, int] = {}
    for row in preview["diff"]:
        diff_counts[row["status"]] = diff_counts.get(row["status"], 0) + 1
    console.print(f"差分: {diff_counts}")
    if not yes:
        click.confirm(f"基準日 {as_of} で取り込みますか?", abort=True)
    result = commit_batch(
        store,
        preview["batch_id"],
        as_of_date=datetime.strptime(as_of, "%Y-%m-%d").date(),
        include_sections=None,
        include_crypto=include_crypto,
    )
    console.print(
        f"[green]取込完了[/green]: 新規{result['created']} 更新{result['updated']}"
    )


@cli.command()
@click.option("--currency", default="JPY", show_default=True)
@click.pass_context
def summary(ctx: click.Context, currency: str) -> None:
    """現在の資産サマリーを表示する。"""
    from rich.table import Table

    from .web.app import compute_summary
    from .core.store import Store

    store = Store(ctx.obj["db_path"])
    data = compute_summary(store, currency.upper())
    table = Table(title=f"資産サマリー ({data['currency']})")
    table.add_column("銘柄")
    table.add_column("口座")
    table.add_column("評価額", justify="right")
    table.add_column("損益", justify="right")
    for h in data["holdings"][:20]:
        pl = h["pl"]
        pl_str = "-" if pl is None else f"{Decimal(pl):+,.0f}"
        table.add_row(h["name"], h["account"], f"{Decimal(h['value'] or 0):,.0f}", pl_str)
    console.print(table)
    console.print(f"[bold]総評価額: {Decimal(data['total_value']):,.0f} {data['currency']}[/bold]")
    if data["total_pl"] is not None:
        console.print(f"評価損益: {Decimal(data['total_pl']):+,.0f}")


if __name__ == "__main__":
    cli()
