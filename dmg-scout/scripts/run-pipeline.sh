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

alembic upgrade head
scout pipeline
