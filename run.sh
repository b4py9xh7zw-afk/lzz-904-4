#!/usr/bin/env bash
# 启动监控视频增强工具
cd "$(dirname "$0")"
exec python3 app/server.py
