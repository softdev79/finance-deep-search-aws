"""secrets.py - on Lambda, load API keys from SSM Parameter Store into os.environ once per cold
start, so the tools keep reading os.environ exactly as they do locally (.env)."""
from __future__ import annotations

import os

_loaded = False
NAMES = ["FINANCIAL_DATASETS_API_KEY", "TAVILY_API_KEY", "SEC_USER_AGENT"]


def load() -> None:
    global _loaded
    prefix = os.getenv("SECRETS_PREFIX")  # e.g. /finance-deep-search/
    if _loaded or not prefix:
        return
    import boto3

    ssm = boto3.client("ssm")
    resp = ssm.get_parameters(Names=[prefix + n.lower().replace("_", "-") for n in NAMES], WithDecryption=True)
    for p in resp["Parameters"]:
        env = p["Name"][len(prefix):].upper().replace("-", "_")
        if p["Value"] and p["Value"] != "-":
            os.environ.setdefault(env, p["Value"])
    _loaded = True
