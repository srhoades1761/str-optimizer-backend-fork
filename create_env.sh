#!/bin/sh
# Creates a local .env from env.example. Fill in your own test keys afterwards.
# This file previously contained real credentials; they have been removed.
[ -f .env ] && { echo ".env already exists"; exit 1; }
cp env.example .env && echo "Created .env — edit it and add your test keys."
