#!/usr/bin/env bash
# Mount an OpenStack Cinder volume (e.g. /dev/vdb) at /data/<project> on a worker node.
# Run on the target node as a user with sudo.
#
# Usage:
#   DEVICE=/dev/vdb PROJECT=project-a ./mount-project-volume-on-node.sh
set -euo pipefail

DEVICE="${DEVICE:-/dev/vdb}"
PROJECT="${PROJECT:-project-a}"
MOUNT="/data/${PROJECT}"

if [[ ! -b "$DEVICE" ]]; then
  echo "ERROR: block device $DEVICE not found."
  echo "Attach the Cinder volume to this VM in OpenStack first, then re-run."
  lsblk
  exit 1
fi

if ! lsblk -no FSTYPE "$DEVICE" | grep -q .; then
  echo "Formatting $DEVICE as ext4..."
  sudo mkfs.ext4 -F "$DEVICE"
fi

sudo mkdir -p "$MOUNT"
if ! mountpoint -q "$MOUNT"; then
  sudo mount "$DEVICE" "$MOUNT"
fi

UUID="$(sudo blkid -s UUID -o value "$DEVICE")"
if ! grep -q "$MOUNT" /etc/fstab; then
  echo "UUID=${UUID} ${MOUNT} ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
fi

for sub in mongodb postgres shareddata tools admindata; do
  sudo mkdir -p "${MOUNT}/${sub}"
done

echo "Mounted $DEVICE -> $MOUNT"
df -h "$MOUNT"
lsblk
