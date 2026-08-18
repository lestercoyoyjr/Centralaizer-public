#!/usr/bin/env bash
#
# Centralaizer — one-shot setup & launch.
#
# Installs everything needed to run the app (Python venv + pip packages + spaCy
# model + Ollama + embedding model), shows you exactly what will be downloaded
# and asks before downloading anything, then starts main.py.
#
# Usage:
#   ./setup_and_run.sh                 # interactive: show plan, confirm, install, run
#   ./setup_and_run.sh --yes           # skip the confirmation prompt
#   ./setup_and_run.sh --no-run        # set everything up but don't launch the app
#   ./setup_and_run.sh --with-reasoning# also pull the optional reasoning model (~20 GB)
#   ./setup_and_run.sh --with-dev      # also install dev/test deps (pytest, ruff, mypy)
#   ./setup_and_run.sh --open-extension# reveal the browser-bridge extension folder to load into Chrome
#
# The setup logic is organised into functions and guarded at the bottom so the
# test suite can `source` this file (SETUP_SH_SOURCE_ONLY=1) and exercise the
# helpers atomically without running the installer.
#
set -euo pipefail

# ── pretty output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  B="\033[1m"; DIM="\033[2m"; OK="\033[92m"; WARN="\033[93m"; ERR="\033[91m"; CY="\033[96m"; END="\033[0m"
else
  B=""; DIM=""; OK=""; WARN=""; ERR=""; CY=""; END=""
fi
say()  { printf "%b\n" "$*"; }
hr()   { say "${DIM}────────────────────────────────────────────────────────────${END}"; }

# ── what to install ──────────────────────────────────────────────────────────
# runtime pip dependencies (mirrors pyproject.toml [project].dependencies)
PIP_PKGS=(
  "fastmcp>=2.0" "chromadb>=0.5" "ollama>=0.3" "networkx>=3.3" "duckdb>=1.0"
  "spacy>=3.7" "apscheduler>=3.10" "fastapi>=0.111" "uvicorn[standard]>=0.30"
  "jinja2>=3.1" "python-multipart>=0.0.9" "pydantic>=2.7" "pydantic-settings>=2.3"
  "rich>=13.7" "httpx>=0.27" "python-dotenv>=1.0"
)
DEV_PKGS=( "pytest>=8.2" "pytest-asyncio>=0.23" "pytest-cov>=5.0" "ruff>=0.4" "mypy>=1.10" )
SPACY_MODEL="en_core_web_sm"
EMBED_MODEL="nomic-embed-text"
REASON_MODEL="qwen3:32b"

# ── locate a suitable Python (3.11+, prefer 3.12 per project gotcha) ─────────
find_python() {
  for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,11) else 1)' 2>/dev/null; then
        echo "$cand"; return 0
      fi
    fi
  done
  return 1
}

# ── plan-line status formatter ───────────────────────────────────────────────
# $1 = 1 if already present, else 0.  $2 = the "will download …" text to show.
status() { [ "$1" = 1 ] && printf "%b" "${OK}already installed${END}" || printf "%b" "${WARN}$2${END}"; }

# ── usage ─────────────────────────────────────────────────────────────────────
print_help() { awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next}{exit}' "$0"; }

