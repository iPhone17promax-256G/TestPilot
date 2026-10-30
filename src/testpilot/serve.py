"""Start the platform with native TLS; plaintext startup is never a fallback."""
import uvicorn

from .config import tls_server_options


def main():
    try:
        options = tls_server_options()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    uvicorn.run("testpilot.api:app", host="0.0.0.0", port=8020, **options)


if __name__ == "__main__":
    main()
