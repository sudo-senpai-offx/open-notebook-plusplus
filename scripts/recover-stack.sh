#!/bin/sh
# Bring the stack back up healthy after a restart, and keep the drain fed.
#
# The box is 14 GB with a 3.8 GB Docker VM, and the model alone is 5.1 GB.
# When two things run at once the kernel starts paging the model out to swap,
# and generation drops from 2.9 tokens per second to 0.2 - which looks like a
# slow model and is really a starved one. So: one job at a time, and the
# swap reclaimed before the model is asked to load.
#
# The reclaim needs the containers down, because the VM's guest memory is most
# of what is being paged out. So the sequence is down, swapoff, up.

set -u

REPO=/home/dev-sudo-senpai/open-notebook
cd "$REPO" || exit 1

echo "[recover] $(date '+%H:%M:%S') bringing the stack down to reclaim the swap"
docker compose down >/dev/null 2>&1
sleep 5
sudo swapoff -a >/dev/null 2>&1 && echo "[recover] the swap is reclaimed"
sudo swapon -a >/dev/null 2>&1
free -h | grep -E "Mem:|Swap:" | sed 's/^/[recover] /'

echo "[recover] starting the stack"
docker compose up -d >/dev/null 2>&1
sleep 30

# One transformation at a time. Two of them together is what starved the
# model and filled the swap.
printf 'OPEN_NOTEBOOK_WORKER_MAX_TASKS=1\n' > notebook_data/throttle.conf
echo "[recover] the window is 1"

# Keep the drain fed.
if ! pgrep -f "scripts/insight-retry.py" >/dev/null 2>&1; then
    nohup .venv/bin/python scripts/insight-retry.py --interval 300 \
        >/tmp/insight-retry.log 2>&1 </dev/null &
    echo "[recover] the drain is running"
else
    echo "[recover] the drain is already running"
fi

echo "[recover] $(date '+%H:%M:%S') done"
