"""Entrypoint kept at this path so existing images and manifests keep working.

The code lives in the openvre_scheduler package next to this file.
"""

from openvre_scheduler.server import main

if __name__ == "__main__":
    main()
