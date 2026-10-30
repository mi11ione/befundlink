import asyncio
import logging
import os

from .config import Config
from .runtime import BotRuntime


def main() -> None:
    logging.basicConfig(level=os.environ.get("BOT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("nio").setLevel(logging.WARNING)
    try:
        asyncio.run(BotRuntime(Config()).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
