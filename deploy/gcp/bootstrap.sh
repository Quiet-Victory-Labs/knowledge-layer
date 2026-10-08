#!/usr/bin/env bash
# Installs the small runtime only. No application secrets or demo service are
# exposed through VM metadata. Provision the reviewed install bundle afterward.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 python3
systemctl enable --now docker

device=/dev/disk/by-id/google-knowledge-data
for attempt in $(seq 1 60); do
  [ -b "$device" ] && break
  sleep 1
done
[ -b "$device" ] || exit 1
# Startup scripts also run after reboots. Never infer permission to destroy a
# retained disk from a failed probe. First initialization is an explicit operator
# step after confirming the newly created, unused disk in the reviewed plan.
blkid "$device" >/dev/null 2>&1 || exit 1
[ "$(blkid -s TYPE -o value "$device")" = ext4 ] || exit 1
mkdir -p /srv/knowledge-layer
disk_uuid=$(blkid -s UUID -o value "$device")
if ! findmnt --mountpoint /srv/knowledge-layer >/dev/null; then
  mount "$device" /srv/knowledge-layer
fi
[ "$(findmnt --noheadings --output UUID --mountpoint /srv/knowledge-layer)" = "$disk_uuid" ] || exit 1
if ! grep -q "UUID=$disk_uuid " /etc/fstab; then
  printf 'UUID=%s /srv/knowledge-layer ext4 defaults,nofail 0 2\n' "$disk_uuid" >> /etc/fstab
fi
chmod 700 /srv/knowledge-layer
# Terraform's scheduled disk snapshots are crash-consistent. Install the
# logical-backup timer and prove a restore before treating them as recovery.
