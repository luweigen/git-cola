#!/bin/sh

GIT_EDITOR='subl --wait' BRANCH_MENU="Stop>_traj/stop.md:Memo>_traj/memo.md" ~/work/git-cola/venv/bin/python -m cola dag --all --orphan-isolate