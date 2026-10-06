"""Generate local secrets without overwriting an existing configuration."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
destination = root / ".env"
if not destination.exists():
    user_password = secrets.token_hex(20)
    root_password = secrets.token_hex(24)
    lines = [
        f"AIQA_DB_PASSWORD={user_password}",
        f"AIQA_DB_ROOT_PASSWORD={root_password}",
        f"AIQA_DATABASE_URL=mysql+pymysql://aiqa:{user_password}@mysql:3306/aiqa?charset=utf8mb4",
        f"AIQA_HOST_DATA={root / '.runtime'}",
        "AIQA_DATA=/data",
        "AIQA_RUNNER_IMAGE=aiqa-runtime:0.1",
        "AIQA_TARGET_NETWORK=aiqa-targets",
        "AIQA_MODEL=ark-code-latest",
        f"AIQA_MODEL_URL={os.environ.get('AIQA_MODEL_URL', '')}",
        f"AIQA_MODEL_KEY={os.environ.get('AIQA_MODEL_KEY', '')}",
        "AIQA_MAX_STEPS=24",
        "AIQA_TASK_TIMEOUT=900",
        f"AIQA_ACCESS_TOKEN={secrets.token_urlsafe(40)}",
        "TEST_USERNAME=",
        "TEST_PASSWORD=",
        "TEST_TOKEN=",
    ]
    destination.write_text("\n".join(lines)+"\n")
    destination.chmod(0o600)
    print("Created private .env; configure model URL/key to enable the Agent.")
else:
    content=destination.read_text()
    if "AIQA_ACCESS_TOKEN=\n" in content:
        content=content.replace("AIQA_ACCESS_TOKEN=\n","AIQA_ACCESS_TOKEN="+secrets.token_urlsafe(40)+"\n")
        destination.write_text(content);destination.chmod(0o600)
    print("Using existing .env.")
(root / ".runtime").mkdir(exist_ok=True)