# ── main flow ─────────────────────────────────────────────────────────────────
main() {
  cd "$(dirname "$0")"
  ROOT="$(pwd)"
  VENV="$ROOT/.venv"
  PY="$VENV/bin/python"

  local ASSUME_YES=0 RUN_APP=1 WITH_REASONING=0 WITH_DEV=0 OPEN_EXT=0
  for arg in "$@"; do
    case "$arg" in
      --yes|-y)         ASSUME_YES=1 ;;
      --no-run)         RUN_APP=0 ;;
      --with-reasoning) WITH_REASONING=1 ;;
      --with-dev)       WITH_DEV=1 ;;
      --open-extension) OPEN_EXT=1 ;;
      -h|--help)        print_help; exit 0 ;;
      *) say "${ERR}Unknown option: $arg${END}"; exit 2 ;;
    esac
  done

  local OS ARCH
  OS="$(uname -s)"; ARCH="$(uname -m)"

  # ── detection: only download the deltas ────────────────────────────────────
  local HOST_PY HOST_PY_VER VENV_OK PIP_OK SPACY_OK OLLAMA_OK EMBED_OK REASON_OK
  HOST_PY="$(find_python || true)"
  HOST_PY_VER=""; [ -n "$HOST_PY" ] && HOST_PY_VER="$("$HOST_PY" -V 2>&1 | awk '{print $2}')"

  VENV_OK=0; [ -x "$PY" ] && VENV_OK=1

  PIP_OK=0
  if [ "$VENV_OK" = 1 ]; then
    if "$PY" -c "import fastmcp,chromadb,ollama,networkx,duckdb,spacy,apscheduler,fastapi,uvicorn,jinja2,multipart,pydantic,pydantic_settings,rich,httpx,dotenv" >/dev/null 2>&1; then
      PIP_OK=1
    fi
  fi

  SPACY_OK=0
  [ "$VENV_OK" = 1 ] && "$PY" -c "import spacy; spacy.load('$SPACY_MODEL')" >/dev/null 2>&1 && SPACY_OK=1

  OLLAMA_OK=0
  command -v ollama >/dev/null 2>&1 && OLLAMA_OK=1

  # NB: use `grep … >/dev/null`, NOT `grep -q` — under `set -o pipefail`, grep -q
  # closes the pipe on first match, `ollama list` then dies with SIGPIPE, and the
  # pipeline reports failure → the model would be seen as missing and re-pulled.
  EMBED_OK=0
  [ "$OLLAMA_OK" = 1 ] && ollama list 2>/dev/null | grep "^${EMBED_MODEL}" >/dev/null && EMBED_OK=1

  REASON_OK=0
  [ "$OLLAMA_OK" = 1 ] && ollama list 2>/dev/null | grep "^${REASON_MODEL%%:*}" >/dev/null && REASON_OK=1

  # ── print the plan ──────────────────────────────────────────────────────────
  say ""
  say "${B}🧠  Centralaizer — setup plan${END}"
  say "${DIM}OS: ${OS}/${ARCH}   ·   project: ${ROOT}${END}"
  hr

  if [ -z "$HOST_PY" ]; then
    say "  ${ERR}✗ Python 3.11+ not found.${END} Install Python 3.12 first (e.g. 'brew install python@3.12')."
    exit 1
  fi
  say "  Python interpreter    ${OK}${HOST_PY} (${HOST_PY_VER})${END}"
  say "  Virtualenv (.venv)    $(status "$VENV_OK" "will create")"
  say "  Pip packages (${#PIP_PKGS[@]})     $(status "$PIP_OK" "will download  ~400–600 MB")"
  [ "$WITH_DEV" = 1 ] && say "  Dev/test packages     ${WARN}will download  ~30 MB${END}"
  say "  spaCy model           $(status "$SPACY_OK" "will download  ~12 MB")   ${DIM}($SPACY_MODEL)${END}"

  if [ "$OS" = "Darwin" ]; then
    say "  Ollama runtime        $(status "$OLLAMA_OK" "will download  ~1 GB (Homebrew cask 'ollama-app')")"
  else
    say "  Ollama runtime        $(status "$OLLAMA_OK" "will install via https://ollama.com/install.sh")"
  fi
  say "  Embedding model       $(status "$EMBED_OK" "will download  ~274 MB")   ${DIM}($EMBED_MODEL — required)${END}"
  if [ "$WITH_REASONING" = 1 ]; then
    say "  Reasoning model       $(status "$REASON_OK" "will download  ~20 GB")   ${DIM}($REASON_MODEL — optional)${END}"
  else
    say "  Reasoning model       ${DIM}skipped (optional ~20 GB; pass --with-reasoning to include)${END}"
  fi
  say "  Browser bridge        ${DIM}bundled — manual load into Chrome (needs your permission, see below)${END}"
  hr
  say "  ${DIM}Everything installs locally. Nothing about your data leaves this machine.${END}"
  say "  After setup: MCP → http://localhost:3000/mcp   ·   UI → http://localhost:3001"
  hr

  if [ "$PIP_OK" = 1 ] && [ "$SPACY_OK" = 1 ] && [ "$OLLAMA_OK" = 1 ] && [ "$EMBED_OK" = 1 ] \
     && { [ "$WITH_REASONING" = 0 ] || [ "$REASON_OK" = 1 ]; } && [ "$WITH_DEV" = 0 ]; then
    say "  ${OK}Everything required is already installed.${END}"
  fi

  # ── confirm before downloading ──────────────────────────────────────────────
  if [ "$ASSUME_YES" != 1 ]; then
    printf "%b" "${CY}Proceed with installation above?${END} [y/N]: "
    local reply; read -r reply
    case "$reply" in
      y|Y|yes|YES) ;;
      *) say "${DIM}Aborted. Nothing was changed.${END}"; exit 0 ;;
    esac
  fi

  # ── 1. venv ─────────────────────────────────────────────────────────────────
  if [ "$VENV_OK" != 1 ]; then
    say "\n${B}» Creating virtualenv with ${HOST_PY}…${END}"
    "$HOST_PY" -m venv "$VENV"
  fi
  say "${B}» Upgrading pip…${END}"
  "$PY" -m pip install --quiet --upgrade pip

  # ── 2. pip packages ──────────────────────────────────────────────────────────
  if [ "$PIP_OK" != 1 ]; then
    say "${B}» Installing Python packages…${END}"
    "$PY" -m pip install "${PIP_PKGS[@]}"
  fi
  if [ "$WITH_DEV" = 1 ]; then
    say "${B}» Installing dev/test packages…${END}"
    "$PY" -m pip install "${DEV_PKGS[@]}"
  fi

  # ── 3. spaCy model ───────────────────────────────────────────────────────────
  if [ "$SPACY_OK" != 1 ]; then
    say "${B}» Downloading spaCy model ${SPACY_MODEL}…${END}"
    "$PY" -m spacy download "$SPACY_MODEL"
  fi

  # ── 4. Ollama runtime ────────────────────────────────────────────────────────
  if [ "$OLLAMA_OK" != 1 ]; then
    if [ "$OS" = "Darwin" ]; then
      if ! command -v brew >/dev/null 2>&1; then
        say "${ERR}Homebrew not found.${END} Install it from https://brew.sh, or install Ollama from https://ollama.com/download, then re-run."
        exit 1
      fi
      say "${B}» Installing Ollama (official app via Homebrew cask)…${END}"
      # NOTE: the 'ollama' *formula* ships without the llama-server runtime and cannot
      # run models — the 'ollama-app' cask bundles the real runtime. Use the cask.
      brew install --cask ollama-app
    else
      say "${B}» Installing Ollama via official installer…${END}"
      curl -fsSL https://ollama.com/install.sh | sh
    fi
  fi

  # ── 5. start Ollama server (if not already listening) ────────────────────────
  if ! curl -s -o /dev/null "http://localhost:11434/api/tags" 2>/dev/null; then
    say "${B}» Starting Ollama server…${END}"
    ollama serve >/tmp/ollama.log 2>&1 &
    local _i
    for _i in $(seq 1 60); do
      curl -s -o /dev/null "http://localhost:11434/api/tags" 2>/dev/null && break
      sleep 0.5
    done
  fi

  # ── 6. models ────────────────────────────────────────────────────────────────
  if [ "$EMBED_OK" != 1 ]; then
    say "${B}» Pulling embedding model ${EMBED_MODEL}…${END}"
    ollama pull "$EMBED_MODEL"
  fi
  if [ "$WITH_REASONING" = 1 ] && [ "$REASON_OK" != 1 ]; then
    say "${B}» Pulling reasoning model ${REASON_MODEL} (large)…${END}"
    ollama pull "$REASON_MODEL"
  fi

  say "\n${OK}${B}✓ Setup complete.${END}"

  # ── 6b. browser bridge (optional, manual Chrome load) ────────────────────────
  # Chrome blocks silent install of unpacked extensions by design, so this can't
  # be auto-installed — we surface the folder and the permissions it will ask for.
  local EXT_DIR="$ROOT/browser-extension"
  if [ -d "$EXT_DIR" ]; then
    hr
    say "${B}» Browser bridge (optional) — use the hub from ChatGPT / Gemini / Qwen${END}"
    say "  Load it once in Chrome:  ${CY}chrome://extensions${END} → enable ${B}Developer mode${END} → ${B}Load unpacked${END} → select:"
    say "    ${B}$EXT_DIR${END}"
    say "  ${DIM}On load, Chrome will ask you to grant these permissions:${END}"
    say "    ${DIM}· read & change your data on ${END}localhost:3001${DIM} (to reach your hub)${END}"
    say "    ${DIM}· run on ${END}chatgpt.com, chat.openai.com, gemini.google.com, chat.qwen.ai${DIM} (the Recall/Remember toolbar)${END}"
    say "  ${DIM}It talks only to your local hub — nothing leaves your machine.${END}"
    if [ "$OPEN_EXT" = 1 ]; then
      if [ "$OS" = "Darwin" ]; then open "$EXT_DIR" >/dev/null 2>&1 || true
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$EXT_DIR" >/dev/null 2>&1 || true; fi
      say "  ${DIM}(opened the folder — in Chrome's Load unpacked dialog, select it; ⌘⇧G pastes the path)${END}"
    else
      say "  ${DIM}Re-run with --open-extension to pop this folder open in Finder.${END}"
    fi
  fi

  # ── 7. run ────────────────────────────────────────────────────────────────────
  if [ "$RUN_APP" = 1 ]; then
    hr
    say "${B}» Launching Centralaizer…${END}   ${DIM}(Ctrl-C to stop)${END}"
    say "${DIM}  MCP: http://localhost:3000/mcp   ·   UI: http://localhost:3001${END}"
    hr
    exec "$PY" main.py
  else
    say "${DIM}Skipping launch (--no-run). Start later with:  ${END}${B}$PY main.py${END}"
  fi
}

# Guard: when sourced by the test suite (SETUP_SH_SOURCE_ONLY=1) we expose the
# functions above without running the installer; otherwise run normally.
if [ "${SETUP_SH_SOURCE_ONLY:-0}" != 1 ]; then
  main "$@"
fi
