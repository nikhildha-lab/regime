#!/bin/bash
cd "$(dirname "$0")" || exit 1
tail -n 40 -f tracker.log
