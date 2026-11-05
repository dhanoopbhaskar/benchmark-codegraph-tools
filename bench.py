#!/usr/bin/env python3
"""Entry point for the code-graph benchmark.

Examples:
    python3 bench.py init
    python3 bench.py doctor
    python3 bench.py mine --repo BroadleafCommerce --tasks localization --limit 4
    python3 bench.py run --tools codegraph,codebase-memory-mcp --run-id baseline
    python3 bench.py report --run baseline
"""
from benchkit.cli import main

if __name__ == "__main__":
    main()
