"""Caption CLI entry point; stored-track generation is under captions.tracks."""

from .tracks.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
