#!/bin/bash

rm -rf /var/www/html/openVRE/public/assets/global/plugins/*
mkdir -p /var/www/html/openVRE/public/assets/global/plugins
cp -r /var/www/html/openVRE/plugins-to-copy/. /var/www/html/openVRE/public/assets/global/plugins
