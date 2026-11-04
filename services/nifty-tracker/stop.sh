#!/bin/bash
PP=$(lsof -tiTCP:8899 -sTCP:LISTEN 2>/dev/null); [ -n "$PP" ] && kill "$PP" 2>/dev/null
pkill -f "run.py" 2>/dev/null
echo "stop signal sent. Check with: bash status.sh"
