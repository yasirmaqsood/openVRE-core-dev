#!/bin/bash

# SGE qmaster hostname — only needed when the legacy grid engine bridge is enabled.
if [ "${OPENVRE_SGE_ENABLED:-true}" != "false" ]; then
	mkdir -p /var/lib/gridengine/default/common
	echo "${OPENVRE_SGE_HOSTNAME:-sgecore}" > /var/lib/gridengine/default/common/act_qmaster
fi
rm -rf /var/www/html/openVRE/public/assets/global/plugins/*
mkdir -p /var/www/html/openVRE/public/assets/global/plugins
cp -r /var/www/html/openVRE/plugins-to-copy/. /var/www/html/openVRE/public/assets/global/plugins