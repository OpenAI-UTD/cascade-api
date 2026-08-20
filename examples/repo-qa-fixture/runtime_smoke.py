from app_config import RETRY_TIMEOUT_SECONDS


if RETRY_TIMEOUT_SECONDS != 30:
    raise SystemExit("runtime smoke failed: timeout must be 30 seconds")

print("runtime smoke passed")
