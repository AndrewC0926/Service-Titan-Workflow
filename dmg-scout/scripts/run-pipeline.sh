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

# 2026-08-13: triggering this script via Render's Jobs API surfaced a
# second, real bug -- but it was NOT a working-directory problem (a direct
# `pwd` job confirmed the cwd here is already /srv/dmg-scout, correctly).
# The actual cause: `scout` is a pip-installed console-script entry point,
# and `pip install .` (no -e, see Dockerfile) copies app/ into
# site-packages as a separate copy -- app/config.py's DEFAULT_CONFIG is
# computed from that copy's own __file__, not from cwd at all, so it
# always resolved to a site-packages/config.yaml that was never put there.
# Fixed at the source via SCOUT_CONFIG in the Dockerfile (load_config()'s
# own documented override), not here -- an explicit `cd` in this script
# would have done nothing for a bug that was never about cwd.
scout check-migrations
alembic upgrade head
scout pipeline
