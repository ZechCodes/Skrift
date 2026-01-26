"""Entry point for the skrift package."""

import uvicorn


def main():
    """Run the Skrift development server."""
    uvicorn.run(
        "skrift.asgi:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
    )


if __name__ == "__main__":
    main()
