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
        f"TESTPILOT_DB_PASSWORD={user_password}",
        f"TESTPILOT_DB_ROOT_PASSWORD={root_password}",
        f"TESTPILOT_DATABASE_URL=mysql+pymysql://testpilot:{user_password}@mysql:3306/testpilot?charset=utf8mb4",
        f"TESTPILOT_HOST_DATA={root / '.runtime'}",
        "TESTPILOT_DATA=/data",
        "TESTPILOT_RUNNER_IMAGE=testpilot-runtime:0.1",
        "TESTPILOT_TARGET_NETWORK=testpilot-targets",
        "TESTPILOT_MODEL=ark-code-latest",
        f"TESTPILOT_MODEL_URL={os.environ.get('TESTPILOT_MODEL_URL', '')}",
        f"TESTPILOT_MODEL_KEY={os.environ.get('TESTPILOT_MODEL_KEY', '')}",
        "TESTPILOT_MAX_STEPS=24",
        "TESTPILOT_TASK_TIMEOUT=900",
        f"TESTPILOT_ACCESS_TOKEN={secrets.token_urlsafe(40)}",
        "TEST_USERNAME=",
        "TEST_PASSWORD=",
        "TEST_TOKEN=",
    ]
    destination.write_text("\n".join(lines)+"\n")
    destination.chmod(0o600)
    print("Created private .env; configure model URL/key to enable the Agent.")
else:
    content=destination.read_text()
    if "TESTPILOT_ACCESS_TOKEN=\n" in content:
        content=content.replace("TESTPILOT_ACCESS_TOKEN=\n","TESTPILOT_ACCESS_TOKEN="+secrets.token_urlsafe(40)+"\n")
        destination.write_text(content);destination.chmod(0o600)
    print("Using existing .env.")
content=destination.read_text()
values=dict(line.split("=",1) for line in content.splitlines() if "=" in line and not line.startswith("#"))
if not values.get("TESTPILOT_REDIS_PASSWORD"):
    redis_password=secrets.token_hex(24)
    with destination.open("a") as file:
        file.write(f"\nTESTPILOT_REDIS_PASSWORD={redis_password}\nTESTPILOT_REDIS_URL=redis://:{redis_password}@redis:6379/0\n")
    destination.chmod(0o600)
(root / ".runtime").mkdir(exist_ok=True)
if "TESTPILOT_QDRANT_API_KEY=" not in destination.read_text():
    with destination.open("a") as file:
        file.write("\nTESTPILOT_QDRANT_API_KEY="+secrets.token_hex(24)+"\nTESTPILOT_QDRANT_URL=http://qdrant:6333\n")
    destination.chmod(0o600)
