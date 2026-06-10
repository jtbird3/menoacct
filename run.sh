#!/bin/bash
cd "$(dirname "$0")"
# Set ANTHROPIC_API_KEY in your environment before running
uvicorn server:app --port 8765
