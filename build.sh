#!/usr/bin/env bash
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# Install Chromium browser + system dependencies for Playwright
playwright install --with-deps chromium
