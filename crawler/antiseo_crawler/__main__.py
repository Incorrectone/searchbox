"""Entry point:  python -m antiseo_crawler <command> [options]

Commands: crawl, seed, search, prepare-model
"""
import sys

COMMANDS = {
    "crawl": "antiseo_crawler.crawl",
    "seed": "antiseo_crawler.seed",
    "search": "antiseo_crawler.search",
    "prepare-model": "antiseo_crawler.prepare_model",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__.strip())
        return 2
    import importlib

    module = importlib.import_module(COMMANDS[sys.argv[1]])
    return module.run(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
