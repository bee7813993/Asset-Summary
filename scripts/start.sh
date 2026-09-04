#!/usr/bin/env bash
# Asset Summary と Crypto-Summary を 1 コマンドで起動する（Linux / macOS / Git Bash）。
#
# 初回に必要な準備（.env の作成、Crypto-Summary リポジトリの場所の解決、
# ログインモードの判定、サービストークンの生成）を済ませてから docker compose を
# 実行し、両方が応答するまで待って URL を出す。
# 2 回目以降は .env の内容をそのまま使う（勝手に上書きしない）。
#
#   ./scripts/start.sh            起動
#   ./scripts/start.sh --check    設定の確認のみ
#   ./scripts/start.sh --down     停止
#   ./scripts/start.sh --cloud    公開用構成（名前付きボリューム + Caddy + Google ログイン）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
COMPOSE_FILE="docker-compose.yml"
ACTION="up"

for arg in "$@"; do
  case "$arg" in
    --check) ACTION="check" ;;
    --down)  ACTION="down" ;;
    --cloud) COMPOSE_FILE="docker-compose.cloud.yml" ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不明な引数: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '\033[33m    ! %s\033[0m\n' "$1"; }

env_get() {
  # .env から有効な行の値を読む（コメント行は無視）
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" | tail -n 1 | sed 's/[[:space:]]*$//'
}

env_set() {
  # コメント行（#KEY=…）は説明として残し、有効な行だけを書き換える
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ] && grep -qE "^[[:space:]]*$key[[:space:]]*=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" '
      $0 ~ "^[ \t]*" k "[ \t]*=" { print k "=" v; next }
      { print }
    ' "$ENV_FILE" > "$tmp"
  else
    [ -f "$ENV_FILE" ] && cat "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
}

new_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif [ -r /dev/urandom ]; then
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  fi
}

is_cs_repo() {
  [ -n "${1:-}" ] && [ -d "$1/src/crypto_summary" ] && [ -f "$1/Dockerfile" ]
}

cd "$ROOT"

command -v docker >/dev/null 2>&1 || { echo "docker が見つかりません。" >&2; exit 1; }

if [ "$ACTION" = "down" ]; then
  step "停止中"
  exec docker compose -f "$COMPOSE_FILE" down
fi

# ---- .env ------------------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  step ".env を .env.example から作成"
  cp "$ROOT/.env.example" "$ENV_FILE"
fi

# ---- Crypto-Summary リポジトリの場所 ---------------------------------------
CS_CONTEXT="$(env_get CS_CONTEXT)"
if ! is_cs_repo "$CS_CONTEXT"; then
  found=""
  for c in "$ROOT/../Crypto-Summary" "$ROOT/../CS/Crypto-Summary" "$ROOT/../../Crypto-Summary"; do
    if is_cs_repo "$c"; then found="$(cd "$c" && pwd)"; break; fi
  done
  if [ -z "$found" ]; then
    cat >&2 <<MSG
Crypto-Summary リポジトリが見つかりません。
$ENV_FILE に場所を書いてから再実行してください:
    CS_CONTEXT=/path/to/Crypto-Summary
MSG
    exit 1
  fi
  CS_CONTEXT="$found"
  step "Crypto-Summary を検出: $CS_CONTEXT"
  env_set CS_CONTEXT "$CS_CONTEXT"
else
  step "Crypto-Summary: $CS_CONTEXT"
fi

# ---- ログインモードの判定（CS のデータディレクトリを見る） ------------------
# {google_sub}.db があればマルチユーザー（Google ログイン）、
# なければシングルユーザー（ログイン無し）として起動する。
sub=""
sub_count=0
if [ -d "$CS_CONTEXT/data" ]; then
  # サイズの大きい順に見て、数字だけの名前の .db を台帳とみなす
  while IFS= read -r f; do
    base="$(basename "$f" .db)"
    case "$base" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "${#base}" -ge 5 ] || continue
    sub_count=$((sub_count + 1))
    [ -z "$sub" ] && sub="$base"
  done <<< "$(ls -S "$CS_CONTEXT/data"/*.db 2>/dev/null || true)"
fi

