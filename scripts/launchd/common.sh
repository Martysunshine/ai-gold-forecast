# Shared settings for the launchd scripts (sourced, not executed).
LABEL="ai-gold-forecast.worker"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/ai-gold-forecast"
DOMAIN="gui/$(id -u)"
PY="$REPO/backend/.venv/bin/python"
