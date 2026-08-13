#!/usr/bin/env bash
# Render cron entry point (dmg-scout-pipeline service, see render.yaml).
#
# Was: dockerCommand: sh -c "alembic upgrade head && scout pipeline" -- a
# multi-word, quoted string that Render's own dockerCommand composition does
# not preserve intact (see the commit that added this file for the exact
# failure: "sh: 1: alembic upgrade head && scout pipeline: not found", `sh`
# treating the whole phrase as one opaque token because the `-c`/script-
# argument pairing didn't survive Render's tokenization of a multi-word
# string). A single bare token -- this script's path -- cannot be split
# apart by any tokenizer, naive or not, because there is nothing in it to
# split on. See tests/test_render_config.py for the check that would have
# caught the original bug and prevents this file from regressing the same
# way.
set -euo pipefail

# 2026-08-13: triggering this script via Render's Jobs API (POST
# .../jobs with an explicit startCommand -- used to verify the fix above
# for real, since a local run can't exercise Render's own composition)
# surfaced a second, different bug: load_config() opens config.yaml by a
# relative path, and the Jobs API does not start the process in the
# image's WORKDIR (/srv/dmg-scout, see Dockerfile) the way a normal
# scheduled dockerCommand run does -- it landed in
# /usr/local/lib/python3.12/site-packages instead (real traceback,
# FileNotFoundError, from that exact API-triggered run). A regular
# scheduled cron firing may or may not hit this the same way; cd'ing to
# the known-fixed deployment path makes the script's behavior independent
# of whatever CWD the invoking process started with, either way.
cd /srv/dmg-scout

alembic upgrade head
scout pipeline
