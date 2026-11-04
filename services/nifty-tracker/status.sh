#!/bin/bash
echo "run.py processes:"; pgrep -fl run.py || echo "  none"
echo "port 8899:"; lsof -iTCP:8899 -sTCP:LISTEN -n 2>/dev/null || echo "  not listening"