# .env に DATA_DIR の行があれば、その選択を尊重する（空＝シングルユーザー）。
# 行そのものが無いときだけ台帳から判定して書き込む。
pinned=0
if [ -f "$ENV_FILE" ] && grep -qE "^[[:space:]]*DATA_DIR[[:space:]]*=" "$ENV_FILE"; then
  pinned=1
fi
if [ "$pinned" = "1" ]; then
  [ -n "$(env_get DATA_DIR)" ] && multi_user=1 || multi_user=0
else
  [ -n "$sub" ] && multi_user=1 || multi_user=0
fi

if [ "$multi_user" = "1" ]; then
  step "Crypto-Summary: マルチユーザー（Google ログイン）"
  [ "$pinned" = "1" ] && info ".env の DATA_DIR に従いました"
  if [ -n "$sub" ]; then
    info "台帳: $sub.db"
    [ "$sub_count" -gt 1 ] && warn "台帳が $sub_count 件あります。一番大きいものを使います（.env の CS_USER_SUB で変更可）。"
    if [ -z "$(env_get CS_USER_SUB)" ]; then
      env_set CS_USER_SUB "$sub"
      info "CS_USER_SUB=$sub を設定しました"
    fi
  fi
  [ "$pinned" = "1" ] || env_set DATA_DIR "/data"
  if [ -z "$(env_get CS_SERVICE_TOKEN)" ]; then
    env_set CS_SERVICE_TOKEN "$(new_token)"
    info "CS_SERVICE_TOKEN を生成しました"
  fi
else
  step "Crypto-Summary: シングルユーザー（ログイン無し）"
  if [ "$pinned" = "1" ]; then
    info ".env の DATA_DIR が空のため、ログイン不要の構成で起動します"
  else
    info "台帳が無いか ledger.db のみのため、ログイン不要の構成で起動します"
    env_set DATA_DIR ""
  fi
fi

if [ "$ACTION" = "check" ]; then
  step "確認のみ（起動しません）"
  docker compose -f "$COMPOSE_FILE" config --quiet
  info "compose の設定は妥当です: $COMPOSE_FILE"
  exit 0
fi

# ---- ポートの先客を確認 -----------------------------------------------------
# Crypto-Summary を単独 compose で起動していると 8000 が埋まる。ビルドしてから
# 失敗すると分かりにくいので、先に名指しで知らせる。
conflicts="$(docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null |
  grep -E ':(8000|8010)->' | grep -v '^asset-stack' || true)"
if [ -n "$conflicts" ]; then
  warn "ポート 8000 / 8010 を他のコンテナが使っています:"
  while IFS='|' read -r name ports; do
    [ -n "$name" ] && warn "  $name  ($ports)"
  done <<< "$conflicts"
  warn "先に停止してください。Crypto-Summary の単独スタックなら:"
  warn "  (cd \"$CS_CONTEXT\" && docker compose down)"
  exit 1
fi

# ---- 起動 ------------------------------------------------------------------
step "docker compose up -d --build（初回はイメージのビルドで数分かかります）"
if ! docker compose -f "$COMPOSE_FILE" up -d --build; then
  warn "起動に失敗しました。ポート 8000 / 8010 を他のスタックが使っていないか確認してください:"
  warn "  docker ps --format '{{.Names}}\t{{.Ports}}'"
  exit 1
fi

step "応答を待っています"
wait_for() {
  local name="$1" url="$2" i
  for i in $(seq 1 60); do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
      info "$name: OK"
      return 0
    fi
    sleep 2
  done
  warn "$name: 応答なし（docker compose logs で確認してください）"
  return 0
}
if command -v curl >/dev/null 2>&1; then
  # 疎通確認だけは 127.0.0.1 を使う。publish 先が IPv4 ループバックなので、
  # localhost が先に ::1 へ解決される環境で空振りしないようにするため。
  # 画面に出す URL は localhost（Google OAuth のリダイレクト URI に合わせる）。
  wait_for "Asset Summary"  "http://127.0.0.1:8010/api/health"
  wait_for "Crypto-Summary" "http://127.0.0.1:8000/api/health"
else
  warn "curl が無いため起動確認を省略しました"
fi

cat <<MSG

  Asset Summary : http://localhost:8010
  Crypto-Summary: http://localhost:8000

  停止: ./scripts/start.sh --down
  ログ: docker compose logs -f
MSG
