# OmniTheater - Personal Offline Movie Server
# Copyright (C) 2026-Present  DarThunder
# SPDX-License-Identifier: AGPL-3.0-or-later.

import os
from pathlib import Path
from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".config" / "omnitheater"
ENV_PATH = CONFIG_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

PROFILES_PATH = str(Path.home() / "omnitheater" / "profiles")
PORT = 8084
TMDB_LANGUAGE= "es-MX"

OSIRIS_JWKS_URL = "http://127.0.0.1:8090/.well-known/jwks.json"
HOLDER_URL = "http://127.0.0.1:8080"
DATABASE_URL = f"sqlite:///{CONFIG_DIR / "omnitheater.db"}"
PROWLARR_URL = "http://127.0.0.1:9696"
ARIA2_RPC_URL = "http://127.0.0.1:6800/jsonrpc"

OSIRIS_AUDIENCE = "omnitheater"
OSIRIS_ISSUER = os.getenv("OSIRIS_ISSUER")
if not OSIRIS_ISSUER:
    raise RuntimeError(f"Falta OSIRIS_ISSUER en {ENV_PATH}")

REDIS_PASSWD = os.getenv("REDIS_PASSWORD")
if not REDIS_PASSWD:
    raise RuntimeError(f"Falta REDIS_PASSWORD en {ENV_PATH}")

HOLDER_PUBLIC_URL = os.getenv("HOLDER_PUBLIC_URL")
if not HOLDER_PUBLIC_URL:
    raise RuntimeError(f"Falta HOLDER_PUBLIC_URL en {ENV_PATH}")

PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "")
if not PROWLARR_API_KEY:
    raise RuntimeError(f"Falta PROWLARR_API_KEY en {ENV_PATH}")

ARIA2_SECRET = os.getenv("ARIA2_SECRET", "")
if not ARIA2_SECRET:
    raise RuntimeError(f"Falta ARIA2_SECRET en {ENV_PATH}")

TMDB_API_KEY = os.getenv("ARIA2_SECRET", "")
if not TMDB_API_KEY:
    raise RuntimeError(f"Falta TMDB_API_KEY en {ENV_PATH}")